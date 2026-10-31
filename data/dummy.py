"""ダミーデータ。race_id から決定的に（毎回同じ内容で）出馬表・オッズ・結果を生成する。

各馬の「強さ」から Harville モデルで着順確率を出し、控除率を引いてオッズにする。
結果も同じ確率から乱数で決めるので、オッズと結果の関係は実際の競馬に近い。
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from itertools import permutations
from typing import Callable

from data.base import DataSource
from models import (
    JST,
    BetType,
    Horse,
    Race,
    RaceResult,
    canonical_race_id,
    combination_key,
    make_race_id,
    parse_race_id,
)

COURSES: dict[str, str] = {"TOKYO": "東京", "KYOTO": "京都", "FUKUSHIMA": "福島"}

# 券種ごとの払戻率（1 − 控除率）
RETURN_RATE: dict[BetType, float] = {
    BetType.WIN: 0.80,
    BetType.PLACE: 0.80,
    BetType.QUINELLA: 0.775,
    BetType.WIDE: 0.775,
    BetType.EXACTA: 0.75,
    BetType.TRIO: 0.75,
    BetType.TRIFECTA: 0.725,
}

_HORSE_PREFIX = ["サクラ", "キタサン", "メイショウ", "ゴールド", "シルバー", "ダイワ", "マチカネ", "エア", "トウカイ", "ナリタ"]
_HORSE_SUFFIX = ["ブレイブ", "スカイ", "ローズ", "サンダー", "ウインド", "ドリーム", "ハート", "スター", "クラウン", "フラッシュ"]
_JOCKEYS = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤", "吉田", "山田", "松本", "井上", "木村", "林"]
_MAIN_RACE_NAMES = ["秋風ステークス", "紅葉賞", "銀杏特別", "霜月ステークス", "菊花特別", "晩秋賞"]


def _rng(*parts: object) -> random.Random:
    seed = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return random.Random(int.from_bytes(seed[:8], "big"))


def _post_time(d: date, number: int) -> datetime:
    """1R 10:00 から35分間隔（11R 15:50）。"""
    return datetime.combine(d, time(10, 0), tzinfo=JST) + timedelta(minutes=35 * (number - 1))


@lru_cache(maxsize=512)
def _build_race(race_id: str) -> tuple[Race, tuple[float, ...]]:
    """レースと各馬の勝率（馬番順）を返す。"""
    key = parse_race_id(race_id)
    r = _rng("race", race_id)
    n = r.randint(10, 16)
    horses = tuple(
        Horse(
            number=i + 1,
            name=r.choice(_HORSE_PREFIX) + r.choice(_HORSE_SUFFIX),
            jockey=r.choice(_JOCKEYS),
        )
        for i in range(n)
    )
    strengths = [r.lognormvariate(0, 0.8) for _ in range(n)]
    total = sum(strengths)
    probs = tuple(s / total for s in strengths)
    if key.number == 11:
        name = r.choice(_MAIN_RACE_NAMES)
    else:
        name = r.choice(["2歳未勝利", "3歳以上1勝クラス", "3歳以上2勝クラス", "2歳新馬"])
    race = Race(
        race_id=race_id,
        date=key.date,
        course=key.course,
        course_name=COURSES[key.course],
        number=key.number,
        name=name,
        surface=r.choice(["芝", "ダート"]),
        distance=r.choice([1200, 1400, 1600, 1800, 2000, 2400]),
        post_time=_post_time(key.date, key.number),
        horses=horses,
    )
    return race, probs


@lru_cache(maxsize=128)
def _probabilities(race_id: str) -> dict[BetType, dict[str, float]]:
    """Harville モデルで全券種の的中確率を計算する。"""
    race, p = _build_race(race_id)
    nums = [h.number for h in race.horses]
    out: dict[BetType, dict[str, float]] = {bt: {} for bt in BetType}

    def add(bt: BetType, horses: tuple[int, ...], prob: float) -> None:
        k = combination_key(bt, horses)
        out[bt][k] = out[bt].get(k, 0.0) + prob

    for i, j, k in permutations(range(len(nums)), 3):
        p2 = p[j] / (1 - p[i])
        p3 = p[k] / (1 - p[i] - p[j])
        prob = p[i] * p2 * p3
        a, b, c = nums[i], nums[j], nums[k]
        add(BetType.TRIFECTA, (a, b, c), prob)
        add(BetType.TRIO, (a, b, c), prob)
        for h in (a, b, c):
            add(BetType.PLACE, (h,), prob)
        for pair in ((a, b), (a, c), (b, c)):
            add(BetType.WIDE, pair, prob)
    for i, j in permutations(range(len(nums)), 2):
        prob = p[i] * p[j] / (1 - p[i])
        add(BetType.EXACTA, (nums[i], nums[j]), prob)
        add(BetType.QUINELLA, (nums[i], nums[j]), prob)
    for i, n in enumerate(nums):
        add(BetType.WIN, (n,), p[i])
    return out


def _odds_from_prob(bt: BetType, prob: float) -> float:
    # 複勝・ワイドも「3着以内に入る確率」から単純に計算する（実際の票数による変動は再現しない）
    odds = RETURN_RATE[bt] / prob if prob > 0 else 9999.9
    return max(1.0, min(round(odds, 1), 9999.9))


class DummyDataSource(DataSource):
    def __init__(self, *, instant_results: bool = False, clock: Callable[[], datetime] | None = None):
        self.instant_results = instant_results
        self.clock = clock or (lambda: datetime.now(JST))

    def get_races(self, race_date: date, course: str | None = None) -> list[Race]:
        courses = list(COURSES)
        if course is not None:
            c = self._normalize_course(course)
            if c is None:
                return []
            courses = [c]
        return [_build_race(make_race_id(race_date, c, n))[0] for c in courses for n in range(1, 13)]

    def get_race(self, race_id: str) -> Race | None:
        try:
            rid = canonical_race_id(race_id)
        except ValueError:
            return None
        if parse_race_id(rid).course not in COURSES:
            return None
        return _build_race(rid)[0]

    def get_odds(self, race_id: str, bet_type: BetType) -> dict[str, float]:
        if self.get_race(race_id) is None:
            return {}
        probs = _probabilities(canonical_race_id(race_id))[bet_type]
        return {k: _odds_from_prob(bet_type, v) for k, v in probs.items()}

    def get_result(self, race_id: str) -> RaceResult | None:
        race = self.get_race(race_id)
        if race is None:
            return None
        if not self.instant_results and self.clock() < race.post_time + timedelta(minutes=10):
            return None
        _, p = _build_race(race.race_id)
        r = _rng("result", race.race_id)
        remaining = list(range(len(race.horses)))
        order: list[int] = []
        while remaining:
            idx = r.choices(remaining, weights=[p[i] for i in remaining])[0]
            remaining.remove(idx)
            order.append(race.horses[idx].number)

        a, b, c = order[:3]
        winners: dict[BetType, list[tuple[int, ...]]] = {
            BetType.WIN: [(a,)],
            BetType.PLACE: [(a,), (b,), (c,)],
            BetType.QUINELLA: [(a, b)],
            BetType.WIDE: [(a, b), (a, c), (b, c)],
            BetType.EXACTA: [(a, b)],
            BetType.TRIO: [(a, b, c)],
            BetType.TRIFECTA: [(a, b, c)],
        }
        payouts: dict[str, dict[str, int]] = {}
        for bt, combos in winners.items():
            odds = self.get_odds(race.race_id, bt)
            payouts[bt.code] = {}
            for combo in combos:
                k = combination_key(bt, combo)
                # 100円あたり払戻。10円未満切り捨て、最低100円
                payouts[bt.code][k] = max(100, int(odds[k] * 100) // 10 * 10)
        return RaceResult(race_id=race.race_id, order=tuple(order), payouts=payouts)

    @staticmethod
    def _normalize_course(course: str) -> str | None:
        c = course.strip().upper()
        if c in COURSES:
            return c
        for code, name in COURSES.items():
            if course.strip() in (name, name + "競馬場"):
                return code
        return None

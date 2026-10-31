"""データ層・ガード層・投票層で共有する型と純粋関数。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


# --------------------------------------------------------------------------
# 券種
# --------------------------------------------------------------------------


class BetType(Enum):
    """券種。value は (コード, 日本語名, 選ぶ頭数, 着順の順番を問うか)。"""

    WIN = ("win", "単勝", 1, True)
    PLACE = ("place", "複勝", 1, True)
    QUINELLA = ("quinella", "馬連", 2, False)
    WIDE = ("wide", "ワイド", 2, False)
    EXACTA = ("exacta", "馬単", 2, True)
    TRIO = ("trio", "3連複", 3, False)
    TRIFECTA = ("trifecta", "3連単", 3, True)

    @property
    def code(self) -> str:
        return self.value[0]

    @property
    def label(self) -> str:
        return self.value[1]

    @property
    def size(self) -> int:
        return self.value[2]

    @property
    def ordered(self) -> bool:
        return self.value[3]


_BET_TYPE_ALIASES: dict[str, BetType] = {}
for _bt in BetType:
    _BET_TYPE_ALIASES[_bt.code] = _bt
    _BET_TYPE_ALIASES[_bt.label] = _bt
# 表記ゆれ
_BET_TYPE_ALIASES.update(
    {
        "三連複": BetType.TRIO,
        "三連単": BetType.TRIFECTA,
        "３連複": BetType.TRIO,
        "３連単": BetType.TRIFECTA,
    }
)

BET_TYPE_HELP = "、".join(f"{bt.code}({bt.label})" for bt in BetType)


def parse_bet_type(value: str) -> BetType:
    """券種コード（win 等）または日本語名（単勝 等）から BetType を返す。"""
    bt = _BET_TYPE_ALIASES.get(str(value).strip().lower()) or _BET_TYPE_ALIASES.get(str(value).strip())
    if bt is None:
        raise ValueError(f"券種 '{value}' は扱えません。使える券種: {BET_TYPE_HELP}")
    return bt


def normalize_horses(bet_type: BetType, horses: tuple[int, ...] | list[int]) -> tuple[int, ...]:
    """順番を問わない券種は馬番を昇順に並べ替える。順番を問う券種はそのまま。"""
    horses = tuple(horses)
    return horses if bet_type.ordered else tuple(sorted(horses))


def combination_key(bet_type: BetType, horses: tuple[int, ...] | list[int]) -> str:
    """買い目の正規化キー。順不同は '1-2-3'、順番ありは '1>2>3'。"""
    sep = ">" if bet_type.ordered and bet_type.size > 1 else "-"
    return sep.join(str(h) for h in normalize_horses(bet_type, horses))


# --------------------------------------------------------------------------
# race_id
# --------------------------------------------------------------------------

_RACE_ID_RE = re.compile(r"^(\d{8})-([A-Z]+)-(\d{1,2})$")


@dataclass(frozen=True)
class RaceKey:
    date: date
    course: str
    number: int


def make_race_id(race_date: date, course: str, number: int) -> str:
    """race_id を作る。形式: YYYYMMDD-競馬場コード-R番号（2桁）例: 20261101-TOKYO-11"""
    return f"{race_date:%Y%m%d}-{course.upper()}-{number:02d}"


def parse_race_id(race_id: str) -> RaceKey:
    """race_id を分解する。形式が不正なら ValueError。"""
    m = _RACE_ID_RE.match(str(race_id).strip().upper())
    if not m:
        raise ValueError(
            f"race_id '{race_id}' の形式が不正です。形式は YYYYMMDD-競馬場コード-R番号（例: 20261101-TOKYO-11）"
        )
    try:
        d = datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError as e:
        raise ValueError(f"race_id '{race_id}' の日付が不正です") from e
    number = int(m.group(3))
    if not 1 <= number <= 12:
        raise ValueError(f"race_id '{race_id}' のレース番号が不正です（1〜12）")
    return RaceKey(d, m.group(2), number)


def canonical_race_id(race_id: str) -> str:
    """表記ゆれ（小文字・R番号1桁）を正規形にそろえる。"""
    k = parse_race_id(race_id)
    return make_race_id(k.date, k.course, k.number)


# --------------------------------------------------------------------------
# 週
# --------------------------------------------------------------------------


def week_start(d: date) -> date:
    """d が属する週の開始日（火曜）。火曜〜翌月曜を1週とし、土日と祝日の月曜開催を同じ週に入れる。"""
    # weekday(): 月=0, 火=1, ... 日=6
    return d - timedelta(days=(d.weekday() - 1) % 7)


# --------------------------------------------------------------------------
# データ
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PastRace:
    """近走成績（出馬表の馬柱）。"""

    date: str
    course: str
    race_name: str
    place: str  # 着順（「中止」などの文字の場合もある）
    field_size: int | None
    popularity: int | None
    jockey: str
    weight: float | None  # 斤量
    distance: str  # 例: 1800ダ
    time: str
    condition: str  # 馬場状態
    horse_weight: int | None
    corners: tuple[int, ...]
    last_3f: str
    winner_or_second: str  # 勝ち馬（自身が勝った場合は2着馬）と着差


@dataclass(frozen=True)
class Horse:
    number: int
    name: str
    jockey: str
    scratched: bool = False
    frame: int | None = None  # 枠番
    sex_age: str = ""  # 例: 牡3
    weight: float | None = None  # 斤量（負担重量）
    trainer: str = ""
    horse_weight: int | None = None  # 馬体重
    horse_weight_diff: int | None = None
    win_odds: float | None = None
    popularity: int | None = None
    sire: str = ""
    dam: str = ""
    dam_sire: str = ""
    past_races: tuple[PastRace, ...] = ()


@dataclass(frozen=True)
class Race:
    race_id: str
    date: date
    course: str
    course_name: str
    number: int
    name: str
    surface: str
    distance: int
    post_time: datetime  # JST
    horses: tuple[Horse, ...]
    race_class: str = ""  # 例: 3歳以上 オープン ハンデ

    def runner_numbers(self) -> set[int]:
        """出走する（取消・除外でない）馬番。"""
        return {h.number for h in self.horses if not h.scratched}


@dataclass(frozen=True)
class RaceResult:
    race_id: str
    order: tuple[int, ...]  # 着順（1着から）
    # 券種コード -> {正規化キー: 100円あたり払戻金}
    payouts: dict[str, dict[str, int]] = field(default_factory=dict)
    # 出走取消・競走除外の馬番。これらを含む買い目は返還（払戻＝投票額）
    refunded_horses: frozenset[int] = frozenset()

    def payout_per_100(self, bet_type: BetType, horses: tuple[int, ...] | list[int]) -> int:
        """的中なら100円あたり払戻金、外れなら0。"""
        return self.payouts.get(bet_type.code, {}).get(combination_key(bet_type, horses), 0)

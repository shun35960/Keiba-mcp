"""補助データ（追い切り・厩舎コメント）の保存と読み込み。

- 投票の経路（DataSource・place_bet）には混ぜない。取得元が落ちても投票は止まらない
- サーバはこのモジュールでファイルを読むだけ。通信も認証情報も使わない（取得は admin.py netkeiba-fetch）
- 投票の記録（db.sqlite）とは別のファイルに置く
- 中身は外部の文章なので、読むときも長さを制限して制御文字を落とす。AI には指示ではなくデータとして渡す
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from models import canonical_race_id

MAX_COMMENT_LENGTH = 300
MAX_NOTE_LENGTH = 100
MAX_SHORT_LENGTH = 30
MAX_HIGHLIGHTS = 10
MAX_HORSE_NUMBER = 18
MAX_FILE_BYTES = 1_000_000


class SupplementError(Exception):
    """補助データのファイルを読めなかった（壊れている・形式が違う）。"""


@dataclass(frozen=True)
class InterimWorkout:
    """中間調教の要点（最終追い切りを除いて、強い調教があった日・時計・短評）。"""

    date: str = ""
    time: str = ""
    note: str = ""


@dataclass(frozen=True)
class HorseInsight:
    number: int
    name: str
    workout_grade: str = ""  # 追い切り評価（尺度は取得元のまま。媒体をまたいで比べない）
    workout_note: str = ""  # 短評
    final_workout_time: str = ""  # 最終追い切りの時計
    interim_count: int | None = None  # 調教の本数（最終追い切りを含む）。None は不明で、0本ではない
    interim_highlights: tuple[InterimWorkout, ...] = ()
    stable_comment: str = ""  # 厩舎コメント（自由文）
    source: str = ""
    fetched_at: str = ""  # 取得時刻（ISO 8601、タイムゾーンつき）


@dataclass(frozen=True)
class RaceInsights:
    race_id: str
    post_time: str  # 発走時刻（ISO 8601、タイムゾーンつき）。これ以降に取得したデータは使わない
    horses: tuple[HorseInsight, ...] = ()


def clean_text(value: Any, limit: int) -> str:
    """制御文字・不可視文字を落として1行にし、limit 文字までに切る。文字列でなければ空。"""
    if not isinstance(value, str):
        return ""
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _aware(value: Any) -> datetime:
    dt = datetime.fromisoformat(value)  # 文字列でなければ TypeError、形式違いは ValueError
    if dt.tzinfo is None:
        raise ValueError("タイムゾーンがありません")
    return dt


def _horse(raw: dict[str, Any]) -> HorseInsight:
    number = raw["number"]
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= MAX_HORSE_NUMBER:
        raise ValueError("馬番が不正です")
    count = raw.get("interim_count")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
        raise ValueError("中間調教の本数が不正です")
    return HorseInsight(
        number=number,
        name=clean_text(raw.get("name"), MAX_SHORT_LENGTH),
        workout_grade=clean_text(raw.get("workout_grade"), MAX_SHORT_LENGTH),
        workout_note=clean_text(raw.get("workout_note"), MAX_NOTE_LENGTH),
        final_workout_time=clean_text(raw.get("final_workout_time"), MAX_NOTE_LENGTH),
        interim_count=count,
        interim_highlights=tuple(
            InterimWorkout(
                date=clean_text(w.get("date"), MAX_SHORT_LENGTH),
                time=clean_text(w.get("time"), MAX_NOTE_LENGTH),
                note=clean_text(w.get("note"), MAX_NOTE_LENGTH),
            )
            for w in raw.get("interim_highlights", ())[:MAX_HIGHLIGHTS]
        ),
        stable_comment=clean_text(raw.get("stable_comment"), MAX_COMMENT_LENGTH),
        source=clean_text(raw["source"], MAX_SHORT_LENGTH),
        fetched_at=raw["fetched_at"],
    )


class SupplementSource(ABC):
    @abstractmethod
    def get_insights(self, race_id: str) -> RaceInsights | None:
        """race_id（正規形）の補助データを返す。なければ None、読めなければ SupplementError。"""


class FileSupplementSource(SupplementSource):
    """<directory>/<race_id>.json を読む。"""

    def __init__(self, directory: str | os.PathLike):
        self.directory = Path(directory)

    def get_insights(self, race_id: str) -> RaceInsights | None:
        rid = canonical_race_id(race_id)  # 数字・英大文字・ハイフンだけになるので、ディレクトリの外は指せない
        path = self.directory / f"{rid}.json"
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise SupplementError(f"{path.name} が大きすぎます")
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise SupplementError(f"{path.name} を読めませんでした（{type(e).__name__}）") from None
        try:
            if raw["race_id"] != rid:
                raise ValueError("race_id が一致しません")
            post_time = _aware(raw["post_time"])
            horses = tuple(_horse(h) for h in raw["horses"])
            # 発走後の情報を予想に混ぜない
            horses = tuple(h for h in horses if _aware(h.fetched_at) < post_time)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise SupplementError(f"{path.name} の形式が不正です（{type(e).__name__}）") from None
        return RaceInsights(race_id=rid, post_time=raw["post_time"], horses=horses)


def save_insights(directory: str | os.PathLike, insights: RaceInsights) -> Path:
    """<directory>/<race_id>.json に書く。途中で失敗しても前のファイルは壊さない。"""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{canonical_race_id(insights.race_id)}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(insights), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path

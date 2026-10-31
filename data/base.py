"""データ取得のインターフェース。実装を差し替えてもサーバ・ガード層は変更しない。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from models import BetType, Race, RaceResult


class DataSource(ABC):
    @abstractmethod
    def get_races(self, race_date: date, course: str | None = None) -> list[Race]:
        """指定日（と競馬場）のレース一覧を出走馬つきで返す。開催がなければ空リスト。"""

    @abstractmethod
    def get_race(self, race_id: str) -> Race | None:
        """race_id（正規形）のレースを返す。存在しなければ None。"""

    @abstractmethod
    def get_odds(self, race_id: str, bet_type: BetType) -> dict[str, float]:
        """券種のオッズを {正規化した買い目キー: オッズ} で返す。複勝・ワイドは下限値。"""

    @abstractmethod
    def get_result(self, race_id: str) -> RaceResult | None:
        """確定した結果を返す。未確定なら None。"""

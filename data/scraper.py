"""Playwright による実データ取得（ステップ6で実装。今はスタブ）。

実装時の必須事項:
- 取得元の利用規約を確認し、スクレイピングが許可されている範囲でのみ取得する
- アクセス間隔を十分に空ける（低頻度）
- 取得結果をキャッシュし、同じページを何度も取りに行かない
"""

from __future__ import annotations

from datetime import date

from data.base import DataSource
from models import BetType, Race, RaceResult


class ScraperDataSource(DataSource):
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("scraper はステップ6で実装します。config.toml の data.source は 'dummy' にしてください。")

    def get_races(self, race_date: date, course: str | None = None) -> list[Race]:
        raise NotImplementedError

    def get_race(self, race_id: str) -> Race | None:
        raise NotImplementedError

    def get_odds(self, race_id: str, bet_type: BetType) -> dict[str, float]:
        raise NotImplementedError

    def get_result(self, race_id: str) -> RaceResult | None:
        raise NotImplementedError

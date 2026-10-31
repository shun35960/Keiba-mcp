"""keiba-mcp サーバ本体。

AI（MCP クライアント）に公開するのは次の4ツールだけ:
  get_race_card / get_odds / place_bet / get_race_result
設定値（上限額など）・認証情報は AI に渡さない。place_bet は必ずガード層を通す。

起動: uv run server.py
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from config import AppConfig, load_config
from data.base import DataSource
from db import Database
from guard import Guard, GuardRejection
from models import JST, BET_TYPE_HELP, BetType, Race, canonical_race_id, parse_bet_type
from voter.base import Voter

logger = logging.getLogger("keiba-mcp")

# 組み合わせ券種のオッズで一度に返す最大件数の上限
MAX_ODDS_LIMIT = 500


def _race_summary(race: Race, *, with_horses: bool) -> dict[str, Any]:
    d: dict[str, Any] = {
        "race_id": race.race_id,
        "course": race.course,
        "course_name": race.course_name,
        "race_number": race.number,
        "name": race.name,
        "surface": race.surface,
        "distance": race.distance,
        "post_time": race.post_time.isoformat(timespec="minutes"),
    }
    if with_horses:
        d["horses"] = [
            {"number": h.number, "name": h.name, "jockey": h.jockey, "scratched": h.scratched} for h in race.horses
        ]
    return d


class KeibaService:
    """MCP ツールの中身。MCP から切り離してテストできるようにしている。"""

    def __init__(
        self,
        *,
        config: AppConfig,
        data: DataSource,
        voter: Voter,
        db: Database,
        clock: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.data = data
        self.voter = voter
        self.db = db
        self.guard = Guard(config.guard, db)
        self.clock = clock or (lambda: datetime.now(JST))

    # ------------------------------------------------------------------ 出馬表・オッズ

    def get_race_card(self, race_date: str, course: str | None = None) -> dict[str, Any]:
        try:
            d = date.fromisoformat(race_date)
        except ValueError:
            raise ValueError(f"date '{race_date}' が不正です。YYYY-MM-DD 形式で指定してください。") from None
        races = self.data.get_races(d, course)
        return {
            "date": d.isoformat(),
            "races": [_race_summary(r, with_horses=True) for r in races],
            "note": "開催がない場合 races は空です。" if not races else "",
        }

    def get_odds(self, race_id: str, bet_type: str | None = None, limit: int = 100) -> dict[str, Any]:
        race = self._require_race(race_id)
        types = [BetType.WIN, BetType.PLACE] if bet_type is None else [parse_bet_type(bet_type)]
        limit = max(1, min(int(limit), MAX_ODDS_LIMIT))
        odds: dict[str, Any] = {}
        truncated: dict[str, int] = {}
        for bt in types:
            o = self.data.get_odds(race.race_id, bt)
            items = sorted(o.items(), key=lambda kv: kv[1])
            if bt.size > 1 and len(items) > limit:
                truncated[bt.code] = len(items)
                items = items[:limit]
            odds[bt.code] = dict(items)
        result: dict[str, Any] = {
            "race": _race_summary(race, with_horses=False),
            "odds": odds,
            "key_format": "順不同の券種は '1-2'（昇順）、順番を問う券種は '1>2>3'（着順どおり）",
        }
        if truncated:
            result["truncated"] = {k: f"全{n}通りのうち人気順（低オッズ順）に上位 {limit} 件のみ" for k, n in truncated.items()}
        return result

    # ------------------------------------------------------------------ 投票

    def place_bet(self, race_id: Any, bet_type: Any, horses: Any, amount: Any) -> dict[str, Any]:
        try:
            rid = canonical_race_id(race_id)
        except (ValueError, TypeError):
            rid = str(race_id)
        race = self.data.get_race(rid)

        try:
            with self.db.transaction():
                try:
                    bet = self.guard.validate(
                        race_id=rid, bet_type=bet_type, horses=horses, amount=amount, race=race, now=self.clock()
                    )
                except GuardRejection as e:
                    self.db.insert_bet(
                        race_id=rid,
                        race_date=race.date.isoformat() if race else None,
                        week_start=None,
                        bet_type=str(bet_type),
                        combination=str(horses),
                        amount=amount if isinstance(amount, int) and not isinstance(amount, bool) else 0,
                        status="rejected",
                        reason=e.reason,
                    )
                    return {"accepted": False, "reason": e.reason}

                receipt = self.voter.vote(bet)
                bet_id = self.db.insert_bet(
                    race_id=bet.race.race_id,
                    race_date=bet.race.date.isoformat(),
                    week_start=bet.week_start,
                    bet_type=bet.bet_type.code,
                    combination=bet.combination,
                    amount=bet.amount,
                    status="accepted",
                    receipt=receipt.receipt,
                )
        except Exception:
            # 投票層の失敗など。記録はロールバックされる。詳細（内部情報）は AI に返さない
            logger.exception("place_bet failed")
            return {"accepted": False, "reason": "投票処理でエラーが発生したため受理されませんでした。"}

        return {
            "accepted": True,
            "bet_id": bet_id,
            "receipt": receipt.receipt,
            "dry_run": receipt.dry_run,
            "race_id": bet.race.race_id,
            "bet_type": bet.bet_type.code,
            "bet_type_label": bet.bet_type.label,
            "combination": bet.combination,
            "amount": bet.amount,
        }

    # ------------------------------------------------------------------ 結果・精算

    def get_race_result(self, race_id: str) -> dict[str, Any]:
        """結果をデータ層から取得し、そのレースの未精算の買い目を精算する。

        結果は必ずデータ層から取る。AI から結果を受け取る経路は作らない（損益をごまかせないように）。
        """
        race = self._require_race(race_id)
        result = self.data.get_result(race.race_id)
        if result is None:
            return {
                "race": _race_summary(race, with_horses=False),
                "status": "未確定",
                "message": "結果はまだ確定していません。発走後しばらくしてから再度取得してください。",
            }

        with self.db.transaction():
            self.db.save_result(result)
            for row in self.db.unsettled_bets(race.race_id):
                bt = parse_bet_type(row["bet_type"])
                per100 = result.payouts.get(bt.code, {}).get(row["combination"], 0)
                self.db.settle_bet(row["id"], row["amount"] * per100 // 100)
            bets = self.db.bets_for_race(race.race_id)

        staked = sum(b["amount"] for b in bets)
        returned = sum(b["payout"] or 0 for b in bets)
        return {
            "race": _race_summary(race, with_horses=False),
            "status": "確定",
            "order": list(result.order),
            "payouts_per_100yen": result.payouts,
            "my_bets": [
                {
                    "bet_id": b["id"],
                    "bet_type": b["bet_type"],
                    "combination": b["combination"],
                    "amount": b["amount"],
                    "payout": b["payout"],
                    "hit": (b["payout"] or 0) > 0,
                }
                for b in bets
            ],
            "race_summary": {"staked": staked, "returned": returned, "profit": returned - staked},
        }

    # ------------------------------------------------------------------

    def _require_race(self, race_id: str) -> Race:
        rid = canonical_race_id(race_id)  # 形式不正は ValueError（そのまま AI に返る）
        race = self.data.get_race(rid)
        if race is None:
            raise ValueError(f"race_id '{race_id}' のレースは存在しません。get_race_card で確認してください。")
        return race


# --------------------------------------------------------------------------
# 組み立て
# --------------------------------------------------------------------------


def build_data_source(config: AppConfig) -> DataSource:
    if config.data_source == "dummy":
        from data.dummy import DummyDataSource

        return DummyDataSource(instant_results=config.dummy_instant_results)
    if config.data_source == "scraper":
        from data.scraper import ScraperDataSource

        return ScraperDataSource()
    raise ValueError(f"config.toml: data.source '{config.data_source}' は不明です")


def build_voter(config: AppConfig) -> Voter:
    if config.voter_mode == "dry_run":
        from voter.dry_run import DryRunVoter

        return DryRunVoter()
    if config.voter_mode == "browser":
        from voter.browser import BrowserVoter

        return BrowserVoter()
    raise ValueError(f"config.toml: voter.mode '{config.voter_mode}' は不明です")


def build_service(config: AppConfig | None = None) -> KeibaService:
    config = config or load_config()
    return KeibaService(
        config=config,
        data=build_data_source(config),
        voter=build_voter(config),
        db=Database(config.db_path),
    )


INSTRUCTIONS = f"""競馬の買い目を決めて投票する MCP サーバです（現在はドライラン：実際の購入は行われません）。
手順: get_race_card で出馬表 → get_odds でオッズ → place_bet で投票 → レース後に get_race_result で結果取得・精算。
race_id の形式: YYYYMMDD-競馬場コード-R番号（例: 20261101-TOKYO-11）。
券種: {BET_TYPE_HELP}。
投票はサーバ側のガード層（上限額・停止条件・重複など）で検証され、拒否されたら理由が返ります。"""


def create_server(service: KeibaService) -> MCPServer:
    mcp = MCPServer("keiba-mcp", instructions=INSTRUCTIONS)

    @mcp.tool()
    def get_race_card(date: str, course: str | None = None) -> dict[str, Any]:
        """指定日の出馬表（レース一覧と出走馬）を返す。

        Args:
            date: 開催日（YYYY-MM-DD）
            course: 競馬場で絞り込む（例: TOKYO / 東京）。省略時は全場
        """
        return service.get_race_card(date, course)

    @mcp.tool()
    def get_odds(race_id: str, bet_type: str | None = None, limit: int = 100) -> dict[str, Any]:
        """指定レースのオッズを返す。

        Args:
            race_id: 例 20261101-TOKYO-11
            bet_type: 券種（win/place/quinella/wide/exacta/trio/trifecta、または 単勝/複勝/馬連/ワイド/馬単/3連複/3連単）。省略時は単勝と複勝
            limit: 組み合わせ券種で返す件数（人気順）。最大 500
        """
        return service.get_odds(race_id, bet_type, limit)

    @mcp.tool()
    def place_bet(race_id: str, bet_type: str, horses: list[int], amount: int) -> dict[str, Any]:
        """買い目を送る。ガード層を通過したものだけ投票される。受理/拒否と理由を返す。

        Args:
            race_id: 例 20261101-TOKYO-11
            bet_type: 券種（win/place/quinella/wide/exacta/trio/trifecta、または日本語名）
            horses: 馬番の配列。単勝・複勝は1頭、馬連・ワイド・馬単は2頭、3連複・3連単は3頭。馬単・3連単は着順どおりに並べる
            amount: 金額（円、100円単位）
        """
        return service.place_bet(race_id, bet_type, horses, amount)

    @mcp.tool()
    def get_race_result(race_id: str) -> dict[str, Any]:
        """レース結果（着順・払戻）を取得し、そのレースの自分の買い目を精算して返す。未確定なら未確定と返す。

        Args:
            race_id: 例 20261101-TOKYO-11
        """
        return service.get_race_result(race_id)

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    create_server(build_service()).run("stdio")


if __name__ == "__main__":
    main()

"""keiba-mcp サーバ本体。

AI（MCP クライアント）に公開するツール:
  get_race_card / get_odds / place_bet / get_race_result / settle_pending_bets / get_stats
  confirm_vote / cancel_vote（二段階投票 confirm = "external" のとき。人間の承認後にだけ confirm_vote を呼ぶ運用）
設定値（上限額など）・認証情報は AI に渡さない。place_bet は必ずガード層を通す。

起動: uv run server.py
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from config import AppConfig, load_config
from data.base import DataSource
from db import Database
from guard import CLOSE_BEFORE_POST, Guard, GuardRejection
from stats import compute_stats
from models import JST, BET_TYPE_HELP, BetType, Race, canonical_race_id, parse_bet_type, parse_race_id
from data.scraper import FetchError
from voter.base import Voter, VoteError, VoteReceipt

logger = logging.getLogger("keiba-mcp")

# 組み合わせ券種のオッズで一度に返す最大件数の上限
MAX_ODDS_LIMIT = 500
# 買い目の理由メモの最大文字数
MAX_NOTE_LENGTH = 500


def _horse_dict(h, *, with_past: bool) -> dict[str, Any]:
    d: dict[str, Any] = {"number": h.number, "name": h.name, "scratched": h.scratched}
    optional = {
        "frame": h.frame,
        "sex_age": h.sex_age,
        "weight": h.weight,
        "jockey": h.jockey,
        "trainer": h.trainer,
        "horse_weight": h.horse_weight,
        "horse_weight_diff": h.horse_weight_diff,
        "win_odds": h.win_odds,
        "popularity": h.popularity,
        "sire": h.sire,
        "dam": h.dam,
        "dam_sire": h.dam_sire,
    }
    d.update({k: v for k, v in optional.items() if v not in (None, "")})
    if with_past and h.past_races:
        d["past_races"] = [asdict(p) for p in h.past_races]
    return d


def _race_summary(race: Race, *, with_horses: bool, with_past: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "race_id": race.race_id,
        "course": race.course,
        "course_name": race.course_name,
        "race_number": race.number,
        "name": race.name,
        "race_class": race.race_class,
        "surface": race.surface,
        "distance": race.distance,
        "post_time": race.post_time.isoformat(timespec="minutes"),
    }
    if with_horses:
        d["horses"] = [_horse_dict(h, with_past=with_past) for h in race.horses]
    return d


def _is_refunded(combination: str, refunded_horses: frozenset[int]) -> bool:
    """出走取消・競走除外の馬を含む買い目は返還。"""
    return bool({int(n) for n in re.findall(r"\d+", combination)} & refunded_horses)


def _race_id_summary(race_id: str) -> dict[str, Any]:
    k = parse_race_id(race_id)
    return {"race_id": race_id, "course": k.course, "race_number": k.number, "date": k.date.isoformat()}


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
        self._recover_pending()

    @property
    def two_phase(self) -> bool:
        return bool(getattr(self.voter, "two_phase", False))

    # ------------------------------------------------------------------ 出馬表・オッズ

    def get_race_card(
        self, race_date: str, course: str | None = None, race_number: int | None = None
    ) -> dict[str, Any]:
        try:
            d = date.fromisoformat(race_date)
        except ValueError:
            raise ValueError(f"date '{race_date}' が不正です。YYYY-MM-DD 形式で指定してください。") from None
        if race_number is not None and not 1 <= race_number <= 12:
            raise ValueError("race_number は 1〜12 で指定してください。")
        races = self.data.get_races(d, course)
        if race_number is not None:
            # レース番号を指定したときは近走成績などの詳細つき
            detailed = [self.data.get_race(r.race_id) for r in races if r.number == race_number]
            races = [r for r in detailed if r is not None]
        return {
            "date": d.isoformat(),
            "races": [_race_summary(r, with_horses=True, with_past=race_number is not None) for r in races],
            "note": "開催がない（または出馬表が未発表の）場合 races は空です。" if not races else "",
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
            "note": "複勝・ワイドは下限値。オッズは発売中に変動します。空の場合は発売前です。",
        }
        if truncated:
            result["truncated"] = {k: f"全{n}通りのうち人気順（低オッズ順）に上位 {limit} 件のみ" for k, n in truncated.items()}
        return result

    # ------------------------------------------------------------------ 投票

    def place_bet(self, race_id: Any, bet_type: Any, horses: Any, amount: Any, note: Any = None) -> dict[str, Any]:
        note = (note.strip()[:MAX_NOTE_LENGTH] or None) if isinstance(note, str) else None
        try:
            rid = canonical_race_id(race_id)
        except (ValueError, TypeError):
            rid = str(race_id)
        try:
            race = self.data.get_race(rid)
        except Exception:
            logger.exception("get_race failed in place_bet")
            return {"accepted": False, "reason": "レース情報を取得できなかったため受理しませんでした。時間をおいて再度試してください。"}

        if self.two_phase:
            return self._place_bet_two_phase(rid, race, bet_type, horses, amount, note)

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
                    note=note,
                )
        except VoteError as e:
            # 購入しなかった（送信前に中止した）。記録はロールバックされる
            logger.warning("place_bet: vote aborted: %s", e.reason)
            return {"accepted": False, "reason": f"投票しませんでした: {e.reason}"}
        except Exception:
            # 投票層の失敗など。記録はロールバックされる。詳細（内部情報）は AI に返さない
            logger.exception("place_bet failed")
            return {"accepted": False, "reason": "投票処理でエラーが発生したため受理されませんでした。"}

        if not receipt.confirmed:
            # 送信はしたが成立を確認できない。損失として数えたまま投票を止め、人間に確認してもらう
            self.db.set_halted("投票結果を確認できない買い目があるため停止（即PAT の投票履歴を人間が確認すること）")

        odds_at_bet = self._snapshot_odds(bet_id, bet)
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
            "odds_at_bet": odds_at_bet,
            "confirmed": receipt.confirmed,
            **(
                {}
                if receipt.confirmed
                else {"warning": "購入を送信しましたが成立を確認できませんでした。投票は停止されました。人間が即PAT の投票履歴を確認します。"}
            ),
        }

    # ------------------------------------------------------------------ 二段階投票（confirm = "external"）

    def _reject_record(self, rid, race, bet_type, horses, amount, reason: str) -> dict[str, Any]:
        self.db.insert_bet(
            race_id=rid,
            race_date=race.date.isoformat() if race else None,
            week_start=None,
            bet_type=str(bet_type),
            combination=str(horses),
            amount=amount if isinstance(amount, int) and not isinstance(amount, bool) else 0,
            status="rejected",
            reason=reason,
        )
        return {"accepted": False, "reason": reason}

    def _place_bet_two_phase(self, rid, race, bet_type, horses, amount, note) -> dict[str, Any]:
        """ガード層で検証して金額を仮押さえし、即PAT の確認画面まで進めて止める（購入はしない）。"""
        self._expire_pending()
        pending_id = secrets.token_urlsafe(24)
        timeout = timedelta(seconds=self.config.browser_external_confirm_timeout_sec)
        try:
            with self.db.transaction():
                if self.db.open_pending():
                    return self._reject_record(
                        rid, race, bet_type, horses, amount,
                        "保留中の買い目があります。人間の承認が取れていれば confirm_vote、そうでなければ cancel_vote で先に片付けてください。",
                    )
                try:
                    bet = self.guard.validate(
                        race_id=rid, bet_type=bet_type, horses=horses, amount=amount, race=race, now=self.clock()
                    )
                except GuardRejection as e:
                    return self._reject_record(rid, race, bet_type, horses, amount, e.reason)
                close_at = bet.race.post_time - CLOSE_BEFORE_POST
                bet_id = self.db.insert_bet(
                    race_id=bet.race.race_id,
                    race_date=bet.race.date.isoformat(),
                    week_start=bet.week_start,
                    bet_type=bet.bet_type.code,
                    combination=bet.combination,
                    amount=bet.amount,
                    status="accepted",  # 仮押さえ（上限・重複・損失に数える）
                    note=note,
                    pending_id=pending_id,
                    pending_expires_at=min(self.clock() + timeout, close_at).isoformat(),
                )
        except Exception:
            logger.exception("place_bet (external) failed")
            return {"accepted": False, "reason": "投票処理でエラーが発生したため受理されませんでした。"}

        try:
            prepared = self.voter.prepare(bet, pending_id)
        except VoteError as e:
            self.db.release_pending(bet_id, f"確認画面まで進めなかった（未購入）: {e.reason}")
            logger.warning("place_bet (external): prepare aborted: %s", e.reason)
            return {"accepted": False, "reason": f"投票しませんでした: {e.reason}"}
        except Exception:
            self.db.release_pending(bet_id, "確認画面まで進めなかった（未購入）")
            logger.exception("place_bet (external): prepare failed")
            return {"accepted": False, "reason": "投票処理でエラーが発生したため受理されませんでした（購入していません）。"}

        # 準備の間に期限切れなどで取り消されていたら、画面を閉じて終わる
        row = self.db.bet_by_pending_id(pending_id)
        if row is None or row["pending_state"] != "pending":
            self.voter.abort(pending_id)
            return {"accepted": False, "reason": "確認画面の準備中に保留が取り消されました（購入していません）。"}
        # 有効期限は確認画面が出た時点から数える（締切が先なら締切まで）
        expires_at = min(self.clock() + timeout, close_at)
        self.db.set_pending_expiry(bet_id, expires_at.isoformat())
        return {
            "accepted": True,
            "status": "pending_confirmation",
            "pending_id": pending_id,
            "expires_at": expires_at.isoformat(timespec="seconds"),
            "bet_id": bet_id,
            "race_id": bet.race.race_id,
            "bet_type": bet.bet_type.code,
            "bet_type_label": bet.bet_type.label,
            "combination": bet.combination,
            "amount": bet.amount,
            "dry_run": False,
            "confirmation_screen": prepared.cart,
            "capture_path": prepared.capture_path,
            "message": (
                "まだ購入していません。即PAT の確認画面で止めています。この内容で人間の承認が取れた場合だけ "
                "confirm_vote(pending_id) を呼んでください。承認されなければ cancel_vote(pending_id)。"
                "有効期限を過ぎると自動で取り消されます。"
            ),
        }

    def confirm_vote(self, pending_id: Any) -> dict[str, Any]:
        """保留中の買い目を確定する（即PAT で「OK」を押す）。人間の承認が取れた後にだけ呼ぶ運用。"""
        if not self.two_phase:
            return {"confirmed": False, "reason": "二段階投票（confirm = \"external\"）が有効ではありません。"}
        self._expire_pending()
        row = self.db.bet_by_pending_id(pending_id) if isinstance(pending_id, str) and pending_id else None
        if row is None:
            return {"confirmed": False, "reason": "その pending_id の保留はありません。"}
        state = row["pending_state"]
        if state == "done":
            return {"confirmed": False, "reason": "この保留は既に確定済みです（二重確定はできません）。", "bet_id": row["id"]}
        if state == "confirming":
            return {"confirmed": False, "reason": "この保留は確定の処理中です。", "bet_id": row["id"]}
        if state != "pending":
            return {"confirmed": False, "reason": f"この保留は取り消し済みです（{row['reason']}）。購入していません。"}
        if not self.db.set_pending_state(row["id"], "confirming", expected="pending"):
            return {"confirmed": False, "reason": "この保留は既に処理されています。"}

        try:
            receipt = self.voter.commit(pending_id)
        except VoteError as e:
            self.db.release_pending(row["id"], f"確定できなかった（未購入）: {e.reason}")
            return {"confirmed": False, "reason": f"購入していません: {e.reason}"}
        except Exception:
            # commit は「OK」を押した後に例外を出さない約束だが、念のため結果不明として扱う
            logger.exception("confirm_vote: unexpected error")
            receipt = VoteReceipt(receipt="UNCONFIRMED", dry_run=False, confirmed=False)

        self.db.finish_pending(row["id"], receipt.receipt)
        if not receipt.confirmed:
            self.db.set_halted("投票結果を確認できない買い目があるため停止（即PAT の投票履歴を人間が確認すること）")
        odds = self._snapshot_odds_for(row["id"], row["race_id"], row["bet_type"], row["combination"])
        out = {
            "confirmed": receipt.confirmed,
            "bet_id": row["id"],
            "receipt": receipt.receipt,
            "dry_run": receipt.dry_run,
            "race_id": row["race_id"],
            "bet_type": row["bet_type"],
            "combination": row["combination"],
            "amount": row["amount"],
            "odds_at_bet": odds,
        }
        if not receipt.confirmed:
            out["warning"] = "購入を送信しましたが成立を確認できませんでした。投票は停止されました。人間が即PAT の投票履歴を確認します。"
        return out

    def cancel_vote(self, pending_id: Any) -> dict[str, Any]:
        """保留中の買い目を取り消す（購入しない）。仮押さえを解放する。"""
        if not self.two_phase:
            return {"cancelled": False, "reason": "二段階投票（confirm = \"external\"）が有効ではありません。"}
        self._expire_pending()
        row = self.db.bet_by_pending_id(pending_id) if isinstance(pending_id, str) and pending_id else None
        if row is None:
            return {"cancelled": False, "reason": "その pending_id の保留はありません。"}
        state = row["pending_state"]
        if state == "released":
            return {"cancelled": True, "reason": f"既に取り消し済みです（{row['reason']}）。購入していません。"}
        if state != "pending" or not self.db.set_pending_state(row["id"], "releasing", expected="pending"):
            return {"cancelled": False, "reason": "この保留は確定済みか確定の処理中のため、取り消せません。"}
        self.voter.abort(pending_id)
        self.db.release_pending(row["id"], "取り消し（未購入）")
        return {"cancelled": True, "bet_id": row["id"], "reason": "取り消しました。購入していません。"}

    def _expire_pending(self) -> None:
        """有効期限（または締切）を過ぎた保留を取り消す。"""
        if not self.two_phase:
            return
        now = self.clock()
        for row in self.db.open_pending():
            if row["pending_state"] != "pending" or not row["pending_expires_at"]:
                continue
            if datetime.fromisoformat(row["pending_expires_at"]) > now:
                continue
            if self.db.set_pending_state(row["id"], "releasing", expected="pending"):
                try:
                    self.voter.abort(row["pending_id"])
                finally:
                    self.db.release_pending(row["id"], "保留の期限切れ（未購入）")

    def _recover_pending(self) -> None:
        """起動時：前回のプロセスの保留を片付ける。ブラウザの画面は残っていないので再送信はしない。"""
        for row in self.db.open_pending():
            if row["pending_state"] == "pending":
                # 「OK」は押していないので、購入していないことが確実
                self.db.release_pending(row["id"], "サーバの再起動で保留が失われた（未購入）")
            else:
                # confirming: 「OK」を押した可能性がある。記録を残したまま停止し、人間が投票履歴を確認する
                self.db.finish_pending(row["id"], "UNCONFIRMED")
                self.db.set_halted("投票結果を確認できない買い目があるため停止（即PAT の投票履歴を人間が確認すること）")

    def _snapshot_odds_for(self, bet_id: int, race_id: str, bet_type: str, combination: str) -> float | None:
        try:
            odds = self.data.get_odds(race_id, parse_bet_type(bet_type)).get(combination)
        except Exception:
            logger.exception("odds snapshot failed")
            return None
        if odds is not None:
            self.db.set_bet_odds(bet_id, odds)
        return odds

    def _snapshot_odds(self, bet_id: int, bet) -> float | None:
        """投票時点のオッズをデータ層から取って記録する（集計用）。取れなくても投票は受理済みのまま。"""
        try:
            odds = self.data.get_odds(bet.race.race_id, bet.bet_type).get(bet.combination)
        except Exception:
            logger.exception("odds snapshot failed")
            return None
        if odds is not None:
            self.db.set_bet_odds(bet_id, odds)
        return odds

    # ------------------------------------------------------------------ 結果・精算

    def get_race_result(self, race_id: str) -> dict[str, Any]:
        """結果をデータ層から取得し、そのレースの未精算の買い目を精算する。

        結果は必ずデータ層から取る。AI から結果を受け取る経路は作らない（損益をごまかせないように）。
        """
        rid = canonical_race_id(race_id)  # 形式不正は ValueError
        # 開催週を過ぎると出馬表は取れないが、結果は取れる。レース情報は取れたときだけ付ける
        race = self.data.get_race(rid)
        summary = _race_summary(race, with_horses=False) if race else _race_id_summary(rid)
        result = self.data.get_result(rid)
        if result is None:
            return {
                "race": summary,
                "status": "未確定",
                "message": "結果はまだ確定していません（または存在しないレースです）。発走後しばらくしてから再度取得してください。",
            }

        with self.db.transaction():
            self.db.save_result(result)
            for row in self.db.unsettled_bets(rid):
                bt = parse_bet_type(row["bet_type"])
                refunded = _is_refunded(row["combination"], result.refunded_horses)
                if refunded:
                    payout = row["amount"]  # 出走取消・競走除外を含む買い目は返還
                else:
                    per100 = result.payouts.get(bt.code, {}).get(row["combination"], 0)
                    payout = row["amount"] * per100 // 100
                self.db.settle_bet(row["id"], payout, refunded=refunded)
            bets = self.db.bets_for_race(rid)

        staked = sum(b["amount"] for b in bets)
        returned = sum(b["payout"] or 0 for b in bets)
        return {
            "race": summary,
            "status": "確定",
            "order": list(result.order),
            "refunded_horses": sorted(result.refunded_horses),
            "payouts_per_100yen": result.payouts,
            "my_bets": [
                {
                    "bet_id": b["id"],
                    "bet_type": b["bet_type"],
                    "combination": b["combination"],
                    "amount": b["amount"],
                    "payout": b["payout"],
                    "hit": (b["payout"] or 0) > 0 and not _is_refunded(b["combination"], result.refunded_horses),
                    "refunded": _is_refunded(b["combination"], result.refunded_horses),
                }
                for b in bets
            ],
            "race_summary": {"staked": staked, "returned": returned, "profit": returned - staked},
        }

    def settle_pending_bets(self) -> dict[str, Any]:
        """未精算の買い目があるレースの結果をすべてデータ層から取得して精算する。"""
        self._expire_pending()
        today = self.clock().date()
        settled: list[dict[str, Any]] = []
        waiting: list[dict[str, str]] = []
        for rid in self.db.unsettled_race_ids():
            if parse_race_id(rid).date > today:
                waiting.append({"race_id": rid, "status": "未確定（開催前）"})
                continue
            try:
                out = self.get_race_result(rid)
            except FetchError:
                logger.exception("settle failed for %s", rid)
                waiting.append({"race_id": rid, "status": "取得失敗（あとで再試行）"})
                continue
            if out["status"] != "確定":
                waiting.append({"race_id": rid, "status": "未確定"})
                continue
            settled.append({"race_id": rid, **out["race_summary"]})
        return {"settled": settled, "waiting": waiting}

    def get_stats(self, since: str | None = None, until: str | None = None) -> dict[str, Any]:
        for label, v in (("since", since), ("until", until)):
            if v is not None:
                try:
                    date.fromisoformat(v)
                except ValueError:
                    raise ValueError(f"{label} '{v}' が不正です。YYYY-MM-DD 形式で指定してください。") from None
        self._expire_pending()
        stats = compute_stats(self.db, since, until)
        stats["betting_halted"] = self.db.halted_reason() is not None
        stats["note"] = (
            "回収率・的中率は精算済みの買い目だけで計算しています（返還は除外）。"
            "未精算がある場合は先に settle_pending_bets を呼んでください。"
        )
        return stats

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
    if config.data_source == "jra":
        from data.scraper import JraDataSource, JraFetcher

        return JraDataSource(
            JraFetcher(
                config.jra_cache_path,
                min_interval=config.jra_request_interval,
                user_agent=config.jra_user_agent,
            )
        )
    raise ValueError(f"config.toml: data.source '{config.data_source}' は不明です")


def build_voter(config: AppConfig) -> Voter:
    if config.voter_mode == "dry_run":
        from voter.dry_run import DryRunVoter

        return DryRunVoter()
    if config.voter_mode == "browser":
        from voter.browser import build_browser_voter

        return build_browser_voter(config)
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
データは JRA 公式サイトから取得します（取得元に負荷をかけないよう、サーバ側で間隔制限とキャッシュをしています。同じ情報を何度も取り直さないでください）。
手順: get_race_card で出馬表 → get_odds でオッズ → place_bet で投票（note に理由を書く）→ レース後に get_race_result または settle_pending_bets で結果取得・精算 → get_stats で成績を振り返る。
出馬表は開催週（通常は木〜金曜に発表）の分だけ取得できます。
race_id の形式: YYYYMMDD-競馬場コード-R番号（例: 20261101-TOKYO-11）。
券種: {BET_TYPE_HELP}。
投票はサーバ側のガード層（上限額・停止条件・重複など）で検証され、拒否されたら理由が返ります。"""


def create_server(service: KeibaService) -> MCPServer:
    mcp = MCPServer("keiba-mcp", instructions=INSTRUCTIONS)

    def guarded(fn):
        """取得元の通信エラーを AI 向けの文言にする。"""

        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except FetchError:
                logger.exception("fetch failed")
                raise ValueError("JRA サイトからの取得に失敗しました。しばらく時間をおいてから再度試してください。") from None

        return wrapper

    @mcp.tool()
    def get_race_card(date: str, course: str | None = None, race_number: int | None = None) -> dict[str, Any]:
        """指定日の出馬表（レース一覧と出走馬）を返す。race_number を指定すると近走成績・血統などの詳細つき。

        Args:
            date: 開催日（YYYY-MM-DD）
            course: 競馬場で絞り込む（例: TOKYO / 東京）。省略時は全場
            race_number: レース番号（1〜12）。指定すると詳細版（例: 11 でメインレース）
        """
        return guarded(service.get_race_card)(date, course, race_number)

    @mcp.tool()
    def get_odds(race_id: str, bet_type: str | None = None, limit: int = 100) -> dict[str, Any]:
        """指定レースのオッズを返す。

        Args:
            race_id: 例 20261101-TOKYO-11
            bet_type: 券種（win/place/quinella/wide/exacta/trio/trifecta、または 単勝/複勝/馬連/ワイド/馬単/3連複/3連単）。省略時は単勝と複勝
            limit: 組み合わせ券種で返す件数（人気順）。最大 500
        """
        return guarded(service.get_odds)(race_id, bet_type, limit)

    @mcp.tool()
    def place_bet(
        race_id: str, bet_type: str, horses: list[int], amount: int, note: str | None = None
    ) -> dict[str, Any]:
        """買い目を送る。ガード層を通過したものだけ投票される。受理/拒否と理由を返す。

        二段階投票（confirm = "external"）のときは購入せず status = "pending_confirmation" と pending_id を返す。
        その場合は人間の承認を得てから confirm_vote、承認されなければ cancel_vote を呼ぶ。

        Args:
            race_id: 例 20261101-TOKYO-11
            bet_type: 券種（win/place/quinella/wide/exacta/trio/trifecta、または日本語名）
            horses: 馬番の配列。単勝・複勝は1頭、馬連・ワイド・馬単は2頭、3連複・3連単は3頭。馬単・3連単は着順どおりに並べる
            amount: 金額（円、100円単位）
            note: この買い目を選んだ理由（任意、500文字まで）。後で成績を振り返るために記録される
        """
        return service.place_bet(race_id, bet_type, horses, amount, note)

    @mcp.tool()
    def confirm_vote(pending_id: str) -> dict[str, Any]:
        """二段階投票で保留中の買い目を確定する（即PAT で実際に購入する）。

        **人間（ユーザー）がこの買い目の購入を承認した後にだけ呼ぶこと。** 承認が取れていない・不明なときは呼ばない。
        期限切れ・不明な pending_id・二重確定は拒否される。

        Args:
            pending_id: place_bet が返した pending_id
        """
        return service.confirm_vote(pending_id)

    @mcp.tool()
    def cancel_vote(pending_id: str) -> dict[str, Any]:
        """二段階投票で保留中の買い目を取り消す（購入しない）。人間が承認しなかったときに呼ぶ。

        Args:
            pending_id: place_bet が返した pending_id
        """
        return service.cancel_vote(pending_id)

    @mcp.tool()
    def get_race_result(race_id: str) -> dict[str, Any]:
        """レース結果（着順・払戻）を取得し、そのレースの自分の買い目を精算して返す。未確定なら未確定と返す。

        Args:
            race_id: 例 20261101-TOKYO-11
        """
        return guarded(service.get_race_result)(race_id)

    @mcp.tool()
    def settle_pending_bets() -> dict[str, Any]:
        """結果がまだ反映されていない自分の買い目を、まとめて結果取得・精算する。成績を見る前に呼ぶ。"""
        return service.settle_pending_bets()

    @mcp.tool()
    def get_stats(since: str | None = None, until: str | None = None) -> dict[str, Any]:
        """ドライランの成績（回収率・的中率・収支）を、全体・券種別・週別・投票時オッズ帯別に返す。直近の買い目と理由メモも返す。

        Args:
            since: 集計開始日（YYYY-MM-DD、レース日で判定）。省略時は全期間
            until: 集計終了日（YYYY-MM-DD）。省略時は全期間
        """
        return service.get_stats(since, until)

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    create_server(build_service()).run("stdio")


if __name__ == "__main__":
    main()

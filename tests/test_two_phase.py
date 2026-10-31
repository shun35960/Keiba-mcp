"""二段階投票（confirm = "external"）のサービス側のテスト。投票層は偽物を使い、ブラウザは起動しない。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from db import Database
from guard import GuardRejection
from models import JST
from server import KeibaService
from tests.conftest import SAT_MAIN, SAT_MAIN_KYOTO, make_config
from voter.base import Voter, VoteError, VoteReceipt
from voter.browser import PreparedVote


class FakeTwoPhase(Voter):
    two_phase = True

    def __init__(self):
        self.sessions: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.prepare_error: Exception | None = None
        self.commit_error: Exception | None = None
        self.commit_receipt = VoteReceipt(receipt="IPAT-0042", dry_run=False, confirmed=True)

    def vote(self, bet):
        raise AssertionError("external では vote を使わない")

    def prepare(self, bet, pending_id):
        self.calls.append(("prepare", pending_id))
        if self.prepare_error:
            raise self.prepare_error
        self.sessions.add(pending_id)
        cart = {"race_number": bet.race.number, "horses": list(bet.horses), "amount": bet.amount, "total": bet.amount}
        return PreparedVote(cart=cart, capture_path="/captures/voter/confirm")

    def commit(self, pending_id):
        self.calls.append(("commit", pending_id))
        if pending_id not in self.sessions:
            raise VoteError("保留中の確認画面が残っていません")
        self.sessions.discard(pending_id)
        if self.commit_error:
            raise self.commit_error
        return self.commit_receipt

    def abort(self, pending_id):
        self.calls.append(("abort", pending_id))
        self.sessions.discard(pending_id)


@pytest.fixture
def fake():
    return FakeTwoPhase()


@pytest.fixture
def svc(make_service, fake):
    return make_service(voter=fake, weekly_limit=10000, per_race_limit=10000)


def row(service, bet_id):
    return service.db._conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()


def place(service, race_id=SAT_MAIN, horses=(1,), amount=300, bet_type="win"):
    return service.place_bet(race_id, bet_type, list(horses), amount)


# ---------------------------------------------------------------- prepare → confirm


def test_prepare_then_confirm(svc, fake):
    r = place(svc)
    assert r["status"] == "pending_confirmation" and r["accepted"] is True
    assert len(r["pending_id"]) >= 32  # 推測困難なランダム値
    assert r["confirmation_screen"]["amount"] == 300 and r["capture_path"]
    assert "まだ購入していません" in r["message"]
    assert row(svc, r["bet_id"])["pending_state"] == "pending"
    assert svc.get_stats()["awaiting_confirmation"] == {"bets": 1, "amount": 300}
    assert svc.get_stats()["recent_bets"][0]["status"] == "確定待ち"

    c = svc.confirm_vote(r["pending_id"])
    assert c["confirmed"] is True and c["receipt"] == "IPAT-0042" and c["bet_id"] == r["bet_id"]
    rr = row(svc, r["bet_id"])
    assert (rr["status"], rr["pending_state"], rr["receipt"]) == ("accepted", "done", "IPAT-0042")
    s = svc.get_stats()
    assert s["awaiting_confirmation"]["bets"] == 0 and s["pending"]["bets"] == 1  # 購入済み・未精算
    assert svc.db.halted_reason() is None


def test_double_confirm_rejected(svc, fake):
    r = place(svc)
    assert svc.confirm_vote(r["pending_id"])["confirmed"] is True
    again = svc.confirm_vote(r["pending_id"])
    assert again["confirmed"] is False and "既に確定済み" in again["reason"]
    assert [c[0] for c in fake.calls].count("commit") == 1


@pytest.mark.parametrize("pid", ["no-such-id", "", None, 123])
def test_unknown_pending_id(svc, pid):
    assert "ありません" in svc.confirm_vote(pid)["reason"]
    assert svc.cancel_vote(pid)["cancelled"] is False


# ---------------------------------------------------------------- prepare → cancel


def test_prepare_then_cancel_releases(svc, fake):
    r = place(svc)
    c = svc.cancel_vote(r["pending_id"])
    assert c["cancelled"] is True and "購入していません" in c["reason"]
    assert ("abort", r["pending_id"]) in fake.calls
    rr = row(svc, r["bet_id"])
    assert (rr["status"], rr["pending_state"]) == ("rejected", "released")
    assert svc.db.week_total("2026-11-03") == 0 and svc.db.cumulative_loss() == 0
    # 仮押さえが解放されたので、同じ買い目をもう一度出せる
    assert place(svc)["status"] == "pending_confirmation"
    # 取り消した保留は確定できない
    assert "取り消し済み" in svc.confirm_vote(r["pending_id"])["reason"]


def test_cannot_cancel_after_confirm(svc):
    r = place(svc)
    svc.confirm_vote(r["pending_id"])
    assert svc.cancel_vote(r["pending_id"])["cancelled"] is False


# ---------------------------------------------------------------- 期限切れ


def test_expired_pending_cannot_be_confirmed(svc, fake, clock):
    r = place(svc)
    clock.now += timedelta(seconds=301)
    c = svc.confirm_vote(r["pending_id"])
    assert c["confirmed"] is False and "期限切れ" in c["reason"]
    assert ("abort", r["pending_id"]) in fake.calls
    assert "commit" not in [x[0] for x in fake.calls]
    assert row(svc, r["bet_id"])["status"] == "rejected"


def test_expiry_is_capped_at_deadline(svc, clock):
    post = svc.data.get_race(SAT_MAIN).post_time
    clock.now = post - timedelta(minutes=3)  # 締切（発走2分前）まで残り1分
    r = place(svc)
    assert datetime.fromisoformat(r["expires_at"]) == post - timedelta(minutes=2)
    clock.now = post - timedelta(minutes=2)
    assert "期限切れ" in svc.confirm_vote(r["pending_id"])["reason"]


def test_expiry_happens_on_other_calls_too(svc, fake, clock):
    r = place(svc)
    clock.now += timedelta(seconds=400)
    svc.get_stats()
    assert row(svc, r["bet_id"])["pending_state"] == "released"


# ---------------------------------------------------------------- 仮押さえが上限・重複・損失に効く


def test_pending_counts_toward_limits_and_duplicates(svc):
    r = place(svc, amount=300)
    assert svc.db.week_total("2026-11-03") == 300
    assert svc.db.race_total(SAT_MAIN) == 300
    assert svc.db.cumulative_loss() == 300
    race = svc.data.get_race(SAT_MAIN)
    with pytest.raises(GuardRejection, match="投票済み"):
        svc.guard.validate(race_id=SAT_MAIN, bet_type="win", horses=[1], amount=100, race=race, now=svc.clock())
    assert r["status"] == "pending_confirmation"


def test_pending_limits_against_small_weekly_limit(make_service, fake):
    service = make_service(voter=fake, weekly_limit=500, per_race_limit=500)
    r = place(service, amount=500)
    service.cancel_vote(r["pending_id"])
    r2 = place(service, amount=500)
    assert r2["status"] == "pending_confirmation"
    service.confirm_vote(r2["pending_id"])
    r3 = place(service, race_id=SAT_MAIN_KYOTO, amount=100)
    assert r3["accepted"] is False and "今週" in r3["reason"]


def test_only_one_pending_at_a_time(svc, fake):
    place(svc)
    r2 = place(svc, race_id=SAT_MAIN_KYOTO)
    assert r2["accepted"] is False and "保留中の買い目" in r2["reason"]
    assert [c[0] for c in fake.calls].count("prepare") == 1


# ---------------------------------------------------------------- 失敗

def test_prepare_failure_releases(svc, fake):
    fake.prepare_error = VoteError("購入予定リストに既に買い目が入っています")
    r = place(svc)
    assert r["accepted"] is False and "既に買い目" in r["reason"]
    assert svc.db.open_pending() == [] and svc.db.week_total("2026-11-03") == 0


def test_commit_vote_error_releases(svc, fake):
    r = place(svc)
    fake.commit_error = VoteError("即PAT の確認画面が閉じていたため、購入していません")
    c = svc.confirm_vote(r["pending_id"])
    assert c["confirmed"] is False and "購入していません" in c["reason"]
    assert row(svc, r["bet_id"])["status"] == "rejected"
    assert svc.db.halted_reason() is None


def test_commit_unconfirmed_halts(svc, fake):
    fake.commit_receipt = VoteReceipt(receipt="UNCONFIRMED", dry_run=False, confirmed=False)
    r = place(svc)
    c = svc.confirm_vote(r["pending_id"])
    assert c["confirmed"] is False and "停止" in c["warning"]
    assert row(svc, r["bet_id"])["status"] == "accepted"  # 損失として数えたまま
    assert "成立を確認できない" in place(svc, race_id=SAT_MAIN_KYOTO)["reason"]


def test_commit_unexpected_exception_is_treated_as_unconfirmed(svc, fake):
    r = place(svc)
    fake.commit_error = RuntimeError("boom")
    c = svc.confirm_vote(r["pending_id"])
    assert c["confirmed"] is False and svc.db.halted_reason() is not None


# ---------------------------------------------------------------- 再起動


def test_restart_releases_pending_without_resending(tmp_path, clock, fake):
    cfg = make_config(tmp_path, weekly_limit=10000, per_race_limit=10000)
    from data.dummy import DummyDataSource

    def make():
        return KeibaService(config=cfg, data=DummyDataSource(instant_results=True, clock=clock), voter=fake,
                            db=Database(cfg.db_path), clock=clock)

    s1 = make()
    r = place(s1)
    s1.db.close()
    fake.sessions.clear()  # プロセスが落ちて画面も消えた
    s2 = make()
    rr = row(s2, r["bet_id"])
    assert (rr["status"], rr["pending_state"]) == ("rejected", "released") and "再起動" in rr["reason"]
    assert "取り消し済み" in s2.confirm_vote(r["pending_id"])["reason"]
    assert [c[0] for c in fake.calls].count("commit") == 0  # 再送信しない
    assert s2.db.halted_reason() is None
    s2.db.close()


def test_restart_during_confirm_halts(tmp_path, clock, fake):
    cfg = make_config(tmp_path, weekly_limit=10000, per_race_limit=10000)
    from data.dummy import DummyDataSource

    def make():
        return KeibaService(config=cfg, data=DummyDataSource(instant_results=True, clock=clock), voter=fake,
                            db=Database(cfg.db_path), clock=clock)

    s1 = make()
    r = place(s1)
    s1.db.set_pending_state(r["bet_id"], "confirming")  # OK を押している最中に落ちた
    s1.db.close()
    s2 = make()
    rr = row(s2, r["bet_id"])
    assert (rr["status"], rr["pending_state"], rr["receipt"]) == ("accepted", "done", "UNCONFIRMED")
    assert "確認できない" in s2.db.halted_reason()
    s2.db.close()


# ---------------------------------------------------------------- external 以外


def test_confirm_and_cancel_without_external(service):
    assert "external" in service.confirm_vote("x")["reason"]
    assert "external" in service.cancel_vote("x")["reason"]
    assert service.place_bet(SAT_MAIN, "win", [1], 100)["accepted"] is True  # 通常どおり

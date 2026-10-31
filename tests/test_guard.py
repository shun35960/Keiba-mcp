"""ガード層のテスト。ここが破られると実害が出るので網羅する。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from models import JST
from tests.conftest import SAT, SAT_MAIN, SAT_MAIN_KYOTO, SUN, SUN_MAIN, runners
from voter.base import Voter


def bet(service, race_id=SAT_MAIN, bet_type="win", horses=None, amount=100):
    if horses is None:
        horses = [runners(service, race_id)[0]]
    return service.place_bet(race_id, bet_type, horses, amount)


def count_bets(service, status):
    return service.db._conn.execute("SELECT COUNT(*) FROM bets WHERE status = ?", (status,)).fetchone()[0]


# ---------------------------------------------------------------- 受理


def test_valid_bet_is_accepted_and_recorded(service):
    r = bet(service, amount=500)
    assert r["accepted"] is True
    assert r["dry_run"] is True
    assert r["receipt"].startswith("DRY-")
    assert count_bets(service, "accepted") == 1


@pytest.mark.parametrize(
    "bet_type,size", [("win", 1), ("place", 1), ("quinella", 2), ("wide", 2), ("exacta", 2), ("trio", 3), ("trifecta", 3)]
)
def test_all_bet_types_accepted(make_service, bet_type, size):
    service = make_service(weekly_limit=100000, per_race_limit=100000)
    r = bet(service, bet_type=bet_type, horses=runners(service, SAT_MAIN)[:size])
    assert r["accepted"] is True, r


def test_japanese_bet_type_and_lowercase_race_id(service):
    r = service.place_bet(SAT_MAIN.lower(), "馬連", runners(service, SAT_MAIN)[:2], 100)
    assert r["accepted"] is True
    assert r["bet_type"] == "quinella"


# ---------------------------------------------------------------- レース


@pytest.mark.parametrize("race_id", ["bad", "2026-11-07-TOKYO-11", "20261107-TOKYO-13"])
def test_reject_malformed_race_id(service, race_id):
    r = service.place_bet(race_id, "win", [1], 100)
    assert r["accepted"] is False
    assert "race_id" in r["reason"]


def test_reject_unknown_course(service):
    r = service.place_bet(f"{SAT}-NAKAYAMA-11", "win", [1], 100)
    assert r["accepted"] is False
    assert "存在しません" in r["reason"]


def test_reject_race_number_not_allowed(service):
    r = bet(service, race_id=f"{SAT}-TOKYO-10")
    assert r["accepted"] is False
    assert "対象外" in r["reason"]


def test_all_races_allowed_when_list_empty(make_service):
    service = make_service(allowed_race_numbers=frozenset())
    assert bet(service, race_id=f"{SAT}-TOKYO-01")["accepted"] is True


def test_reject_after_close(service, clock):
    post = service.data.get_race(SAT_MAIN).post_time
    clock.now = post - timedelta(minutes=2)
    r = bet(service)
    assert r["accepted"] is False
    assert "締切" in r["reason"]


def test_accept_just_before_close(service, clock):
    post = service.data.get_race(SAT_MAIN).post_time
    clock.now = post - timedelta(minutes=2, seconds=1)
    assert bet(service)["accepted"] is True


def test_reject_past_race(service, clock):
    clock.now = datetime(2026, 11, 8, 9, 0, tzinfo=JST)
    assert bet(service, race_id=SAT_MAIN)["accepted"] is False


# ---------------------------------------------------------------- 券種・馬番


def test_reject_unknown_bet_type(service):
    r = bet(service, bet_type="bracket_quinella")
    assert r["accepted"] is False
    assert "券種" in r["reason"]


@pytest.mark.parametrize("bet_type,horses", [("win", [1, 2]), ("quinella", [1]), ("trio", [1, 2]), ("trifecta", [1, 2, 3, 4]), ("win", [])])
def test_reject_wrong_number_of_horses(service, bet_type, horses):
    r = bet(service, bet_type=bet_type, horses=horses)
    assert r["accepted"] is False
    assert "頭指定" in r["reason"]


def test_reject_same_horse_twice(service):
    r = bet(service, bet_type="quinella", horses=[1, 1])
    assert r["accepted"] is False
    assert "重複" in r["reason"]


def test_reject_nonexistent_horse(service):
    n = max(runners(service, SAT_MAIN))
    r = bet(service, horses=[n + 1])
    assert r["accepted"] is False
    assert "出走しません" in r["reason"]


@pytest.mark.parametrize("horses", [[0], [-1], ["1"], [1.0], [True], "1", None])
def test_reject_invalid_horse_values(service, horses):
    r = service.place_bet(SAT_MAIN, "win", horses, 100)
    assert r["accepted"] is False


# ---------------------------------------------------------------- 金額


@pytest.mark.parametrize("amount", [0, -100, 50, 150, 99, "100", 100.0, True, None])
def test_reject_invalid_amount(service, amount):
    r = bet(service, amount=amount)
    assert r["accepted"] is False
    assert count_bets(service, "accepted") == 0


# ---------------------------------------------------------------- 重複


def test_reject_duplicate_bet(service):
    assert bet(service, horses=[1])["accepted"] is True
    r = bet(service, horses=[1])
    assert r["accepted"] is False
    assert "投票済み" in r["reason"]


def test_duplicate_detection_ignores_order_for_unordered_types(service):
    assert bet(service, bet_type="quinella", horses=[1, 2])["accepted"] is True
    assert bet(service, bet_type="quinella", horses=[2, 1])["accepted"] is False


def test_ordered_types_are_not_duplicates_when_reversed(service):
    assert bet(service, bet_type="exacta", horses=[1, 2])["accepted"] is True
    assert bet(service, bet_type="exacta", horses=[2, 1])["accepted"] is True


def test_same_horses_different_bet_type_is_not_duplicate(service):
    assert bet(service, bet_type="win", horses=[1])["accepted"] is True
    assert bet(service, bet_type="place", horses=[1])["accepted"] is True


def test_rejected_bet_does_not_block_retry(service):
    assert bet(service, horses=[1], amount=150)["accepted"] is False
    assert bet(service, horses=[1], amount=100)["accepted"] is True


# ---------------------------------------------------------------- 上限額


def test_per_race_limit(make_service):
    service = make_service(per_race_limit=500, weekly_limit=10000)
    assert bet(service, horses=[1], amount=300)["accepted"] is True
    assert bet(service, horses=[2], amount=200)["accepted"] is True  # ちょうど上限
    r = bet(service, horses=[3], amount=100)
    assert r["accepted"] is False
    assert "1レースあたり上限額" in r["reason"]
    # 別レースなら通る
    assert bet(service, race_id=SAT_MAIN_KYOTO, horses=[1], amount=100)["accepted"] is True


def test_single_bet_over_per_race_limit(make_service):
    service = make_service(per_race_limit=500, weekly_limit=10000)
    assert bet(service, amount=600)["accepted"] is False


def test_weekly_limit_spans_saturday_and_sunday(service):
    # 既定: 週 1000 円
    assert bet(service, race_id=SAT_MAIN, horses=[1], amount=600)["accepted"] is True
    r = bet(service, race_id=SUN_MAIN, horses=[1], amount=500)
    assert r["accepted"] is False
    assert "今週" in r["reason"]
    assert bet(service, race_id=SUN_MAIN, horses=[1], amount=400)["accepted"] is True


def test_weekly_limit_includes_monday_holiday(service):
    assert bet(service, race_id=SAT_MAIN, horses=[1], amount=1000)["accepted"] is True
    assert bet(service, race_id="20261109-TOKYO-11", horses=[1], amount=100)["accepted"] is False


def test_weekly_limit_resets_next_week(service):
    assert bet(service, race_id=SAT_MAIN, horses=[1], amount=1000)["accepted"] is True
    assert bet(service, race_id="20261114-TOKYO-11", horses=[1], amount=1000)["accepted"] is True


def test_rejected_bets_do_not_count_toward_limits(service):
    assert bet(service, horses=[1], amount=1100)["accepted"] is False
    assert bet(service, horses=[1], amount=1000)["accepted"] is True


def test_limit_messages_do_not_reveal_config_values(make_service):
    service = make_service(per_race_limit=700, weekly_limit=900, loss_stop_threshold=1300)
    per_race = bet(service, horses=[1], amount=800)
    assert bet(service, race_id=SAT_MAIN_KYOTO, horses=[1], amount=700)["accepted"] is True
    weekly = bet(service, race_id=SUN_MAIN, horses=[1], amount=300)
    service2 = make_service(per_race_limit=100000, weekly_limit=100000, loss_stop_threshold=1300)
    loss = bet(service2, race_id=SUN_MAIN, horses=[2], amount=700)
    for r in (per_race, weekly, loss):
        assert r["accepted"] is False
        reason = r["reason"]
        for secret in ("700", "900", "1300"):
            assert secret not in reason


# ---------------------------------------------------------------- 損失による停止


def test_bet_that_could_exceed_loss_threshold_is_rejected(make_service):
    service = make_service(per_race_limit=100000, weekly_limit=100000, loss_stop_threshold=1000)
    assert bet(service, horses=[1], amount=800)["accepted"] is True
    r = bet(service, horses=[2], amount=300)
    assert r["accepted"] is False
    assert "停止ライン" in r["reason"]


def test_halts_when_loss_reaches_threshold_and_stays_halted(make_service, clock):
    service = make_service(per_race_limit=100000, weekly_limit=100000, loss_stop_threshold=1000)
    race = service.data.get_race(SAT_MAIN)
    result = service.data.get_result(SAT_MAIN)
    loser = next(n for n in runners(service, SAT_MAIN) if n != result.order[0])
    assert bet(service, horses=[loser], amount=1000)["accepted"] is True

    r = bet(service, race_id=SAT_MAIN_KYOTO, horses=[1], amount=100)
    assert r["accepted"] is False
    assert "自動停止中" in r["reason"]
    assert service.db.halted_reason() is not None

    # 精算されても、別の週になっても停止は解除されない（解除は人間が行う）
    clock.now = race.post_time + timedelta(hours=1)
    service.get_race_result(SAT_MAIN)
    clock.now = datetime(2026, 11, 14, 9, 0, tzinfo=JST)
    r = bet(service, race_id="20261114-TOKYO-11", horses=[1], amount=100)
    assert r["accepted"] is False
    assert "自動停止中" in r["reason"]


def test_halt_survives_restart(make_service):
    service = make_service(loss_stop_threshold=100)
    assert bet(service, amount=100)["accepted"] is True
    assert bet(service, race_id=SAT_MAIN_KYOTO, amount=100)["accepted"] is False
    service2 = make_service(loss_stop_threshold=100)  # 同じ DB ファイルで再起動
    assert bet(service2, race_id=SUN_MAIN, amount=100)["reason"].startswith("累計損失")


def test_halt_checked_before_anything_else(make_service):
    service = make_service(loss_stop_threshold=100)
    service.db.set_halted("test")
    r = service.place_bet("garbage", "win", [1], 100)
    assert "自動停止中" in r["reason"]


# ---------------------------------------------------------------- 記録・投票層


def test_rejections_are_recorded_with_reason(service):
    bet(service, amount=150)
    row = service.db._conn.execute("SELECT status, reason FROM bets").fetchone()
    assert row["status"] == "rejected"
    assert "円単位" in row["reason"]


class FailingVoter(Voter):
    def vote(self, bet):
        raise RuntimeError("boom secret-detail")


def test_voter_failure_is_not_recorded_as_accepted(make_service):
    service = make_service(voter=FailingVoter())
    r = bet(service)
    assert r["accepted"] is False
    assert "secret-detail" not in r["reason"]
    assert count_bets(service, "accepted") == 0


def test_only_validated_bets_reach_voter(make_service):
    calls = []

    class SpyVoter(Voter):
        def vote(self, bet):
            calls.append(bet)
            from voter.dry_run import DryRunVoter

            return DryRunVoter().vote(bet)

    service = make_service(voter=SpyVoter())
    bet(service, amount=150)
    bet(service, bet_type="quinella", horses=[1, 1])
    assert calls == []
    bet(service, amount=100)
    assert len(calls) == 1

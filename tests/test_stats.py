"""ステップ7: 成績集計・まとめて精算・投票時オッズと理由メモの記録。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from db import Database
from models import JST
from report import render
from tests.conftest import SAT_MAIN, SAT_MAIN_KYOTO, SUN_MAIN, runners


def _bet(db, *, race_id="20261107-TOKYO-11", race_date="2026-11-07", week="2026-11-03", bet_type="win",
         combination="1", amount=100, payout=None, refunded=False, odds=None, note=None):
    bid = db.insert_bet(race_id=race_id, race_date=race_date, week_start=week, bet_type=bet_type,
                        combination=combination, amount=amount, status="accepted", receipt="DRY", note=note)
    if odds is not None:
        db.set_bet_odds(bid, odds)
    if payout is not None:
        db.settle_bet(bid, payout, refunded=refunded)
    return bid


# ---------------------------------------------------------------- 集計


def test_empty_stats(service):
    s = service.get_stats()
    assert s["overall"]["bets"] == 0
    assert s["overall"]["recovery_rate"] is None
    assert s["by_bet_type"] == {} and s["by_week"] == [] and s["recent_bets"] == []
    assert s["betting_halted"] is False


def test_recovery_and_hit_rate(service):
    db = service.db
    _bet(db, combination="1", amount=300, payout=1260, odds=4.2)  # 的中
    _bet(db, combination="2", amount=200, payout=0, odds=12.0)  # 外れ
    _bet(db, bet_type="quinella", combination="1-2", amount=500, payout=0, odds=35.5)  # 外れ
    s = service.get_stats()["overall"]
    assert (s["bets"], s["hits"], s["staked"], s["returned"], s["profit"]) == (3, 1, 1000, 1260, 260)
    assert s["recovery_rate"] == 126.0
    assert s["hit_rate"] == 33.3
    assert s["races"] == 1


def test_pending_and_refunded_are_excluded_from_rates(service):
    db = service.db
    _bet(db, combination="1", amount=100, payout=0)
    _bet(db, combination="2", amount=500)  # 未精算
    _bet(db, combination="3", amount=300, payout=300, refunded=True)  # 返還
    s = service.get_stats()
    assert s["overall"]["staked"] == 100 and s["overall"]["recovery_rate"] == 0.0
    assert s["pending"] == {"bets": 1, "amount": 500, "race_ids": ["20261107-TOKYO-11"]}
    assert s["refunded"] == {"bets": 1, "amount": 300}
    statuses = {b["combination"]: b["status"] for b in s["recent_bets"]}
    assert statuses == {"1": "外れ", "2": "未精算", "3": "返還"}


def test_breakdowns(service):
    db = service.db
    _bet(db, bet_type="win", combination="1", amount=100, payout=250, odds=2.5)
    _bet(db, bet_type="wide", combination="1-2", amount=100, payout=0, odds=1.5)
    _bet(db, race_id="20261114-TOKYO-11", race_date="2026-11-14", week="2026-11-10",
         bet_type="win", combination="3", amount=200, payout=0, odds=150.0)
    s = service.get_stats()
    assert set(s["by_bet_type"]) == {"単勝", "ワイド"}
    assert s["by_bet_type"]["単勝"]["staked"] == 300
    assert [(w["week_start"], w["profit"]) for w in s["by_week"]] == [("2026-11-03", 50), ("2026-11-10", -200)]
    bands = {b["odds_band"]: b["bets"] for b in s["by_odds_at_bet"]}
    assert bands == {"1.9倍以下": 1, "2.0〜4.9倍": 1, "100倍以上": 1}


def test_period_filter(service):
    db = service.db
    _bet(db, amount=100, payout=0)
    _bet(db, race_id="20261114-TOKYO-11", race_date="2026-11-14", week="2026-11-10", amount=200, payout=0)
    assert service.get_stats(since="2026-11-08")["overall"]["staked"] == 200
    assert service.get_stats(until="2026-11-07")["overall"]["staked"] == 100
    assert service.get_stats("2026-11-07", "2026-11-07")["overall"]["staked"] == 100
    with pytest.raises(ValueError):
        service.get_stats(since="11/07")


def test_max_drawdown(service):
    db = service.db
    for amount, payout in [(100, 500), (300, 0), (200, 0), (100, 300)]:
        _bet(db, amount=amount, payout=payout, combination=str(amount + payout))
    # 累積: +400 → +100 → -100 → +100。最大下落は 400 − (−100) = 500
    assert service.get_stats()["overall"]["max_drawdown"] == 500


def test_stats_do_not_reveal_config_values(make_service):
    service = make_service(per_race_limit=777, weekly_limit=888, loss_stop_threshold=99900)
    text = repr(service.get_stats())
    for secret in ("777", "888", "99900"):
        assert secret not in text


def test_halted_flag(service):
    service.db.set_halted("test")
    assert service.get_stats()["betting_halted"] is True


# ---------------------------------------------------------------- 投票時の記録


def test_place_bet_records_odds_and_note(service):
    r = service.place_bet(SAT_MAIN, "win", [1], 100, note="  先行力があり展開が向く  ")
    assert r["accepted"]
    expected = service.data.get_odds(SAT_MAIN, __import__("models").BetType.WIN)["1"]
    assert r["odds_at_bet"] == expected
    row = service.db._conn.execute("SELECT odds_at_bet, note FROM bets WHERE id = ?", (r["bet_id"],)).fetchone()
    assert row["odds_at_bet"] == expected and row["note"] == "先行力があり展開が向く"


def test_note_is_truncated_and_optional(service):
    long = "あ" * 600
    r = service.place_bet(SAT_MAIN, "win", [1], 100, note=long)
    r2 = service.place_bet(SAT_MAIN, "win", [2], 100, note="   ")
    r3 = service.place_bet(SAT_MAIN_KYOTO, "win", [1], 100, note=123)
    notes = [row["note"] for row in service.db._conn.execute("SELECT note FROM bets ORDER BY id")]
    assert notes == ["あ" * 500, None, None]
    assert r["accepted"] and r2["accepted"] and r3["accepted"]


def test_odds_snapshot_failure_does_not_block_bet(service, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(service.data, "get_odds", boom)
    r = service.place_bet(SAT_MAIN, "win", [1], 100)
    assert r["accepted"] and r["odds_at_bet"] is None


# ---------------------------------------------------------------- まとめて精算


def test_settle_pending_bets(make_service, clock):
    service = make_service(instant_results=False, weekly_limit=10000, per_race_limit=10000)
    assert service.place_bet(SAT_MAIN, "win", [1], 100)["accepted"]
    assert service.place_bet(SAT_MAIN_KYOTO, "win", [1], 100)["accepted"]
    assert service.place_bet(SUN_MAIN, "win", [1], 100)["accepted"]

    out = service.settle_pending_bets()  # 土曜の朝：どれも未確定
    assert out["settled"] == []
    assert {w["race_id"]: w["status"] for w in out["waiting"]} == {
        SAT_MAIN: "未確定", SAT_MAIN_KYOTO: "未確定", SUN_MAIN: "未確定（開催前）",
    }

    clock.now = datetime(2026, 11, 7, 20, 0, tzinfo=JST)  # 土曜のレース後
    out = service.settle_pending_bets()
    assert {s["race_id"] for s in out["settled"]} == {SAT_MAIN, SAT_MAIN_KYOTO}
    assert [w["race_id"] for w in out["waiting"]] == [SUN_MAIN]
    assert service.db.unsettled_race_ids() == [SUN_MAIN]

    clock.now = datetime(2026, 11, 8, 20, 0, tzinfo=JST)
    out = service.settle_pending_bets()
    assert [s["race_id"] for s in out["settled"]] == [SUN_MAIN]
    assert service.get_stats()["pending"]["bets"] == 0


def test_settlement_marks_refunds(make_service):
    service = make_service()
    _bet(service.db, race_id=SAT_MAIN, combination="1")
    from models import RaceResult
    res = service.data.get_result(SAT_MAIN)
    service.data.get_result = lambda rid: RaceResult(rid, res.order, res.payouts, frozenset({1}))
    service.get_race_result(SAT_MAIN)
    s = service.get_stats()
    assert s["refunded"]["bets"] == 1 and s["overall"]["bets"] == 0


# ---------------------------------------------------------------- 既存 DB の移行


def test_migrates_old_db(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE bets (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, race_id TEXT NOT NULL,
        race_date TEXT, week_start TEXT, bet_type TEXT NOT NULL, combination TEXT NOT NULL, amount INTEGER NOT NULL,
        status TEXT NOT NULL, reason TEXT, receipt TEXT, payout INTEGER, settled_at TEXT);
        INSERT INTO bets (created_at, race_id, race_date, week_start, bet_type, combination, amount, status, payout)
        VALUES ('x', '20261101-TOKYO-11', '2026-11-01', '2026-10-27', 'win', '7', 100, 'accepted', 250);"""
    )
    conn.close()
    db = Database(path)
    from stats import compute_stats
    s = compute_stats(db)
    assert s["overall"]["returned"] == 250 and s["refunded"]["bets"] == 0
    db.close()


# ---------------------------------------------------------------- レポート


def test_report_render(service):
    _bet(service.db, combination="1", amount=300, payout=1260, odds=4.2, note="本命")
    _bet(service.db, combination="2", amount=200)
    text = render(service.get_stats())
    assert "回収率" in text and "420.0%" in text  # 未精算は回収率に含めない
    assert "未精算 1点 200円" in text
    assert "理由: 本命" in text

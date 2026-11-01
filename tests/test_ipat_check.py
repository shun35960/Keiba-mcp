"""ipat-check（本物の即PAT で確認画面まで進めてキャンセルする確認ツール）のテスト。

偽の即PAT サイト（tests/fixtures/ipat/）を実際のブラウザで操作する。本物の即PAT には接続しない。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import admin
from models import JST, BetType
from tests.test_browser_voter import CHROMIUM, SITE, FakeCreds, race
from voter.browser import IpatOptions
from voter.ipat_check import CheckOnlyVoter, make_bet, run_check, sample_horses

needs_browser = pytest.mark.skipif(CHROMIUM is None, reason="Chromium が見つからない")


class SpyCheckVoter(CheckOnlyVoter):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.purchases_seen: list = []

    def _save_debug(self, page, kind):
        self.purchases_seen.append(page.evaluate("() => JSON.parse(sessionStorage.getItem('purchases') || '[]')"))
        return f"/captures/{kind}"


def check_voter(query=""):
    return SpyCheckVoter(FakeCreds(), IpatOptions(
        confirm="external", headless=True, executable_path=CHROMIUM,
        base_url=(SITE / "login1.html").as_uri() + query, step_timeout_ms=5000, debug_dir=Path("/unused"),
    ))


@needs_browser
def test_all_bet_types_reach_confirm_and_never_buy():
    v = check_voter()
    lines: list[str] = []
    results = run_check(v, race(), list(BetType), out=lines.append, sleep=lambda s: None)
    assert [r.ok for r in results] == [True] * 7, [r.message for r in results]
    assert all(p == [] for p in v.purchases_seen)  # 1件も購入していない
    by = {r.bet_type: r for r in results}
    assert by[BetType.TRIFECTA].cart["horses"] == [1, 2, 3]
    assert by[BetType.QUINELLA].cart["total"] == 100
    assert sum(line.startswith("[OK]") for line in lines) == 7
    assert not v._sessions  # 保留（ブラウザ）が残っていない


@needs_browser
def test_failure_is_reported_and_others_continue():
    v = check_voter("?prefill=1")
    results = run_check(v, race(), [BetType.WIN, BetType.PLACE], out=lambda s: None, sleep=lambda s: None)
    assert [r.ok for r in results] == [False, False]
    assert "既に買い目" in results[0].message
    assert all(p == [] for p in v.purchases_seen)


def test_check_voter_cannot_buy():
    v = CheckOnlyVoter(FakeCreds(), IpatOptions())
    bet = make_bet(race(), BetType.WIN, (1,))
    for call in (lambda: v.vote(bet), lambda: v.commit("x"), lambda: v._confirm(None, bet)):
        with pytest.raises(RuntimeError, match="購入しません"):
            call()


def test_check_module_never_presses_ok():
    src = Path("voter/ipat_check.py").read_text(encoding="utf-8")
    assert "dialog_ok" not in src and ".click(" not in src


def test_run_check_requires_check_only_voter():
    from voter.browser import BrowserVoter

    with pytest.raises(TypeError):
        run_check(BrowserVoter(FakeCreds(), IpatOptions()), race(), [BetType.WIN], out=lambda s: None)


def test_sample_horses_skips_scratched():
    r = race()  # 9番は取消
    assert sample_horses(r, BetType.TRIFECTA) == (1, 2, 3)
    assert 9 not in sample_horses(r, BetType.WIN)


# ---------------------------------------------------------------- admin.py ipat-check


class FakeData:
    def __init__(self, r):
        self.r = r

    def get_race(self, rid):
        return self.r if rid == self.r.race_id else None


def _cfg(tmp_path):
    from tests.conftest import make_config

    return make_config(tmp_path)


def test_admin_rejects_unknown_race(tmp_path, capsys):
    assert admin.ipat_check(_cfg(tmp_path), "20261101-TOKYO-10", "all", data=FakeData(race())) == 2
    assert "見つかりません" in capsys.readouterr().out


def test_admin_rejects_closed_race(tmp_path, capsys):
    now = datetime(2026, 11, 1, 15, 39, tzinfo=JST)
    assert admin.ipat_check(_cfg(tmp_path), "20261101-TOKYO-11", "all", data=FakeData(race()), now=now) == 2
    assert "締切済み" in capsys.readouterr().out


def test_admin_rejects_bad_types(tmp_path, capsys):
    assert admin.ipat_check(_cfg(tmp_path), "20261101-TOKYO-11", "win,bracket", data=FakeData(race())) == 2


def test_admin_needs_confirmation(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    now = datetime(2026, 11, 1, 10, 0, tzinfo=JST)
    assert admin.ipat_check(_cfg(tmp_path), "20261101-TOKYO-11", "win", data=FakeData(race()), now=now) == 1
    out = capsys.readouterr().out
    assert "購入はしません" in out and "中止しました" in out


@needs_browser
def test_admin_runs_check(tmp_path, capsys):
    now = datetime(2026, 11, 1, 10, 0, tzinfo=JST)
    v = check_voter()
    code = admin.ipat_check(_cfg(tmp_path), "20261101-TOKYO-11", "win,exacta", assume_yes=True,
                            voter=v, data=FakeData(race()), now=now)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "2/2 券種で確認画面まで進めました（購入はしていません）" in out
    assert all(p == [] for p in v.purchases_seen)

"""即PAT 投票（voter/browser.py）の通しテスト。

本物の即PAT には接続しない。tests/fixtures/ipat/ の偽サイト（架空）を実際のブラウザで操作する。
Chromium が見つからない環境ではスキップする（KEIBA_TEST_CHROMIUM で実行ファイルを指定できる）。
"""

from __future__ import annotations

import glob
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from credentials import CredentialProvider, Secret
from guard import ValidatedBet
from models import JST, BetType, Horse, Race, combination_key, normalize_horses
from voter.base import VoteError
from voter.browser import BrowserVoter, IpatOptions

SITE = Path(__file__).parent / "fixtures" / "ipat"
SECRETS = {"inet_id": "FAKEINET", "subscriber_number": "11112222", "pin": "9999", "pars_number": "5555"}


def _chromium() -> str | None:
    env = os.environ.get("KEIBA_TEST_CHROMIUM")
    if env and Path(env).exists():
        return env
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return found[-1] if found else None


CHROMIUM = _chromium()
pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="Chromium が見つからない")
try:
    import playwright  # noqa: F401
except ImportError:  # playwright 未インストール
    pytestmark = pytest.mark.skip(reason="playwright 未インストール")


class FakeCreds(CredentialProvider):
    def get(self, name):
        return Secret(SECRETS[name])


class SpyVoter(BrowserVoter):
    """完了時・失敗時に偽サイトの状態（購入記録・入力された認証情報）を記録する。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.snapshots: list[dict] = []

    def _save_debug(self, page, kind):
        state = page.evaluate(
            "() => ({purchases: JSON.parse(sessionStorage.getItem('purchases') || '[]'),"
            " creds: ['inetid','i','p','r'].map(k => sessionStorage.getItem(k)), url: location.pathname})"
        )
        self.snapshots.append({"kind": kind, **state})


def race(number=11, course="TOKYO", d=date(2026, 11, 1)):
    return Race(
        race_id=f"{d:%Y%m%d}-{course}-{number:02d}", date=d, course=course, course_name="東京", number=number,
        name="架空賞", surface="芝", distance=2000, post_time=datetime(2026, 11, 1, 15, 40, tzinfo=JST),
        horses=tuple(Horse(number=i, name=f"馬{i}", jockey="騎手", scratched=(i == 9)) for i in range(1, 13)),
    )


def vbet(bet_type=BetType.WIN, horses=(1,), amount=100, r=None):
    h = normalize_horses(bet_type, horses)
    return ValidatedBet(race=r or race(), bet_type=bet_type, horses=h,
                        combination=combination_key(bet_type, h), amount=amount, week_start="2026-10-27")


def voter(query="", **opts):
    defaults = dict(confirm="auto", headless=True, executable_path=CHROMIUM, base_url=(SITE / "login1.html").as_uri() + query,
                    step_timeout_ms=5000, manual_confirm_timeout_sec=10, result_timeout_sec=3, debug_dir=None)
    defaults.update(opts)
    return SpyVoter(FakeCreds(), IpatOptions(**defaults))


# ---------------------------------------------------------------- 成功


@pytest.mark.parametrize(
    "bet_type,horses,expected",
    [
        (BetType.WIN, (3,), [3]),
        (BetType.PLACE, (5,), [5]),
        (BetType.QUINELLA, (7, 2), [2, 7]),
        (BetType.WIDE, (1, 4), [1, 4]),
        (BetType.EXACTA, (7, 2), [7, 2]),
        (BetType.TRIO, (8, 1, 4), [1, 4, 8]),
        (BetType.TRIFECTA, (8, 1, 4), [8, 1, 4]),
    ],
)
def test_auto_purchase_each_bet_type(bet_type, horses, expected):
    v = voter()
    receipt = v.vote(vbet(bet_type, horses, amount=300))
    assert receipt.confirmed and receipt.receipt == "IPAT-0042" and not receipt.dry_run
    (snap,) = v.snapshots
    (purchase,) = snap["purchases"]
    (item,) = purchase
    assert item["course"] == "東京" and item["race"] == 11 and item["unit"] == 3
    assert item["horses"] == expected
    assert snap["creds"] == ["FAKEINET", "11112222", "9999", "5555"]


def test_manual_confirm_human_presses_ok():
    v = voter("?autook=1500", confirm="manual")
    receipt = v.vote(vbet())
    assert receipt.confirmed
    assert len(v.snapshots[-1]["purchases"]) == 1


# ---------------------------------------------------------------- 中止（何も買わない）


def _assert_not_bought(v):
    assert all(s["purchases"] == [] for s in v.snapshots)


def test_manual_confirm_human_cancels():
    v = voter("?autocancel=1000", confirm="manual")
    with pytest.raises(VoteError, match="キャンセル"):
        v.vote(vbet())
    _assert_not_bought(v)


def test_manual_confirm_timeout_cancels():
    v = voter(confirm="manual", manual_confirm_timeout_sec=2)
    with pytest.raises(VoteError, match="時間内に押されなかった"):
        v.vote(vbet())
    _assert_not_bought(v)


def test_abort_when_cart_not_empty():
    v = voter("?prefill=1")
    with pytest.raises(VoteError, match="既に買い目"):
        v.vote(vbet())
    _assert_not_bought(v)


def test_abort_when_cart_amount_differs():
    v = voter("?wrongunit=1")
    with pytest.raises(VoteError, match="金額"):
        v.vote(vbet(amount=200))
    _assert_not_bought(v)


def test_abort_on_unknown_course():
    v = voter()
    with pytest.raises(VoteError, match="中山"):
        v.vote(vbet(r=race(course="NAKAYAMA")))
    _assert_not_bought(v)


def test_abort_on_wrong_weekday():
    # 偽サイトは日曜開催。土曜のレースとして投票しようとしたら中止
    v = voter()
    with pytest.raises(VoteError, match="（土）"):
        v.vote(vbet(r=race(d=date(2026, 10, 31))))
    _assert_not_bought(v)


def test_abort_on_post_time_mismatch():
    r = race()
    r = Race(**{**r.__dict__, "post_time": datetime(2026, 11, 1, 12, 0, tzinfo=JST)})
    v = voter()
    with pytest.raises(VoteError, match="時刻"):
        v.vote(vbet(r=r))
    _assert_not_bought(v)


def test_abort_on_scratched_horse():
    v = voter()
    with pytest.raises(VoteError, match="選べません"):
        v.vote(vbet(horses=(9,)))
    _assert_not_bought(v)


@pytest.mark.parametrize(
    "bet_type,horses,expected",
    [(BetType.WIN, (3,), [3]), (BetType.TRIO, (8, 1, 4), [1, 4, 8]), (BetType.TRIFECTA, (8, 1, 4), [8, 1, 4])],
)
def test_waits_for_horse_rows_rendered_late(bet_type, horses, expected):
    # 本物の即PAT では出馬表の行が券種の部品より遅れて描画される（2026-11-01 の ipat-check で確認）
    v = voter("?horsedelay=1500")
    receipt = v.vote(vbet(bet_type, horses))
    assert receipt.confirmed
    (snap,) = v.snapshots
    assert snap["purchases"][0][0]["horses"] == expected


@pytest.mark.parametrize(
    "query", ["?covered=1", "?loading=1500", "?horsedelay=500&loading=1500&covered=1"],
)
@pytest.mark.parametrize(
    "bet_type,horses,expected",
    [(BetType.QUINELLA, (7, 2), [2, 7]), (BetType.TRIFECTA, (8, 1, 4), [8, 1, 4])],
)
def test_ticks_horse_despite_real_site_obstacles(query, bet_type, horses, expected):
    # 本物の即PAT では、ラベルの押下がセルやナビバーに遮られ、読み込み中の表示が消えると行が描き直される
    # （2026-11-01 の ipat-check で確認）
    v = voter(query)
    receipt = v.vote(vbet(bet_type, horses))
    assert receipt.confirmed
    (snap,) = v.snapshots
    assert snap["purchases"][0][0]["horses"] == expected


def test_abort_when_horse_rows_never_appear():
    v = voter(step_timeout_ms=1500)
    with pytest.raises(VoteError, match="馬番の選択欄（no13）が見つかりませんでした"):
        v.vote(vbet(horses=(13,)))
    _assert_not_bought(v)


def test_abort_on_notice_screen():
    v = voter("?notice=1")
    with pytest.raises(VoteError, match="約定"):
        v.vote(vbet())


def test_error_messages_never_contain_credentials():
    for q in ("?prefill=1", "?wrongunit=1", "?notice=1"):
        with pytest.raises(VoteError) as e:
            voter(q).vote(vbet(amount=200))
        for secret in SECRETS.values():
            assert secret not in str(e.value)


# ---------------------------------------------------------------- 送信後に結果が分からない


def test_unconfirmed_after_submit_does_not_raise():
    v = voter("?noticket=1")
    receipt = v.vote(vbet())
    assert receipt.confirmed is False and receipt.receipt == "UNCONFIRMED"
    assert len(v.snapshots[-1]["purchases"]) == 1  # 実際には送信されている


# ---------------------------------------------------------------- 二段階投票（confirm = "external"）


def test_external_prepare_then_commit():
    v = voter(confirm="external")
    prepared = v.prepare(vbet(BetType.EXACTA, (7, 2), amount=200), "pid-1")
    assert prepared.cart["horses"] == [7, 2] and prepared.cart["total"] == 200
    assert prepared.cart["course"] == "東京" and prepared.cart["race_number"] == 11
    assert v.snapshots[-1]["kind"] == "confirm" and v.snapshots[-1]["purchases"] == []  # まだ買っていない
    assert v.has_pending("pid-1")
    receipt = v.commit("pid-1")
    assert receipt.confirmed and receipt.receipt == "IPAT-0042"
    assert len(v.snapshots[-1]["purchases"]) == 1
    assert not v.has_pending("pid-1")


def test_external_prepare_then_abort():
    v = voter(confirm="external")
    v.prepare(vbet(), "pid-2")
    v.abort("pid-2")
    assert not v.has_pending("pid-2")
    assert all(s["purchases"] == [] for s in v.snapshots)
    with pytest.raises(VoteError, match="残っていません"):
        v.commit("pid-2")


def test_external_only_one_session():
    v = voter(confirm="external")
    v.prepare(vbet(), "pid-3")
    try:
        with pytest.raises(VoteError, match="保留中"):
            v.prepare(vbet(horses=(2,)), "pid-4")
    finally:
        v.abort("pid-3")


def test_external_prepare_failure_buys_nothing():
    v = voter("?prefill=1", confirm="external")
    with pytest.raises(VoteError, match="既に買い目"):
        v.prepare(vbet(), "pid-5")
    assert not v.has_pending("pid-5")


def test_external_vote_is_not_used():
    with pytest.raises(VoteError):
        voter(confirm="external").vote(vbet())


def test_external_end_to_end_through_service(make_service, clock):
    """サービス＋ブラウザ（偽サイト）で prepare → confirm_vote まで通す。"""
    v = voter(confirm="external")
    service = make_service(voter=v, weekly_limit=10000, per_race_limit=10000)
    # 偽サイトは 2026-11-01（日）の東京11R
    from datetime import datetime as _dt

    clock.now = _dt(2026, 11, 1, 10, 0, tzinfo=JST)
    service.data.get_race = lambda rid: race() if rid == "20261101-TOKYO-11" else None
    r = service.place_bet("20261101-TOKYO-11", "quinella", [7, 2], 300)
    assert r["status"] == "pending_confirmation", r
    assert r["confirmation_screen"]["horses"] == [2, 7]
    c = service.confirm_vote(r["pending_id"])
    assert c["confirmed"] is True and c["receipt"] == "IPAT-0042"


def test_unexpected_error_message_names_the_step(monkeypatch):
    """想定外のエラーでは、どの手順で止まったかをメッセージに入れる（画面の変化を調べやすくする）。"""
    v = voter()

    def boom(page, bet):
        v._step = "場・レースの選択"
        raise TimeoutError("simulated")

    monkeypatch.setattr(v, "_select_race", boom)
    with pytest.raises(VoteError, match="「場・レースの選択」で想定外のエラー"):
        v.vote(vbet())

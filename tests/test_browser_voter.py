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
except ImportError:  # capture グループ未インストール
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

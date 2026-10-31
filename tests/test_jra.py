"""JRA 公式サイトからの取得（パーサー・間隔制限・キャッシュ・データソース）のテスト。

ネットワークには出ない。tests/fixtures/jra/ の架空データの見本 HTML を使う。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from data import jra_parse as P
from data.scraper import FetchError, JraDataSource, JraFetcher
from models import JST, BetType
from server import KeibaService
from voter.dry_run import DryRunVoter

FIX = Path(__file__).parent / "fixtures" / "jra"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# cname -> 見本ファイル
PAGES = {
    "pw01dli00/F3": "card_index.html",
    "pw01sli00/AF": "result_index.html",
    "pw01drl00052026050120261107/AA": "race_list_tokyo.html",
    "pw01srl00052026050120261107/B2": "result_list_tokyo.html",
    "pw01des01052026050120261107/AD": "des_tokyo.html",
    "pw01dde0105202605011120261107/AF": "dde_tokyo_11.html",
    "pw151ouS305202605011120261107Z/B4": "odds151_11.html",
    "pw154ouS305202605011120261107Z/C4": "odds154_11.html",
    "pw155ouS305202605011120261107Z/C5": "odds155_11.html",
    "pw156ouS305202605011120261107Z/C6": "odds156_11.html",
    "pw157ouS305202605011120261107Z99/C7": "odds157_11.html",
    "pw158ouS305202605011120261107Z/C8": "odds158_11.html",
    "pw01sde0105202605011120261107/B3": "result_11.html",
}

RACE_11 = "20261107-TOKYO-11"


# ---------------------------------------------------------------- パーサー


def test_parse_meeting_links():
    links = P.parse_meeting_links(fixture("card_index.html"))
    assert {(m.course, m.date) for m in links} == {
        ("TOKYO", date(2026, 11, 7)),
        ("KYOTO", date(2026, 11, 7)),
        ("TOKYO", date(2026, 11, 8)),
    }
    results = P.parse_meeting_links(fixture("result_index.html"))
    assert {(m.course, m.date) for m in results} == {("TOKYO", date(2026, 11, 7)), ("TOKYO", date(2026, 10, 25))}
    assert all(m.path == "/JRADB/accessS.html" for m in results)


def test_parse_race_links():
    links = P.parse_race_links(fixture("race_list_tokyo.html"))
    assert links[10]["result"] == "pw01sde0105202605011020261107/B1"
    assert links[11]["card"] == "pw01dde0105202605011120261107/AF"
    assert links[11]["odds"] == "pw151ouS305202605011120261107Z/B4"
    assert "result" not in links[11]  # 未確定のレースには結果リンクがない


def test_parse_compact_cards():
    races = P.parse_race_cards(fixture("des_tokyo.html"))
    assert [r.number for r in races] == [10, 11]
    r = races[1]
    assert r.post_time == datetime(2026, 11, 7, 15, 40, tzinfo=JST)
    assert (r.name, r.surface, r.distance, r.course_name) == ("架空ステークス", "ダート", 1600, "東京")
    assert r.race_class == "3歳以上 オープン （国際） ハンデ"
    assert [h.number for h in r.horses] == [1, 2, 3, 4]
    h1 = r.horses[0]
    assert (h1.name, h1.jockey, h1.trainer, h1.sex_age, h1.weight) == ("カクウアルファ", "架空 三郎", "架空 厩舎", "牡5", 57.0)
    assert (h1.horse_weight, h1.horse_weight_diff, h1.win_odds, h1.frame) == (500, 0, 3.0, 1)
    scratched = r.horses[3]
    assert scratched.scratched and scratched.win_odds is None and scratched.name == "カクウデルタ"
    assert races[0].surface == "芝"


def test_parse_detailed_card():
    (r,) = P.parse_race_cards(fixture("dde_tokyo_11.html"))
    h = r.horses[0]
    assert (h.popularity, h.win_odds, h.sex_age, h.weight, h.jockey) == (1, 3.0, "牡5", 57.0, "架空 三郎")
    assert (h.sire, h.dam, h.dam_sire, h.trainer) == ("カクウチチ", "カクウハハ", "カクウソフ", "架空 厩舎")
    assert len(h.past_races) == 1  # 空の近走欄は無視
    p = h.past_races[0]
    assert (p.course, p.race_name, p.place, p.field_size, p.popularity) == ("中山", "前走特別", "2着", 14, 2)
    assert (p.distance, p.time, p.condition, p.horse_weight, p.last_3f) == ("1800ダ", "1:51.0", "良", 500, "36.9")
    assert p.corners == (4, 4, 3, 2)
    # 取消の馬は馬番の代わりに「取消」と表示されるので、並び順（直前の馬番＋1）で馬番を決める
    assert [(x.number, x.scratched) for x in r.horses] == [(1, False), (2, False), (3, False), (4, True)]


def test_parse_win_place_odds():
    odds = P.parse_win_place_odds(fixture("odds151_11.html"))
    assert odds[BetType.WIN] == {"1": 3.0, "2": 4.2, "3": 1234.5}
    assert odds[BetType.PLACE] == {"1": 1.2, "2": 1.4, "3": 99.9}  # 下限値


def test_parse_odds_links_skips_axis_pages_and_other_races():
    links = P.parse_odds_links(fixture("odds151_11.html"), 11)
    assert links["7"] == "pw157ouS305202605011120261107Z99/C7"
    assert set(links) == {"3", "4", "5", "6", "7", "8"}


def test_parse_combination_odds():
    assert P.parse_odds(fixture("odds154_11.html"), BetType.QUINELLA) == {"1-2": 5.6, "1-3": 12.0, "2-3": 15.5}
    assert P.parse_odds(fixture("odds155_11.html"), BetType.WIDE) == {"1-2": 2.1, "1-3": 4.0, "2-3": 5.0}
    assert P.parse_odds(fixture("odds156_11.html"), BetType.EXACTA) == {"1>2": 9.9, "1>3": 20.1, "2>1": 11.3, "2>3": 30.0}
    assert P.parse_odds(fixture("odds157_11.html"), BetType.TRIO) == {"1-2-3": 18.8}
    assert P.parse_odds(fixture("odds158_11.html"), BetType.TRIFECTA) == {"1>2>3": 45.6, "2>1>3": 52.3}


def test_parse_result():
    res = P.parse_result(fixture("result_11.html"))
    assert res.order == [2, 3, 1]
    assert res.refunded_horses == {4}
    assert "wakuren" not in res.payouts
    assert res.payouts["win"] == {"2": 420}
    assert res.payouts["place"] == {"2": 150, "3": 300}
    assert res.payouts["quinella"] == {"2-3": 1550}
    assert res.payouts["wide"] == {"2-3": 500, "1-2": 210}
    assert res.payouts["exacta"] == {"2>3": 3000}
    assert res.payouts["trio"] == {"1-2-3": 1880}
    assert res.payouts["trifecta"] == {"2>3>1": 12340}


def test_parse_result_without_payouts_is_none():
    assert P.parse_result("<html><table><tr><td class='place'>1</td><td class='num'>2</td></tr></table></html>") is None


# ---------------------------------------------------------------- 取得（間隔制限・キャッシュ）


class FakeClock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_800_000_000.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def sleep(self, s):
        self.sleeps.append(s)
        self.mono += s
        self.wall += s


def make_fetcher(tmp_path, clock, handler=None, interval=3.0):
    requests: list[str] = []

    def default_handler(request: httpx.Request) -> httpx.Response:
        cname = dict(httpx.QueryParams(request.content.decode()))["cname"]
        requests.append(cname)
        name = PAGES.get(cname)
        if name is None:
            return httpx.Response(404)
        # 本物と同じく Shift_JIS（charset 指定なし）で返す
        return httpx.Response(200, content=fixture(name).encode("cp932"), headers={"content-type": "text/html"})

    f = JraFetcher(
        tmp_path / "cache.sqlite",
        min_interval=interval,
        transport=httpx.MockTransport(handler or default_handler),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        wall_clock=clock.time,
    )
    return f, requests


def test_fetcher_waits_between_requests(tmp_path):
    clock = FakeClock()
    f, requests = make_fetcher(tmp_path, clock)
    f.get("/JRADB/accessD.html", "pw01dli00/F3", ttl=600)
    f.get("/JRADB/accessS.html", "pw01sli00/AF", ttl=600)
    clock.mono += 1.0
    f.get("/JRADB/accessD.html", "pw01des01052026050120261107/AD", ttl=600)
    assert requests == ["pw01dli00/F3", "pw01sli00/AF", "pw01des01052026050120261107/AD"]
    assert clock.sleeps == [3.0, 2.0]


def test_fetcher_uses_cache_within_ttl(tmp_path):
    clock = FakeClock()
    f, requests = make_fetcher(tmp_path, clock)
    a = f.get("/JRADB/accessD.html", "pw01dli00/F3", ttl=600)
    clock.wall += 599
    b = f.get("/JRADB/accessD.html", "pw01dli00/F3", ttl=600)
    assert a == b and len(requests) == 1
    clock.wall += 2
    f.get("/JRADB/accessD.html", "pw01dli00/F3", ttl=600)
    assert len(requests) == 2


def test_fetcher_cache_survives_restart(tmp_path):
    clock = FakeClock()
    f, requests = make_fetcher(tmp_path, clock)
    f.get("/JRADB/accessD.html", "pw01dli00/F3", ttl=600)
    f.close()
    f2, requests2 = make_fetcher(tmp_path, clock)
    f2.get("/JRADB/accessD.html", "pw01dli00/F3", ttl=600)
    assert requests2 == []


def test_fetcher_decodes_shift_jis(tmp_path):
    f, _ = make_fetcher(tmp_path, FakeClock())
    assert "出馬表" in f.get("/JRADB/accessD.html", "pw01dli00/F3", ttl=600)


def test_fetcher_raises_fetch_error_and_does_not_cache(tmp_path):
    clock = FakeClock()
    f, requests = make_fetcher(tmp_path, clock)
    with pytest.raises(FetchError):
        f.get("/JRADB/accessD.html", "unknown/00", ttl=600)
    with pytest.raises(FetchError):
        f.get("/JRADB/accessD.html", "unknown/00", ttl=600)
    assert len(requests) == 2


def test_fetcher_network_error(tmp_path):
    def boom(request):
        raise httpx.ConnectError("no route")

    f, _ = make_fetcher(tmp_path, FakeClock(), handler=boom)
    with pytest.raises(FetchError):
        f.get("/JRADB/accessD.html", "pw01dli00/F3", ttl=600)


# ---------------------------------------------------------------- データソース


@pytest.fixture
def jra(tmp_path):
    f, requests = make_fetcher(tmp_path, FakeClock())
    ds = JraDataSource(f)
    ds.requests = requests  # type: ignore[attr-defined]
    yield ds
    f.close()


def test_get_races(jra):
    races = jra.get_races(date(2026, 11, 7), "東京")
    assert [r.race_id for r in races] == ["20261107-TOKYO-10", RACE_11]
    assert races[1].course_name == "東京"
    assert jra.get_races(date(2026, 11, 7), "NAKAYAMA") == []
    assert jra.get_races(date(2026, 11, 14)) == []


def test_get_race_detailed(jra):
    race = jra.get_race(RACE_11)
    assert race.race_id == RACE_11 and race.number == 11
    assert race.horses[0].past_races
    assert jra.get_race("20261107-TOKYO-12") is None
    assert jra.get_race("20261107-NAKAYAMA-11") is None
    assert jra.get_race("bad") is None


def test_get_odds_all_types(jra):
    assert jra.get_odds(RACE_11, BetType.WIN)["1"] == 3.0
    assert jra.get_odds(RACE_11, BetType.TRIO) == {"1-2-3": 18.8}
    assert jra.get_odds(RACE_11, BetType.TRIFECTA)["2>1>3"] == 52.3
    assert jra.get_odds("20261114-TOKYO-11", BetType.WIN) == {}  # 出馬表の開催一覧に無い日


def test_get_result(jra):
    res = jra.get_result(RACE_11)
    assert res.order == (2, 3, 1)
    assert res.refunded_horses == frozenset({4})
    assert res.payout_per_100(BetType.TRIFECTA, [2, 3, 1]) == 12340
    assert jra.get_result("20261108-TOKYO-11") is None  # 結果一覧に無い開催


def test_repeated_calls_use_cache(jra):
    jra.get_race(RACE_11)
    n = len(jra.requests)
    jra.get_race(RACE_11)
    jra.get_odds(RACE_11, BetType.WIN)
    jra.get_odds(RACE_11, BetType.WIN)
    assert len(jra.requests) == n + 1  # 単勝オッズの1回だけ


# ---------------------------------------------------------------- サービス経由（返還を含む精算）


def test_service_with_jra_settles_and_refunds(tmp_path, jra):
    from tests.conftest import make_config
    from db import Database

    now = {"t": datetime(2026, 11, 7, 12, 0, tzinfo=JST)}
    cfg = make_config(tmp_path, per_race_limit=10000, weekly_limit=10000)
    svc = KeibaService(config=cfg, data=jra, voter=DryRunVoter(), db=Database(cfg.db_path), clock=lambda: now["t"])

    card = svc.get_race_card("2026-11-07", "TOKYO", 11)
    assert card["races"][0]["horses"][0]["past_races"][0]["race_name"] == "前走特別"

    assert svc.place_bet(RACE_11, "win", [2], 300)["accepted"]
    assert svc.place_bet(RACE_11, "quinella", [2, 3], 200)["accepted"]
    assert svc.place_bet(RACE_11, "place", [1], 100)["accepted"]
    # 取消の馬（4番）は出走しないので投票できない
    assert not svc.place_bet(RACE_11, "win", [4], 100)["accepted"]

    now["t"] = datetime(2026, 11, 7, 16, 30, tzinfo=JST)
    out = svc.get_race_result(RACE_11)
    assert out["status"] == "確定"
    by = {b["bet_type"]: b for b in out["my_bets"]}
    assert by["win"]["payout"] == 1260 and by["win"]["hit"]
    assert by["quinella"]["payout"] == 3100
    assert by["place"]["payout"] == 0  # 3着の1番は払戻一覧に無い（見本では複勝2頭分のみ）
    assert out["refunded_horses"] == [4]
    svc.db.close()


def test_refund_for_scratched_horse_after_bet(tmp_path, jra):
    """投票後に出走取消になった馬を含む買い目は返還される。"""
    from tests.conftest import make_config
    from db import Database

    cfg = make_config(tmp_path, per_race_limit=10000, weekly_limit=10000)
    svc = KeibaService(
        config=cfg, data=jra, voter=DryRunVoter(), db=Database(cfg.db_path),
        clock=lambda: datetime(2026, 11, 7, 16, 30, tzinfo=JST),
    )
    # 投票時は出走予定だった想定で直接記録
    svc.db.insert_bet(
        race_id=RACE_11, race_date="2026-11-07", week_start="2026-11-03", bet_type="wide",
        combination="2-4", amount=500, status="accepted", receipt="DRY-x",
    )
    out = svc.get_race_result(RACE_11)
    (b,) = out["my_bets"]
    assert b["payout"] == 500 and b["refunded"] and not b["hit"]
    assert out["race_summary"]["profit"] == 0
    svc.db.close()

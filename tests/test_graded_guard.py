"""投票対象の拡大（11R ＋ 重賞）のテスト。race.grade は JRA のレース一覧の grade_icon から決まる。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest

from config import RACE_SCOPE_GRADED, RACE_SCOPE_MAIN, load_config
from data import jra_parse as P
from data.scraper import JraDataSource, JraFetcher
from db import Database
from models import GRADED_STAKES, GUARD_GRADES, JST
from server import KeibaService, _race_summary
from tests.conftest import Clock, make_config
from voter.dry_run import DryRunVoter

FIX = Path(__file__).parent / "fixtures" / "jra"
SAT_MORNING = datetime(2026, 11, 7, 9, 0, tzinfo=JST)

PAGES = {
    "pw01dli00/F3": "card_index.html",
    "pw01drl00082026040120261107/AB": "race_list_kyoto.html",
    "pw01dde0108202604010720261107/A7": "dde_kyoto_07.html",
    "pw01dde0108202604010820261107/A8": "dde_kyoto_08.html",
    "pw01dde0108202604010920261107/A9": "dde_kyoto_09.html",
    "pw01dde0108202604011020261107/AA": "dde_kyoto_10.html",
    "pw01dde0108202604011120261107/AB": "dde_kyoto_11.html",
}


def jra_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        cname = dict(httpx.QueryParams(request.content.decode()))["cname"]
        if cname not in PAGES:
            return httpx.Response(404)
        return httpx.Response(200, content=(FIX / PAGES[cname]).read_bytes(), headers={"content-type": "text/html; charset=utf-8"})

    return httpx.MockTransport(handler)


def jra_source(tmp_path) -> JraDataSource:
    return JraDataSource(
        JraFetcher(tmp_path / "jra_cache.sqlite", transport=jra_transport(), sleep=lambda s: None)
    )


@pytest.fixture
def make_jra_service(tmp_path):
    created = []

    def _make(**guard_overrides) -> KeibaService:
        cfg = make_config(tmp_path, **guard_overrides)
        svc = KeibaService(
            config=cfg, data=jra_source(tmp_path), voter=DryRunVoter(), db=Database(cfg.db_path), clock=Clock(SAT_MORNING)
        )
        created.append(svc)
        return svc

    yield _make
    for s in created:
        s.db.close()
        s.data.f.close()


def bet(service, number):
    return service.place_bet(f"20261107-KYOTO-{number:02d}", "win", [1], 100)


# ---------------------------------------------------------------- 格の判定


@pytest.mark.parametrize(
    "alt,grade",
    [
        ("GⅠ", "G1"),
        ("GⅡ", "G2"),
        ("GⅢ", "G3"),
        ("J･GⅠ", "JG1"),  # JRA の実際の表記（半角の中黒）
        ("J･GⅡ", "JG2"),
        ("J･GⅢ", "JG3"),
        ("J・GⅢ", "JG3"),
        ("ＧⅠ", "G1"),
        ("G1", "G1"),
        ("リステッド", "L"),
        ("L", "L"),
        ("OP", ""),
        ("オープン", ""),
        ("GⅣ", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_grade(alt, grade):
    assert P.normalize_grade(alt) == grade


def test_listed_passes_guard_but_is_not_graded_stakes():
    assert "L" in GUARD_GRADES and "L" not in GRADED_STAKES


def test_parse_race_links_reads_grade_per_row():
    links = P.parse_race_links((FIX / "race_list_kyoto.html").read_text(encoding="utf-8"))
    assert links[8]["grade"] == "JG3"
    assert links[9]["grade"] == "L"
    assert links[8]["card"] == "pw01dde0108202604010820261107/A8"
    assert "grade" not in links[7]
    assert "grade" not in links[10]  # 知らない表記は格なし
    assert "grade" not in links[11]


def test_get_race_has_grade_from_race_list(tmp_path):
    src = jra_source(tmp_path)
    assert src.get_race("20261107-KYOTO-08").grade == "JG3"
    assert src.get_race("20261107-KYOTO-07").grade == ""
    src.f.close()


def test_race_summary_shows_grade_only_when_present(tmp_path):
    src = jra_source(tmp_path)
    assert _race_summary(src.get_race("20261107-KYOTO-08"), with_horses=False)["grade"] == "JG3"
    assert "grade" not in _race_summary(src.get_race("20261107-KYOTO-07"), with_horses=False)
    src.f.close()


# ---------------------------------------------------------------- 既定（11R のみ）


def test_default_scope_is_main_only(make_jra_service):
    service = make_jra_service()
    assert service.config.guard.race_scope == RACE_SCOPE_MAIN
    r = bet(service, 8)  # 重賞でも既定では不可
    assert r["accepted"] is False
    assert r["reason"] == "8R は投票対象外です。投票できるのは 11R だけです。"
    assert bet(service, 11)["accepted"] is True


# ---------------------------------------------------------------- 重賞まで広げた設定


def test_graded_8r_passes(make_jra_service):
    assert bet(make_jra_service(race_scope=RACE_SCOPE_GRADED), 8)["accepted"] is True


def test_listed_9r_passes(make_jra_service):
    assert bet(make_jra_service(race_scope=RACE_SCOPE_GRADED), 9)["accepted"] is True


def test_non_graded_race_rejected(make_jra_service):
    r = bet(make_jra_service(race_scope=RACE_SCOPE_GRADED), 7)
    assert r["accepted"] is False
    assert "対象外" in r["reason"] and "重賞と確認できません" in r["reason"]


def test_unknown_grade_rejected(make_jra_service):
    assert bet(make_jra_service(race_scope=RACE_SCOPE_GRADED), 10)["accepted"] is False


def test_11r_still_allowed_without_grade(make_jra_service):
    assert bet(make_jra_service(race_scope=RACE_SCOPE_GRADED), 11)["accepted"] is True


def test_dummy_data_has_no_grade_so_only_11r(make_service):
    service = make_service(race_scope=RACE_SCOPE_GRADED)
    assert service.place_bet("20261107-TOKYO-08", "win", [1], 100)["accepted"] is False


def test_money_limits_apply_to_graded_race(make_jra_service):
    service = make_jra_service(race_scope=RACE_SCOPE_GRADED, per_race_limit=500, weekly_limit=1000)
    assert service.place_bet("20261107-KYOTO-08", "win", [1], 600)["accepted"] is False
    assert service.place_bet("20261107-KYOTO-08", "win", [1], 500)["accepted"] is True
    r = service.place_bet("20261107-KYOTO-08", "place", [1], 100)
    assert r["accepted"] is False and "1レース" in r["reason"]
    assert service.place_bet("20261107-KYOTO-09", "win", [1], 500)["accepted"] is True
    r = service.place_bet("20261107-KYOTO-11", "win", [1], 100)
    assert r["accepted"] is False and "今週" in r["reason"]


def test_rejection_has_no_limit_values(make_jra_service):
    r = bet(make_jra_service(race_scope=RACE_SCOPE_GRADED, per_race_limit=777), 7)
    assert "777" not in r["reason"]


def test_deadline_applies_to_graded_race(make_jra_service):
    service = make_jra_service(race_scope=RACE_SCOPE_GRADED)
    service.clock.now = datetime(2026, 11, 7, 14, 3, tzinfo=JST)  # 8R は 14:05 発走
    r = bet(service, 8)
    assert r["accepted"] is False and "締切" in r["reason"]


# ---------------------------------------------------------------- 設定の読み込み


def write_config(tmp_path, extra=""):
    p = tmp_path / "c.toml"
    p.write_text(
        "[guard]\nper_race_limit = 500\nweekly_limit = 1000\nloss_stop_threshold = 3000\nbet_unit = 100\n"
        f"allowed_race_numbers = [11]\n{extra}\n",
        encoding="utf-8",
    )
    return p


def test_config_default_scope(tmp_path):
    assert load_config(write_config(tmp_path)).guard.race_scope == RACE_SCOPE_MAIN


def test_config_graded_scope(tmp_path):
    cfg = load_config(write_config(tmp_path, 'race_scope = "11r_and_graded"'))
    assert cfg.guard.race_scope == RACE_SCOPE_GRADED


def test_config_rejects_unknown_scope(tmp_path):
    with pytest.raises(ValueError, match="race_scope"):
        load_config(write_config(tmp_path, 'race_scope = "all"'))

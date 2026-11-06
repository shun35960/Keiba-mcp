"""補助データ（netkeiba 取り込み・get_race_insights）のテスト。

ネットワークには出ない。tests/fixtures/netkeiba/ の架空データと httpx.MockTransport を使う。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx
import pytest

import admin
from config import load_config
from credentials import BitwardenCredentialProvider, CredentialError, EnvCredentialProvider, Secret
from data import netkeiba_parse as P
from data.netkeiba import COMMENT_URL, LOGIN_URL, TRAINING_URL, NetkeibaError, NetkeibaFetcher, fetch_race_insights
from data.supplement import (
    MAX_COMMENT_LENGTH,
    FileSupplementSource,
    HorseInsight,
    InterimWorkout,
    RaceInsights,
    SupplementError,
    SupplementSource,
    clean_text,
    save_insights,
)
from db import Database
from models import JST, Horse, Race
from server import KeibaService, build_service, create_server
from tests.conftest import SAT_MAIN, Clock, make_config, runners
from tests.test_graded_guard import SAT_MORNING, jra_source
from tests.test_two_phase import FakeTwoPhase
from voter.dry_run import DryRunVoter

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures" / "netkeiba"
RACE = "20261107-KYOTO-08"
NK_ID = "202608040108"
USER, PASSWORD = "FAKE-USER-ID", "FAKE-PASS-word"


def page(name: str) -> bytes:
    text = (FIX / name).read_text(encoding="utf-8")
    return text.encode("euc_jp" if 'charset="EUC-JP"' in text else "utf-8")


def fake_race() -> Race:
    names = ["カクウハードル", "カクウセカンド", "カクウバンケン"]
    return Race(
        race_id=RACE,
        date=SAT_MORNING.date(),
        course="KYOTO",
        course_name="京都",
        number=8,
        name="架空ジャンプステークス",
        surface="芝",
        distance=3170,
        post_time=datetime(2026, 11, 7, 14, 5, tzinfo=JST),
        horses=tuple(Horse(number=i, name=n, jockey="架空") for i, n in enumerate(names, 1)),
        grade="JG3",
    )


class Site:
    """偽の netkeiba。リクエストを記録する。"""

    def __init__(self, *, training=None, comment=None, login_ok=True, page_status=200):
        self.requests: list[httpx.Request] = []
        self.training = page("training.html") if training is None else training
        self.comment = page("comment.html") if comment is None else comment
        self.login_ok = login_ok
        self.page_status = page_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        html = {"content-type": "text/html"}
        url = str(request.url)
        if request.method == "POST":
            if self.login_ok:
                return httpx.Response(302, headers={"location": "https://www.netkeiba.com/"})
            return httpx.Response(200, content=page("login.html"), headers=html)
        if url == LOGIN_URL:
            return httpx.Response(200, content=page("login.html"), headers=html)
        if self.page_status != 200:
            return httpx.Response(self.page_status)
        body = self.training if "oikiri" in url else self.comment
        return httpx.Response(200, content=body, headers=html)

    def pages(self) -> list[str]:
        return [str(r.url) for r in self.requests if r.method == "GET" and str(r.url) != LOGIN_URL]


class Wall:
    def __init__(self):
        self.now = SAT_MORNING.timestamp()
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def creds():
    return Secret(USER), Secret(PASSWORD)


def make_fetcher(tmp_path, site, wall, credentials=creds, **kw):
    return NetkeibaFetcher(
        tmp_path / "nk.sqlite",
        credentials,
        transport=httpx.MockTransport(site),
        sleep=wall.sleep,
        wall_clock=wall,
        **kw,
    )


# ---------------------------------------------------------------- race_id の変換


def test_netkeiba_race_id_from_cname():
    # 設計メモの実例（2026-11-01 東京11R）
    assert P.netkeiba_race_id("pw01dde0105202604111120261101/A6", "20261101-TOKYO-11") == "202605041111"


@pytest.mark.parametrize(
    "cname,race_id",
    [
        ("pw01dde0105202604111120261101/A6", "20261101-KYOTO-11"),  # 場が違う
        ("pw01dde0105202604111120261101/A6", "20261101-TOKYO-10"),  # レースが違う
        ("pw01dde0105202604111120261101/A6", "20261102-TOKYO-11"),  # 日付が違う
        ("", "20261101-TOKYO-11"),
    ],
)
def test_netkeiba_race_id_rejects_mismatch(cname, race_id):
    with pytest.raises(ValueError):
        P.netkeiba_race_id(cname, race_id)


# ---------------------------------------------------------------- 解析


def test_parse_training():
    rows = P.parse_training(page("training.html").decode("utf-8"))
    assert [r.number for r in rows] == [1, 2]  # 馬番のない馬は2行とも読まない
    r = rows[0]
    assert (r.name, r.grade, r.note) == ("カクウハードル", "A", "架空の短評その一")
    assert r.final_time == "栗Ｗ 80.1-65.2-50.3-36.4-11.5 馬也"  # 括弧のラップは入れない
    assert rows[1].final_time == "美坂 53.1-38.2-25.3-12.4 強め"  # 計測のない区間（-）は入れない
    assert [t.interim_count for t in rows] == [5, 2]  # 最終追い切りを含む本数


def test_parse_training_highlights_strongest_interims():
    r = P.parse_training(page("training.html").decode("utf-8"))[0]
    assert r.interim_highlights == (
        # 印の点数が最も高い（オレンジ2点＋黄色1点）。行の順番は2番目
        P.ParsedInterim("2026/10/21(水)", "栗Ｗ 一番時計 81.1-66.2-51.3-37.4-11.6 一杯", "架空の中間その一"),
        # 同じ2点なら、古くても評価が高い方が先
        P.ParsedInterim("2026/10/14(水)", "栗坂 53.6-38.7-25.8-12.9 強め", "架空の中間その三"),
        P.ParsedInterim("2026/10/17(土)", "栗坂 53.1-38.2-25.3-12.5 馬也", "架空の中間その二"),
    )  # 評価が A でも速い時計の印がなければ入らない。最終追い切りも入らない


def test_parse_training_ties_keep_newest_first():
    html = page("training.html").decode("utf-8").replace("Rank_C", "Rank_B").replace(">C</td>", ">B</td>")
    dates = [w.date for w in P.parse_training(html)[0].interim_highlights]
    assert dates == ["2026/10/21(水)", "2026/10/17(土)", "2026/10/14(水)"]


def test_parse_training_page_without_all_workouts_is_unknown_not_zero():
    """最終追切のみの表（type=2 など）が返っても、本数を決めつけない。最終追い切りは読む。"""
    html = page("training.html").decode("utf-8").replace(" Oikiri_Between", "")
    rows = P.parse_training(html)
    assert [(t.interim_count, t.interim_highlights) for t in rows] == [(None, ()), (None, ())]
    assert (rows[0].grade, rows[0].note) == ("A", "架空の短評その一")


def test_training_url_is_the_all_workouts_page():
    assert TRAINING_URL.format(NK_ID).endswith(f"race_id={NK_ID}&type=1")


def test_parse_comments():
    rows = P.parse_comments(page("comment.html").decode("utf-8"))
    assert [(r.number, r.name) for r in rows] == [(1, "カクウハードル"), (3, "カクウバンケン")]  # 枠番ではなく馬番
    assert rows[1].comment == "Ｑ 架空の見出し〈架空記者〉 架空のコメントその二 〈架空師〉"  # 全角空白も半角にそろう


@pytest.mark.parametrize("parse", [P.parse_training, P.parse_comments])
def test_parse_unknown_structure_is_an_error(parse):
    with pytest.raises(P.NetkeibaParseError):
        parse("<html><body>構造が違うページ</body></html>")


def test_parse_login_form():
    form = P.parse_login_form(page("login.html").decode("euc_jp"))
    assert (form.user_field, form.password_field) == ("login_id", "pswd")
    assert form.hidden == {"pid": "login", "action": "auth"}


# ---------------------------------------------------------------- 取得


def test_fetch_logs_in_once_and_merges_pages(tmp_path):
    site, wall = Site(), Wall()
    f = make_fetcher(tmp_path, site, wall)
    insights, warnings = fetch_race_insights(f, fake_race(), NK_ID)
    assert site.pages() == [TRAINING_URL.format(NK_ID), COMMENT_URL.format(NK_ID)]
    assert sum(r.method == "POST" for r in site.requests) == 1
    assert [h.number for h in insights.horses] == [1, 3]
    h1 = insights.horses[0]
    assert h1.workout_grade == "A" and "架空の厩舎コメント" in h1.stable_comment
    assert (h1.source, h1.fetched_at) == ("netkeiba", "2026-11-07T09:00:09+09:00")
    assert h1.final_workout_time == "栗Ｗ 80.1-65.2-50.3-36.4-11.5 馬也" and h1.interim_count == 5
    assert h1.interim_highlights[0] == InterimWorkout(
        "2026/10/21(水)", "栗Ｗ 一番時計 81.1-66.2-51.3-37.4-11.6 一杯", "架空の中間その一"
    )
    assert insights.horses[1].workout_grade == "" and insights.horses[1].interim_count is None
    f.close()


def test_mismatched_horse_is_discarded(tmp_path):
    f = make_fetcher(tmp_path, Site(), Wall())
    insights, warnings = fetch_race_insights(f, fake_race(), NK_ID)
    assert 2 not in [h.number for h in insights.horses]
    assert any("2番" in w for w in warnings)
    assert "馬名が違う" not in json.dumps([h.workout_note for h in insights.horses], ensure_ascii=False)
    f.close()


def test_every_request_is_at_least_3s_apart_including_login(tmp_path):
    site, wall = Site(), Wall()
    f = make_fetcher(tmp_path, site, wall, min_interval=0.1)  # 3秒未満は指定しても効かない
    fetch_race_insights(f, fake_race(), NK_ID)
    assert len(site.requests) == 4
    assert wall.sleeps == [3.0, 3.0, 3.0]
    f.close()


def test_interval_holds_across_runs(tmp_path):
    wall = Wall()
    f = make_fetcher(tmp_path, Site(), wall)
    fetch_race_insights(f, fake_race(), NK_ID)
    f.close()
    wall.sleeps.clear()
    wall.now += 1
    f = make_fetcher(tmp_path, Site(), wall)
    f.get("https://race.netkeiba.com/race/comment.html?race_id=other", P.parse_comments, name="x")
    assert wall.sleeps[0] == pytest.approx(2.0)
    f.close()


def test_cache_avoids_login_and_requests(tmp_path):
    wall = Wall()
    f = make_fetcher(tmp_path, Site(), wall)
    first, _ = fetch_race_insights(f, fake_race(), NK_ID)
    f.close()

    def no_credentials():
        raise AssertionError("キャッシュが有効なら認証情報は読まない")

    site = Site()
    wall.now += 3600
    f = make_fetcher(tmp_path, site, wall, credentials=no_credentials)
    second, _ = fetch_race_insights(f, fake_race(), NK_ID)
    assert site.requests == []
    assert second.horses[0].fetched_at == first.horses[0].fetched_at  # 実際に取得した時刻のまま
    f.close()


def test_page_that_fails_to_parse_is_not_cached(tmp_path):
    empty = "<html></html>".encode()
    wall = Wall()
    f = make_fetcher(tmp_path, Site(training=empty), wall)
    insights, warnings = fetch_race_insights(f, fake_race(), NK_ID)  # 片方だけでも返す
    assert insights.horses and any("調教" in w for w in warnings)
    f.close()
    site = Site()
    f = make_fetcher(tmp_path, site, wall)
    fetch_race_insights(f, fake_race(), NK_ID)
    assert site.pages() == [TRAINING_URL.format(NK_ID)]  # 解析できなかった方だけ取り直す
    f.close()


def test_both_pages_unreadable_is_an_error(tmp_path):
    f = make_fetcher(tmp_path, Site(training=b"<html></html>", comment=b"<html></html>"), Wall())
    with pytest.raises(NetkeibaError, match="保存しません"):
        fetch_race_insights(f, fake_race(), NK_ID)
    f.close()


def test_login_failure_stops_without_retry(tmp_path):
    site = Site(login_ok=False)
    f = make_fetcher(tmp_path, site, Wall())
    with pytest.raises(NetkeibaError, match="再試行はしません") as e:
        fetch_race_insights(f, fake_race(), NK_ID)
    assert sum(r.method == "POST" for r in site.requests) == 1
    assert site.pages() == []
    assert USER not in str(e.value) and PASSWORD not in str(e.value)
    f.close()


def test_credentials_go_only_in_post_body(tmp_path):
    site = Site()
    f = make_fetcher(tmp_path, site, Wall())
    fetch_race_insights(f, fake_race(), NK_ID)
    posts = [r for r in site.requests if r.method == "POST"]
    assert PASSWORD in posts[0].content.decode().replace("%2D", "-")
    for r in site.requests:
        assert USER not in str(r.url) and PASSWORD not in str(r.url)
    f.close()
    assert PASSWORD.encode() not in (tmp_path / "nk.sqlite").read_bytes()


def test_login_form_pointing_elsewhere_is_refused(tmp_path):
    evil = page("login.html").replace(b"https://regist.netkeiba.com/", b"https://evil.example/")

    class EvilSite(Site):
        def __call__(self, request):
            if str(request.url) == LOGIN_URL:
                self.requests.append(request)
                return httpx.Response(200, content=evil, headers={"content-type": "text/html"})
            return super().__call__(request)

    site = EvilSite()
    f = make_fetcher(tmp_path, site, Wall())
    with pytest.raises(NetkeibaError, match="送信先"):
        fetch_race_insights(f, fake_race(), NK_ID)
    assert not any(r.method == "POST" for r in site.requests)
    f.close()


@pytest.mark.parametrize("status", [403, 429, 500, 302])
def test_page_error_stops_immediately(tmp_path, status):
    site = Site(page_status=status)
    f = make_fetcher(tmp_path, site, Wall())
    with pytest.raises(NetkeibaError):
        fetch_race_insights(f, fake_race(), NK_ID)
    assert len(site.pages()) == 1
    f.close()


def test_connection_error_message_has_no_details(tmp_path):
    def boom(request):
        raise httpx.ConnectError("secret detail")

    f = NetkeibaFetcher(tmp_path / "nk.sqlite", creds, transport=httpx.MockTransport(boom), sleep=lambda s: None)
    with pytest.raises(NetkeibaError) as e:
        fetch_race_insights(f, fake_race(), NK_ID)
    assert "secret detail" not in str(e.value)
    f.close()


# ---------------------------------------------------------------- 認証情報


def bws_list(entries, returncode=0):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout=json.dumps(entries), stderr="error: x")

    return run, calls


KEYS = {"netkeiba_id": "SC_NetkeibaId", "netkeiba_password": "SC_NetkeibaPassword"}


def test_bitwarden_finds_secret_by_key_name():
    run, calls = bws_list([{"key": "other", "value": "NOPE"}, {"key": "SC_NetkeibaId", "value": USER}])
    p = BitwardenCredentialProvider({}, lambda: "T", runner=run, secret_keys=KEYS)
    assert p.get("netkeiba_id").reveal() == USER
    assert calls == [["bws", "secret", "list"]]


def test_bitwarden_secret_id_takes_priority_over_key():
    uuid = "55555555-5555-5555-5555-555555555555"

    def run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"value": USER}), stderr="")

    p = BitwardenCredentialProvider({"netkeiba_id": uuid}, lambda: "T", runner=run, secret_keys=KEYS)
    assert p.get("netkeiba_id").reveal() == USER


def test_bitwarden_key_missing_message_has_no_other_values():
    run, _ = bws_list([{"key": "other", "value": "OTHER-SECRET-VALUE"}])
    p = BitwardenCredentialProvider({}, lambda: "T", runner=run, secret_keys=KEYS)
    with pytest.raises(CredentialError, match="SC_NetkeibaId") as e:
        p.get("netkeiba_id")
    assert "OTHER-SECRET-VALUE" not in str(e.value)


def test_bitwarden_duplicate_key_is_an_error():
    run, _ = bws_list([{"key": "SC_NetkeibaId", "value": "a"}, {"key": "SC_NetkeibaId", "value": "b"}])
    with pytest.raises(CredentialError, match="複数"):
        BitwardenCredentialProvider({}, lambda: "T", runner=run, secret_keys=KEYS).get("netkeiba_id")


def test_env_provider_reads_netkeiba(monkeypatch):
    monkeypatch.setenv("KEIBA_NETKEIBA_ID", USER)
    assert EnvCredentialProvider().get("netkeiba_id").reveal() == USER


def test_config_secret_key_defaults_and_override(tmp_path):
    base = "[guard]\nper_race_limit = 1\nweekly_limit = 1\nloss_stop_threshold = 1\nbet_unit = 1\n"
    p = tmp_path / "c.toml"
    p.write_text(base, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.bws_secret_keys == KEYS
    assert cfg.insights_dir == tmp_path / "insights"
    assert cfg.netkeiba_cache_path == tmp_path / "netkeiba_cache.sqlite"
    assert cfg.netkeiba_request_interval == 3.0
    p.write_text(base + '[credentials.bitwarden.secret_keys]\nnetkeiba_id = "MyKey"\n', encoding="utf-8")
    assert load_config(p).bws_secret_keys == {**KEYS, "netkeiba_id": "MyKey"}


def test_config_rejects_short_interval(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        "[guard]\nper_race_limit = 1\nweekly_limit = 1\nloss_stop_threshold = 1\nbet_unit = 1\n"
        "[data.netkeiba]\nrequest_interval_sec = 2\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="3 秒以上"):
        load_config(p)


# ---------------------------------------------------------------- admin.py netkeiba-fetch


class NoSecrets:
    def get(self, name):
        raise CredentialError("シークレット（キー名 SC_NetkeibaId）が見つかりません")


class FakeProvider:
    def get(self, name):
        return Secret(USER if name == "netkeiba_id" else PASSWORD)


@pytest.fixture
def run_fetch(tmp_path):
    src = jra_source(tmp_path)
    cfg = replace(
        make_config(tmp_path), insights_dir=tmp_path / "insights", netkeiba_cache_path=tmp_path / "nk.sqlite"
    )

    def _run(*, race=None, date=None, site=None, provider=None, now=SAT_MORNING, **kw):
        return admin.netkeiba_fetch(
            cfg,
            race,
            date,
            data=src,
            provider=provider or FakeProvider(),
            transport=httpx.MockTransport(site or Site()),
            sleep=lambda s: None,
            now=now,
            **kw,
        )

    yield _run, cfg
    src.f.close()


def test_admin_saves_json_that_the_tool_reads(run_fetch, tmp_path, capsys):
    run, cfg = run_fetch
    site = Site()
    assert run(race=RACE, site=site) == 0
    assert site.pages() == [TRAINING_URL.format(NK_ID), COMMENT_URL.format(NK_ID)]
    out = capsys.readouterr().out
    assert "架空の厩舎コメント" not in out and USER not in out and PASSWORD not in out
    saved = (cfg.insights_dir / f"{RACE}.json").read_text(encoding="utf-8")
    assert PASSWORD not in saved and USER not in saved
    insights = FileSupplementSource(cfg.insights_dir).get_insights(RACE)
    assert [h.number for h in insights.horses] == [1, 3]
    assert insights.horses[0].interim_count == 5 and len(insights.horses[0].interim_highlights) == 3


def test_admin_missing_secrets_fails_gracefully(run_fetch, capsys):
    run, cfg = run_fetch
    site = Site()
    assert run(race=RACE, site=site, provider=NoSecrets()) == 1
    assert site.requests == []  # netkeiba には接続しない
    out = capsys.readouterr().out
    assert "認証情報を取り出せないため中止" in out and "SC_NetkeibaId" in out
    assert not (cfg.insights_dir / f"{RACE}.json").exists()


@pytest.mark.parametrize("race", ["20261107-KYOTO-07", "20261107-KYOTO-09", "20261107-KYOTO-11"])
def test_admin_refuses_non_graded_including_listed(run_fetch, race, capsys):
    run, _ = run_fetch
    site = Site()
    assert run(race=race, site=site) == 2
    assert site.requests == []
    assert "重賞ではない" in capsys.readouterr().out


def test_admin_refuses_after_post_time(run_fetch, capsys):
    run, _ = run_fetch
    site = Site()
    assert run(race=RACE, site=site, now=datetime(2026, 11, 7, 14, 5, tzinfo=JST)) == 2
    assert site.requests == []
    assert "発走後" in capsys.readouterr().out


def test_admin_failure_keeps_previous_file(run_fetch):
    run, cfg = run_fetch
    assert run(race=RACE) == 0
    path = cfg.insights_dir / f"{RACE}.json"
    before = path.read_text(encoding="utf-8")
    cfg.netkeiba_cache_path.unlink()
    assert run(race=RACE, site=Site(page_status=403)) == 1
    assert path.read_text(encoding="utf-8") == before


def test_admin_requires_jra_source(tmp_path, capsys):
    from data.dummy import DummyDataSource

    assert admin.netkeiba_fetch(make_config(tmp_path), RACE, data=DummyDataSource()) == 2
    assert "jra" in capsys.readouterr().out


def test_admin_check_credentials_netkeiba_does_not_touch_network(capsys):
    from credentials import NETKEIBA_CREDENTIAL_NAMES

    assert admin.check_credentials(NoSecrets(), NETKEIBA_CREDENTIAL_NAMES) == 1
    assert "netkeiba の ID: NG" in capsys.readouterr().out


# ---------------------------------------------------------------- ファイルの読み込み


def horse(**kw) -> dict:
    return {"number": 1, "name": "カクウ", "source": "netkeiba", "fetched_at": "2026-11-06T19:00:00+09:00", **kw}


def write(tmp_path, content, race_id=RACE):
    path = tmp_path / f"{race_id}.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return FileSupplementSource(tmp_path)


def doc(*horses, race_id=RACE, post_time="2026-11-07T14:05+09:00"):
    return {"race_id": race_id, "post_time": post_time, "horses": list(horses)}


def test_missing_file_is_none(tmp_path):
    assert FileSupplementSource(tmp_path / "none").get_insights(RACE) is None


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        "[]",
        {"race_id": RACE},
        doc(race_id="20261107-KYOTO-09"),
        doc(horse(number=0)),
        doc(horse(number=True)),
        doc(horse(interim_count=-1)),
        doc(horse(fetched_at="yesterday")),
        doc(horse(fetched_at="2026-11-06T19:00:00")),
        doc(horse(interim_highlights=["x"])),
    ],
)
def test_broken_file_raises(tmp_path, content):
    with pytest.raises(SupplementError):
        write(tmp_path, content).get_insights(RACE)


def test_oversized_file_raises(tmp_path):
    with pytest.raises(SupplementError):
        write(tmp_path, " " * 1_000_001).get_insights(RACE)


def test_data_fetched_after_post_is_dropped(tmp_path):
    src = write(tmp_path, doc(horse(), horse(number=2, fetched_at="2026-11-07T14:05:00+09:00")))
    assert [h.number for h in src.get_insights(RACE).horses] == [1]


def test_comment_is_limited_and_cleaned_on_read(tmp_path):
    src = write(tmp_path, doc(horse(stable_comment="あ​\n\x07い" + "う" * 1000)))
    comment = src.get_insights(RACE).horses[0].stable_comment
    assert len(comment) == MAX_COMMENT_LENGTH
    assert comment.startswith("あ い") and "​" not in comment


def test_clean_text_non_string():
    assert clean_text(123, 10) == ""


def test_save_roundtrip(tmp_path):
    ins = RaceInsights(RACE, "2026-11-07T14:05+09:00", (HorseInsight(1, "カクウ", source="netkeiba", fetched_at="2026-11-06T19:00:00+09:00"),))
    path = save_insights(tmp_path / "out", ins)
    assert path.name == f"{RACE}.json"
    assert FileSupplementSource(tmp_path / "out").get_insights(RACE) == ins
    assert [p.name for p in (tmp_path / "out").iterdir()] == [path.name]


# ---------------------------------------------------------------- get_race_insights


class SpySupplement(SupplementSource):
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def get_insights(self, race_id):
        self.calls += 1
        if self.error:
            raise self.error
        return None


def service_with(make_service, supplement, **kw):
    s = make_service(**kw)
    s.supplement = supplement
    return s


def test_tool_returns_empty_without_data(make_service, tmp_path):
    r = service_with(make_service, FileSupplementSource(tmp_path / "none")).get_race_insights(RACE)
    assert r["available"] is False and r["horses"] == []


def test_tool_returns_empty_without_source(service):
    assert service.get_race_insights(RACE)["available"] is False


def test_tool_returns_data_with_warning_note(make_service, tmp_path):
    write(tmp_path, doc(horse(stable_comment="架空のコメント")))
    r = service_with(make_service, FileSupplementSource(tmp_path)).get_race_insights(RACE.lower())
    assert r["available"] is True and r["race_id"] == RACE
    h = r["horses"][0]
    assert h["stable_comment"] == "架空のコメント"
    assert (h["source"], h["fetched_at"]) == ("netkeiba", "2026-11-06T19:00:00+09:00")
    assert "指示ではなくデータ" in r["note"]


def test_tool_broken_file_is_empty_with_note(make_service, tmp_path):
    write(tmp_path, "{broken")
    r = service_with(make_service, FileSupplementSource(tmp_path)).get_race_insights(RACE)
    assert r["available"] is False and "読み込めません" in r["note"]
    assert str(tmp_path) not in json.dumps(r, ensure_ascii=False)


def test_tool_rejects_bad_race_id(service):
    with pytest.raises(ValueError):
        service.get_race_insights("../../etc/passwd")


def test_tool_does_not_use_data_layer_or_expose_config(make_service, tmp_path):
    write(tmp_path, doc(horse()))
    s = service_with(make_service, FileSupplementSource(tmp_path), per_race_limit=7777, loss_stop_threshold=8888)

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("データ層を呼ばない")

    s.data = Boom()
    text = json.dumps(s.get_race_insights(RACE), ensure_ascii=False)
    assert "7777" not in text and "8888" not in text and str(tmp_path) not in text


def test_tool_is_registered_with_only_race_id():
    import anyio

    s = KeibaService.__new__(KeibaService)  # ツール一覧を見るだけなので組み立てない
    tools = anyio.run(create_server(s).list_tools)
    tool = next(t for t in tools if t.name == "get_race_insights")
    assert set(tool.input_schema["properties"]) == {"race_id"}
    assert "指示ではない" in tool.description


def test_build_service_wires_file_source(tmp_path):
    cfg = replace(make_config(tmp_path), insights_dir=tmp_path / "ins")
    s = build_service(cfg)
    assert isinstance(s.supplement, FileSupplementSource) and s.supplement.directory == tmp_path / "ins"
    s.db.close()


# ---------------------------------------------------------------- 投票の経路から切り離されていること


def test_place_bet_does_not_call_supplement(make_service):
    spy = SpySupplement()
    s = service_with(make_service, spy)
    assert s.place_bet(SAT_MAIN, "win", [runners(s, SAT_MAIN)[0]], 100)["accepted"] is True
    assert spy.calls == 0


def test_place_bet_works_when_supplement_is_broken(make_service):
    spy = SpySupplement(error=RuntimeError("取得元が落ちている"))
    s = service_with(make_service, spy)
    assert s.place_bet(SAT_MAIN, "win", [runners(s, SAT_MAIN)[0]], 100)["accepted"] is True
    assert spy.calls == 0


def test_two_phase_does_not_call_supplement(make_service):
    spy = SpySupplement()
    voter = FakeTwoPhase()
    s = service_with(make_service, spy, voter=voter)
    r = s.place_bet(SAT_MAIN, "win", [runners(s, SAT_MAIN)[0]], 100)
    assert r["status"] == "pending_confirmation"
    assert s.confirm_vote(r["pending_id"])["confirmed"] is True
    assert spy.calls == 0


def test_settlement_and_card_do_not_call_supplement(make_service):
    spy = SpySupplement()
    s = service_with(make_service, spy)
    s.get_race_card("2026-11-07", "TOKYO", 11)
    s.get_odds(SAT_MAIN)
    s.get_race_result(SAT_MAIN)
    s.settle_pending_bets()
    s.get_stats()
    assert spy.calls == 0


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


@pytest.mark.parametrize("path", ["server.py", "guard.py", "data/supplement.py", "data/scraper.py", "voter/browser.py"])
def test_no_netkeiba_import_in_server_or_voting_path(path):
    assert not {n for n in _imports(ROOT / path) if "netkeiba" in n}


@pytest.mark.parametrize("path", ["server.py", "data/supplement.py", "data/netkeiba_parse.py"])
def test_server_side_never_imports_credentials(path):
    assert not {n for n in _imports(ROOT / path) if n.split(".")[0] == "credentials"}


def test_importing_server_loads_neither_credentials_nor_fetcher():
    code = (
        "import sys, server; "
        "assert not {'credentials', 'data.netkeiba', 'data.netkeiba_parse'} & set(sys.modules), sys.modules.keys()"
    )
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True, capture_output=True)


def test_mcp_has_no_fetch_tool():
    import anyio

    names = {t.name for t in anyio.run(create_server(KeibaService.__new__(KeibaService)).list_tools)}
    assert not {n for n in names if "netkeiba" in n or "fetch" in n}


def test_db_is_untouched_by_supplement_tables(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {t for t in tables if "insight" in t or "supplement" in t}
    db.close()

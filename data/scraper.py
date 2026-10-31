"""JRA 公式サイトからの実データ取得。

- 利用規約（https://www.jra.go.jp/use/）と robots.txt（全ページ許可）を確認済み。取得したデータは私的使用の範囲で使う
- アクセスは低頻度：リクエストの間隔を必ず空ける（既定 3 秒）
- 取得結果は SQLite にキャッシュし、有効期限内は同じページを取りに行かない
- JRADB のページは JavaScript なしで取得できるため、Playwright ではなく HTTP（POST）で取得する
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time as _time
from datetime import date
from pathlib import Path
from typing import Callable

import httpx

from data import jra_parse as P
from data.base import DataSource
from models import BetType, Race, RaceResult, canonical_race_id, make_race_id, parse_race_id

BASE_URL = "https://www.jra.go.jp"

# 開催選択ページ（トップページのメニューにある固定の cname）
CARD_INDEX = ("/JRADB/accessD.html", "pw01dli00/F3")
RESULT_INDEX = ("/JRADB/accessS.html", "pw01sli00/AF")

# キャッシュの有効期限（秒）
TTL_INDEX = 600
TTL_RACE_LIST = 300
TTL_CARD = 600
TTL_ODDS = 60
TTL_RESULT = 300


class FetchError(Exception):
    """JRA サイトからの取得に失敗した。"""


class JraFetcher:
    """間隔制限とキャッシュつきの取得クライアント。"""

    def __init__(
        self,
        cache_path: str | Path,
        *,
        min_interval: float = 3.0,
        user_agent: str = "keiba-mcp (personal use)",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = _time.monotonic,
        sleep: Callable[[float], None] = _time.sleep,
        wall_clock: Callable[[], float] = _time.time,
    ):
        self.min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._wall = wall_clock
        self._last_request: float | None = None
        self._lock = threading.Lock()
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            transport=transport,
        )
        self._db = sqlite3.connect(str(cache_path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS http_cache (key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body TEXT NOT NULL)"
        )
        self._db.commit()
        self.request_count = 0

    def close(self) -> None:
        self._client.close()
        self._db.close()

    def get(self, path: str, cname: str, ttl: float | None) -> str:
        """ページを取得する。ttl 秒以内に取得済みならキャッシュを返す（None なら無期限）。"""
        key = f"{path}?{cname}"
        with self._lock:
            row = self._db.execute("SELECT fetched_at, body FROM http_cache WHERE key = ?", (key,)).fetchone()
            if row is not None and (ttl is None or self._wall() - row[0] < ttl):
                return row[1]

            if self._last_request is not None:
                wait = self.min_interval - (self._monotonic() - self._last_request)
                if wait > 0:
                    self._sleep(wait)
            try:
                self.request_count += 1
                resp = self._client.post(path, data={"cname": cname})
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise FetchError(f"JRA サイトからの取得に失敗しました: {type(e).__name__}") from e
            finally:
                self._last_request = self._monotonic()

            body = resp.content.decode(resp.charset_encoding or "cp932", errors="replace")
            self._db.execute(
                "INSERT OR REPLACE INTO http_cache (key, fetched_at, body) VALUES (?, ?, ?)",
                (key, self._wall(), body),
            )
            self._db.commit()
            return body


class JraDataSource(DataSource):
    def __init__(self, fetcher: JraFetcher):
        self.f = fetcher

    # ------------------------------------------------------------------ 内部

    def _meeting(self, index: tuple[str, str], course: str, d: date) -> P.MeetingLink | None:
        html = self.f.get(*index, ttl=TTL_INDEX)
        for m in P.parse_meeting_links(html):
            if m.course == course and m.date == d:
                return m
        return None

    def _card_meetings(self, d: date, course: str | None) -> list[P.MeetingLink]:
        html = self.f.get(*CARD_INDEX, ttl=TTL_INDEX)
        return [m for m in P.parse_meeting_links(html) if m.date == d and (course is None or m.course == course)]

    def _race_list(self, m: P.MeetingLink) -> str:
        return self.f.get(m.path, m.cname, ttl=TTL_RACE_LIST)

    def _card_links(self, race_id: str) -> dict[str, str] | None:
        k = parse_race_id(race_id)
        m = self._meeting(CARD_INDEX, k.course, k.date)
        if m is None:
            return None
        return P.parse_race_links(self._race_list(m)).get(k.number)

    @staticmethod
    def _to_race(course: str, pr: P.ParsedRace) -> Race:
        return Race(
            race_id=make_race_id(pr.date, course, pr.number),
            date=pr.date,
            course=course,
            course_name=P.COURSE_BY_ALPHA[course][1],
            number=pr.number,
            name=pr.name,
            surface=pr.surface,
            distance=pr.distance,
            post_time=pr.post_time,
            horses=tuple(pr.horses),
            race_class=pr.race_class,
        )

    @staticmethod
    def _course(course: str | None) -> str | None:
        if course is None:
            return None
        c = course.strip()
        if c.upper() in P.COURSE_BY_ALPHA:
            return c.upper()
        return P.COURSE_BY_NAME.get(c.removesuffix("競馬場"), "UNKNOWN")

    # ------------------------------------------------------------------ DataSource

    def get_races(self, race_date: date, course: str | None = None) -> list[Race]:
        races: list[Race] = []
        for m in self._card_meetings(race_date, self._course(course)):
            race_list = self._race_list(m)
            des = re.search(r"pw01des\d{2}\d{2}\d{4}\d{2}\d{2}\d{8}/[0-9A-F]{2}", race_list)
            if des is None:
                continue
            html = self.f.get(m.path, des.group(0), ttl=TTL_CARD)
            races.extend(self._to_race(m.course, pr) for pr in P.parse_race_cards(html))
        return sorted(races, key=lambda r: (r.course, r.number))

    def get_race(self, race_id: str) -> Race | None:
        try:
            rid = canonical_race_id(race_id)
        except ValueError:
            return None
        k = parse_race_id(rid)
        if k.course not in P.COURSE_BY_ALPHA:
            return None
        links = self._card_links(rid)
        if not links or "card" not in links:
            return None
        html = self.f.get("/JRADB/accessD.html", links["card"], ttl=TTL_CARD)
        for pr in P.parse_race_cards(html):
            if pr.number == k.number and pr.date == k.date:
                return self._to_race(k.course, pr)
        return None

    def get_odds(self, race_id: str, bet_type: BetType) -> dict[str, float]:
        rid = canonical_race_id(race_id)
        links = self._card_links(rid)
        if not links or "odds" not in links:
            return {}
        base = self.f.get("/JRADB/accessO.html", links["odds"], ttl=TTL_ODDS)
        if bet_type in (BetType.WIN, BetType.PLACE):
            return P.parse_odds(base, bet_type)
        kind_cname = P.parse_odds_links(base, parse_race_id(rid).number).get(P.ODDS_KIND[bet_type])
        if kind_cname is None:
            return {}
        return P.parse_odds(self.f.get("/JRADB/accessO.html", kind_cname, ttl=TTL_ODDS), bet_type)

    def get_result(self, race_id: str) -> RaceResult | None:
        rid = canonical_race_id(race_id)
        k = parse_race_id(rid)
        if k.course not in P.COURSE_BY_ALPHA:
            return None
        m = self._meeting(RESULT_INDEX, k.course, k.date)
        if m is None:
            return None
        links = P.parse_race_links(self._race_list(m)).get(k.number)
        if not links or "result" not in links:
            return None
        parsed = P.parse_result(self.f.get("/JRADB/accessS.html", links["result"], ttl=TTL_RESULT))
        if parsed is None:
            return None
        return RaceResult(
            race_id=rid,
            order=tuple(parsed.order),
            payouts=parsed.payouts,
            refunded_horses=frozenset(parsed.refunded_horses),
        )

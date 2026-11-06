"""netkeiba からの補助データの取得。admin.py netkeiba-fetch 専用で、サーバ（server.py）からは読み込まない。

- 規約（第14条 私的利用・第17条 運営の妨害の禁止）と公式サポートの通信制限の説明を踏まえ、低頻度に限る
  - 重賞だけ、1レース2ページ。リクエスト間隔は最低3秒（プロセスをまたいでも守る）。キャッシュを必ず通す
  - 自動で再試行しない。ログイン失敗・403 などが出たら、その場で止める
- リダイレクトは自動で追わない（間隔制限の外でリクエストが飛ばないように）
- 認証情報の値はログイン時の POST にだけ使う。エラーメッセージ・ログ・ファイルに入れない
"""

from __future__ import annotations

import re
import sqlite3
import time as _time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, TypeVar
from urllib.parse import urljoin, urlsplit

import httpx

from config import NETKEIBA_MIN_INTERVAL
from data import netkeiba_parse as P
from data.supplement import (
    MAX_COMMENT_LENGTH,
    MAX_HIGHLIGHTS,
    MAX_NOTE_LENGTH,
    MAX_SHORT_LENGTH,
    HorseInsight,
    InterimWorkout,
    RaceInsights,
    clean_text,
)
from models import JST, Race

if TYPE_CHECKING:
    from credentials import Secret

T = TypeVar("T")

SOURCE = "netkeiba"
LOGIN_URL = "https://regist.netkeiba.com/?pid=login"
TRAINING_URL = "https://race.netkeiba.com/race/oikiri.html?race_id={}&type=1"
COMMENT_URL = "https://race.netkeiba.com/race/comment.html?race_id={}"

# キャッシュの有効期限（秒）。短くしない
TTL_PAGE = 6 * 3600


class NetkeibaError(Exception):
    """取得に失敗した。メッセージは人間向けで、認証情報の値は含めない。"""


class NetkeibaFetcher:
    """間隔制限・キャッシュ・ログインつきの取得クライアント。

    credentials はキャッシュに無いページを取りに行くときに初めて呼ばれる（ID とパスワードを返す）。
    セッション（Cookie）はメモリだけに持ち、ファイルに残さない。
    """

    def __init__(
        self,
        cache_path: str | Path,
        credentials: Callable[[], tuple[Secret, Secret]],
        *,
        min_interval: float = NETKEIBA_MIN_INTERVAL,
        user_agent: str = "keiba-mcp (personal use)",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = _time.sleep,
        wall_clock: Callable[[], float] = _time.time,
        pages_dir: Path | None = None,
    ):
        self.min_interval = max(min_interval, NETKEIBA_MIN_INTERVAL)
        self._credentials = credentials
        self._sleep = sleep
        self._wall = wall_clock
        self._pages_dir = pages_dir
        self._logged_in = False
        self._client = httpx.Client(headers={"User-Agent": user_agent}, timeout=timeout, transport=transport)
        self._db = sqlite3.connect(str(cache_path))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS http_cache (url TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body TEXT NOT NULL)"
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS last_request (id INTEGER PRIMARY KEY CHECK (id = 1), at REAL NOT NULL)")
        self._db.commit()
        self.request_count = 0

    def close(self) -> None:
        self._client.close()
        self._db.close()

    def _throttle(self) -> None:
        row = self._db.execute("SELECT at FROM last_request").fetchone()
        if row is not None:
            wait = self.min_interval - (self._wall() - row[0])
            if wait > 0:
                self._sleep(min(wait, self.min_interval))  # 時計が戻っていても待ちすぎない

    def _request(self, method: str, url: str, data: dict[str, str] | None = None) -> httpx.Response:
        self._throttle()
        try:
            self.request_count += 1
            return self._client.request(method, url, data=data)
        except httpx.HTTPError as e:
            raise NetkeibaError(f"netkeiba への接続に失敗しました（{type(e).__name__}）") from None
        finally:
            self._db.execute("INSERT OR REPLACE INTO last_request (id, at) VALUES (1, ?)", (self._wall(),))
            self._db.commit()

    @staticmethod
    def _decode(resp: httpx.Response) -> str:
        """Content-Type → meta charset → EUC-JP の順で文字コードを決める。"""
        m = re.search(rb"charset=[\"']?([\w-]+)", resp.content[:2048], re.I)
        encoding = resp.charset_encoding or (m.group(1).decode("ascii") if m else "euc_jp")
        try:
            return resp.content.decode(encoding, errors="replace")
        except LookupError:
            return resp.content.decode("euc_jp", errors="replace")

    def _login(self) -> None:
        if self._logged_in:
            return
        user, password = self._credentials()
        page = self._request("GET", LOGIN_URL)
        if page.status_code != 200:
            raise NetkeibaError(f"ログイン画面を開けませんでした（HTTP {page.status_code}）")
        try:
            form = P.parse_login_form(self._decode(page))
        except P.NetkeibaParseError as e:
            raise NetkeibaError(f"{e}。ログイン画面の構造が変わった可能性があります") from None
        action = urljoin(LOGIN_URL, form.action)
        parts = urlsplit(action)
        if parts.scheme != "https" or not (parts.hostname or "").endswith(".netkeiba.com"):
            raise NetkeibaError("ログインフォームの送信先が netkeiba ではないため中止しました")
        resp = self._request(
            "POST",
            action,
            {**form.hidden, form.user_field: user.reveal(), form.password_field: password.reveal()},
        )
        if resp.is_redirect:
            rejected = "pid=login" in resp.headers.get("location", "")
        elif resp.status_code == 200:
            rejected = P.has_password_field(self._decode(resp))
        else:
            raise NetkeibaError(f"ログインに失敗しました（HTTP {resp.status_code}）。再試行はしません")
        if rejected:
            raise NetkeibaError("ログインに失敗しました（ID・パスワードを確認してください）。再試行はしません")
        self._logged_in = True

    def get(self, url: str, parse: Callable[[str], T], *, name: str) -> tuple[T, float]:
        """ページを取得して解析する。戻り値は (解析結果, 取得時刻)。

        解析に成功したページだけをキャッシュする（公開前の空のページを有効期限の間使い続けないように）。
        """
        row = self._db.execute("SELECT fetched_at, body FROM http_cache WHERE url = ?", (url,)).fetchone()
        if row is not None and self._wall() - row[0] < TTL_PAGE:
            return parse(row[1]), row[0]

        self._login()
        resp = self._request("GET", url)
        if resp.is_redirect:
            raise NetkeibaError(f"ページが転送されました（HTTP {resp.status_code}）。ログインが切れた可能性があります")
        if resp.status_code != 200:
            raise NetkeibaError(
                f"ページを取得できませんでした（HTTP {resp.status_code}）。通信制限の可能性があるので、時間をおいてください"
            )
        body = self._decode(resp)
        if self._pages_dir is not None:
            self._pages_dir.mkdir(parents=True, exist_ok=True)
            (self._pages_dir / f"{name}.html").write_text(body, encoding="utf-8")
        if P.has_password_field(body):
            raise NetkeibaError("ログイン画面が返ってきました。ログインが切れた可能性があります")
        parsed = parse(body)
        fetched_at = self._wall()
        self._db.execute(
            "INSERT OR REPLACE INTO http_cache (url, fetched_at, body) VALUES (?, ?, ?)", (url, fetched_at, body)
        )
        self._db.commit()
        return parsed, fetched_at


def _matched(race: Race, rows: list, label: str, warnings: list[str]) -> dict:
    """馬番で出馬表と突き合わせ、馬名が一致したものだけ残す（枠順確定前のデータなどを混ぜない）。"""
    names = {h.number: P.normalize_name(h.name) for h in race.horses}
    out = {}
    for row in rows:
        if names.get(row.number) != P.normalize_name(row.name):
            warnings.append(f"{label}: {row.number}番は出馬表と馬名が一致しないため捨てました")
            continue
        out.setdefault(row.number, row)
    return out


def fetch_race_insights(fetcher: NetkeibaFetcher, race: Race, nk_race_id: str) -> tuple[RaceInsights, list[str]]:
    """1レース分（2ページ）を取得する。戻り値は (補助データ, 警告)。

    片方のページが解析できなければ、もう片方だけで返す。両方だめなら NetkeibaError。
    通信の失敗は NetkeibaError のまま上に投げ、それ以上リクエストしない。
    """
    warnings: list[str] = []
    training: dict[int, P.ParsedTraining] = {}
    comments: dict[int, P.ParsedComment] = {}
    times: list[float] = []
    try:
        rows, at = fetcher.get(TRAINING_URL.format(nk_race_id), P.parse_training, name=f"{nk_race_id}-training")
        training = _matched(race, rows, "調教", warnings)
        times.append(at)
    except P.NetkeibaParseError as e:
        warnings.append(str(e))
    try:
        rows, at = fetcher.get(COMMENT_URL.format(nk_race_id), P.parse_comments, name=f"{nk_race_id}-comment")
        comments = _matched(race, rows, "厩舎コメント", warnings)
        times.append(at)
    except P.NetkeibaParseError as e:
        warnings.append(str(e))

    if not training and not comments:
        raise NetkeibaError(f"{race.race_id} のデータを読み取れなかったため保存しません（{' / '.join(warnings)}）")

    fetched_at = datetime.fromtimestamp(max(times), JST).isoformat(timespec="seconds")
    horses = []
    for h in race.horses:
        t, c = training.get(h.number), comments.get(h.number)
        if t is None and c is None:
            continue
        horses.append(
            HorseInsight(
                number=h.number,
                name=h.name,
                workout_grade=clean_text(t.grade, MAX_SHORT_LENGTH) if t else "",
                workout_note=clean_text(t.note, MAX_NOTE_LENGTH) if t else "",
                final_workout_time=clean_text(t.final_time, MAX_NOTE_LENGTH) if t else "",
                interim_count=t.interim_count if t else None,
                interim_highlights=tuple(
                    InterimWorkout(
                        clean_text(w.date, MAX_SHORT_LENGTH),
                        clean_text(w.time, MAX_NOTE_LENGTH),
                        clean_text(w.note, MAX_NOTE_LENGTH),
                    )
                    for w in t.interim_highlights[:MAX_HIGHLIGHTS]
                )
                if t
                else (),
                stable_comment=clean_text(c.comment, MAX_COMMENT_LENGTH) if c else "",
                source=SOURCE,
                fetched_at=fetched_at,
            )
        )
    return RaceInsights(race.race_id, race.post_time.isoformat(timespec="minutes"), tuple(horses)), warnings

"""即PAT（JRA インターネット投票）での実投票。Playwright でブラウザを操作する。

画面の構造は tools/capture_ipat.py で保存した即PAT の画面（2026年10月時点）に基づく。

安全のための約束:
- ガード層を通過した買い目（ValidatedBet）しか受け取らない
- 認証情報は credentials.py から投票の直前に取り出し、入力欄に入れるときだけ reveal() する。ログ・例外・画面保存に含めない
- 購入予定リストが空でなければ中止する（他の買い目を一緒に買わない）
- 購入予定リストの内容（場・日・レース・券種・馬番・金額・合計）がガード層の内容と完全に一致しなければ中止する
- 合計金額入力欄には、ガード層で検証した金額だけを入れる
- confirm = "manual"（既定）では、最後の「OK」は人間が押す。"auto" のときだけプログラムが押す
- confirm = "external" では二段階：prepare で確認ダイアログまで進めて止め、MCP クライアント側で人間の承認が
  取れてから commit で「OK」を押す（承認されなければ abort）。保留中はブラウザ専用のスレッドが画面を持ち続ける
- プロキシ（proxy_server / HTTPS_PROXY）と、TLS を中継するプロキシの CA（tls_ca_file）に対応する。
  CA は公開鍵の指紋（SPKI）だけを信頼する。検証の全面無効化（ignore_https_errors）は既定で使わない
- 「OK」を押す前の失敗は VoteError（何も買っていない）。「OK」を押した後は例外を出さず、
  結果を確認できなければ confirmed=False を返す（呼び出し側が投票を停止し、人間が投票履歴を確認する）
- 自動での再試行はしない（二重購入を防ぐため）
- 約定の同意画面など、想定外の画面が出たら中止する（同意などの判断はプログラムがしない）
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from urllib.parse import unquote, urlsplit

from credentials import CredentialError, CredentialProvider
from data.jra_parse import COURSE_BY_ALPHA
from guard import ValidatedBet
from models import BetType
from voter.base import Voter, VoteError, VoteReceipt

logger = logging.getLogger("keiba-mcp.ipat")

IPAT_URL = "https://www.ipat.jra.go.jp/"
WEEKDAYS = "月火水木金土日"

# 即PAT の式別プルダウンの表示名
TYPE_LABEL = {
    BetType.WIN: "単勝",
    BetType.PLACE: "複勝",
    BetType.QUINELLA: "馬連",
    BetType.WIDE: "ワイド",
    BetType.EXACTA: "馬単",
    BetType.TRIO: "３連複",
    BetType.TRIFECTA: "３連単",
}

# 画面の部品（即PAT の Angular の属性）
SEL = {
    # ログイン1段目
    "inetid": "input[name='inetid'][type='text']",
    "login1_submit": "a[title='ログイン']",
    # ログイン2段目
    "subscriber": "input[name='i']",
    "pin": "input[name='p']",
    "pars": "input[name='r']",
    "login2_submit": "a[title='ネット投票メニューへ']",
    # メニュー
    "basic_vote": "button[href='#!/bet/basic']",
    "cart_badge": "button.btn-vote-list .badge",
    # レース選択
    "course_button": "button[ng-click='vm.selectCourse(oCourse.courseId)']",
    "race_button": "button[ng-click='vm.selectRace(oJgRn.nRaceIndex + 1)']",
    "course_select": "select#select-course-race-course",
    "race_select": "select#select-course-race-race",
    # 券種・馬番・金額
    "type_select": "select#bet-basic-type",
    "method_select": "select#bet-basic-method",
    "unit": "input[ng-model='vm.nUnit']",
    "set": "button[ng-click='vm.onSet()']",
    "finish_input": "button[ng-click='vm.onShowBetList()']",
    # 購入予定リスト
    "cart": "div.ipat-vote-list",
    "cart_rows": "tr[ng-repeat*='vm.oItems.oaBetData']",
    "cart_total": "span[ng-bind*='getCalcTotalAmount']",
    "cart_total_input": "input[ng-model='vm.cAmountTotal']",
    "purchase": "button[ng-click='vm.clickPurchase()']",
    # 確認・結果
    "dialog": "div.ipat-error-window",
    "dialog_body": "div.ipat-error-window .dialog-body-contents",
    "dialog_ok": "div.ipat-error-window button.btn-ok",
    "dialog_cancel": "div.ipat-error-window button.btn-cancel",
    "ticket": "div.ipat-ticket",
    "result": "bet-list-result",
}

CONFIRM_TEXT = "送信してもよろしいですか"


@dataclass(frozen=True)
class IpatOptions:
    confirm: Literal["manual", "auto", "external"] = "manual"
    headless: bool = False
    browser_channel: str | None = "chrome"  # None なら Playwright の Chromium
    executable_path: str | None = None  # テスト用
    base_url: str = IPAT_URL
    step_timeout_ms: int = 20_000
    manual_confirm_timeout_sec: int = 180
    result_timeout_sec: int = 60
    debug_dir: Path | None = None  # 失敗時の画面保存先（加工してから保存）
    proxy_server: str | None = None  # 未指定なら環境変数 HTTPS_PROXY / https_proxy
    tls_ca_file: Path | None = None  # TLS を中継するプロキシの CA（PEM）。少数の証明書だけを入れる
    ignore_https_errors: bool = False  # 証明書の検証を全面的に無効にする（非推奨）


# tls_ca_file に入れてよい証明書の数の上限（OS の証明書束をそのまま渡すと、全部を無条件に信頼してしまうため）
MAX_CA_CERTS = 5


def spki_hashes(ca_file: str | os.PathLike) -> list[str]:
    """PEM ファイル内の各証明書の公開鍵（SubjectPublicKeyInfo）の SHA-256 を base64 で返す。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    path = Path(ca_file)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise VoteError(f"tls_ca_file を読めませんでした（{path}、{type(e).__name__}）") from None
    try:
        certs = x509.load_pem_x509_certificates(data)
    except ValueError:
        raise VoteError(f"tls_ca_file に PEM 形式の証明書が見つかりませんでした（{path}）") from None
    if not certs:
        raise VoteError(f"tls_ca_file に証明書がありません（{path}）")
    if len(certs) > MAX_CA_CERTS:
        raise VoteError(
            f"tls_ca_file に証明書が {len(certs)} 個あります。プロキシの CA だけを入れたファイルを指定してください"
            f"（OS の証明書束をそのまま使うと、すべてを無条件に信頼してしまうため {MAX_CA_CERTS} 個までにしています）"
        )
    out = []
    for c in certs:
        spki = c.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        out.append(base64.b64encode(hashlib.sha256(spki).digest()).decode())
    return out


def proxy_settings(proxy_server: str | None, env: Mapping[str, str]) -> dict[str, str] | None:
    """Playwright の proxy 設定。認証情報は config に置かず、URL のユーザー情報か環境変数から取る。"""
    raw = (proxy_server or env.get("HTTPS_PROXY") or env.get("https_proxy") or "").strip()
    if not raw:
        return None
    u = urlsplit(raw if "://" in raw else f"http://{raw}")
    if not u.hostname:
        raise VoteError("プロキシの指定（proxy_server / HTTPS_PROXY）の形式が不正です")
    server = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
    out = {"server": server}
    user = env.get("KEIBA_PROXY_USERNAME") or (unquote(u.username) if u.username else "")
    password = env.get("KEIBA_PROXY_PASSWORD") or (unquote(u.password) if u.password else "")
    if user:
        out["username"] = user
    if password:
        out["password"] = password
    bypass = env.get("NO_PROXY") or env.get("no_proxy")
    if bypass:
        out["bypass"] = bypass
    return out


def build_launch_options(o: IpatOptions, env: Mapping[str, str] | None = None) -> tuple[dict, dict]:
    """(chromium.launch の引数, browser.new_context の引数)。プロキシのパスワードを含むのでログに出さないこと。"""
    env = os.environ if env is None else env
    launch: dict[str, Any] = {"headless": o.headless}
    if o.executable_path:
        launch["executable_path"] = o.executable_path
    elif o.browser_channel:
        launch["channel"] = o.browser_channel
    proxy = proxy_settings(o.proxy_server, env)
    if proxy:
        launch["proxy"] = proxy
    if o.tls_ca_file:
        launch["args"] = ["--ignore-certificate-errors-spki-list=" + ",".join(spki_hashes(o.tls_ca_file))]
    context: dict[str, Any] = {"locale": "ja-JP", "viewport": {"width": 1400, "height": 1000}}
    if o.ignore_https_errors:
        logger.warning("ipat: ignore_https_errors が有効です。証明書の検証を行いません")
        context["ignore_https_errors"] = True
    return launch, context


@dataclass(frozen=True)
class PreparedVote:
    """二段階投票の保留（確認ダイアログで止めた状態）。"""

    cart: dict[str, Any]  # 画面から読み取った購入予定リスト（ガード層の内容と照合済み）
    capture_path: str | None  # 加工済みの確認画面の保存先


class _BrowserThread:
    """Playwright（同期 API）を1本の専用スレッドで動かす。保留中の画面を別のツール呼び出しから操作するため。"""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._t = threading.Thread(target=self._run, name="ipat-browser", daemon=True)
        self._t.start()

    def _run(self) -> None:
        while True:
            fn, fut = self._q.get()
            if fn is None:
                return
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except BaseException as e:  # noqa: BLE001 - 呼び出し元へそのまま渡す
                    fut.set_exception(e)

    def call(self, fn: Callable[[], Any], timeout: float | None = None) -> Any:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((fn, fut))
        return fut.result(timeout)

    def stop(self) -> None:
        self._q.put((None, None))


@dataclass
class _Session:
    thread: _BrowserThread
    pw: Any
    browser: Any
    context: Any
    page: Any
    bet: ValidatedBet
    submitted: bool = False
    extra: dict = field(default_factory=dict)


def _digits(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", text)]


def _yen(text: str) -> int | None:
    m = re.search(r"\d[\d,]*", text or "")
    return int(m.group(0).replace(",", "")) if m else None


class BrowserVoter(Voter):
    """即PAT で投票する。"""

    def __init__(self, credentials: CredentialProvider, options: IpatOptions | None = None,
                 *, clock: Callable[[], float] = time.monotonic):
        self.credentials = credentials
        self.options = options or IpatOptions()
        self.clock = clock
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    @property
    def two_phase(self) -> bool:
        return self.options.confirm == "external"

    def _launch(self, pw):
        try:
            launch, context_opts = build_launch_options(self.options)
        except VoteError:
            raise
        try:
            browser = pw.chromium.launch(**launch)
        except Exception as e:
            raise VoteError(f"ブラウザを起動できませんでした（{type(e).__name__}）") from None
        context = browser.new_context(**context_opts)
        page = context.new_page()
        page.set_default_timeout(self.options.step_timeout_ms)
        return browser, context, page

    # ------------------------------------------------------------------ 入口

    def vote(self, bet: ValidatedBet) -> VoteReceipt:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise VoteError("Playwright が入っていません（uv sync を実行してください）") from None

        o = self.options
        if self.two_phase:
            raise VoteError("confirm = \"external\" では prepare / commit を使います（内部エラー）")
        with sync_playwright() as p:
            browser, context, page = self._launch(p)
            self._submitted = False
            try:
                self._login(page)
                self._open_basic_vote(page)
                self._select_race(page, bet)
                self._enter_bet(page, bet)
                self._verify_cart(page, bet)
                self._open_confirm_dialog(page, bet)
                return self._confirm(page, bet)
            except VoteError:
                # VoteError は「購入していないことが確実」なときだけ出す（送信後のキャンセル検出を含む）
                self._save_debug(page, "error")
                raise
            except Exception as e:
                if self._submitted:  # 送信した可能性があるときは例外を出さない（記録を残して人間が確認する）
                    logger.exception("ipat: error after submit")
                    return VoteReceipt(receipt="UNCONFIRMED", dry_run=False, confirmed=False)
                self._save_debug(page, "error")
                logger.exception("ipat: unexpected error before submit")
                raise VoteError(f"即PAT の「{getattr(self, '_step', '?')}」で想定外のエラーが起きたため、購入せずに中止しました（{type(e).__name__}）") from None
            finally:
                if o.headless or not self._submitted or o.confirm == "auto":
                    context.close()
                    browser.close()
                else:
                    # 手動確認では、人間が結果を見られるよう少し待ってから閉じる
                    try:
                        page.wait_for_timeout(5000)
                    finally:
                        context.close()
                        browser.close()

    # ------------------------------------------------------------------ ログイン

    def _secret(self, name: str) -> str:
        try:
            return self.credentials.get(name).reveal()
        except CredentialError as e:
            raise VoteError(f"認証情報を取り出せませんでした: {e}") from None

    def _login(self, page) -> None:
        self._step = "ログイン"
        page.goto(self.options.base_url)
        inet = page.locator(SEL["inetid"])
        if inet.count() == 0:
            raise VoteError("即PAT のログイン画面（INET-ID の入力欄）が見つかりませんでした")
        inet.fill(self._secret("inet_id"))
        page.locator(SEL["login1_submit"]).first.click()

        try:
            page.locator(SEL["pin"]).wait_for(state="visible")
        except Exception:
            raise VoteError("INET-ID の次の画面（加入者番号などの入力欄）が出ませんでした。INET-ID かサイトの状態を確認してください") from None
        page.locator(SEL["subscriber"]).first.fill(self._secret("subscriber_number"))
        page.locator(SEL["pin"]).first.fill(self._secret("pin"))
        page.locator(SEL["pars"]).first.fill(self._secret("pars_number"))
        page.locator(SEL["login2_submit"]).first.click()

        try:
            page.locator(SEL["basic_vote"]).first.wait_for(state="visible")
        except Exception:
            body = page.locator("body").inner_text()
            if "約定" in body and ("同意" in body or "全文を表示" in body):
                raise VoteError("約定（利用規約）の確認画面が出ています。手動でログインして内容を確認してください") from None
            raise VoteError("ログイン後の投票メニューが表示されませんでした。認証情報か、お知らせ画面が出ていないかを確認してください") from None

    # ------------------------------------------------------------------ レース選択

    def _open_basic_vote(self, page) -> None:
        self._step = "通常投票を開く"
        if page.locator(SEL["cart_badge"]).count() > 0 and page.locator(SEL["cart_badge"]).first.is_visible():
            raise VoteError("即PAT の購入予定リストに既に買い目が入っています。手動で空にしてから再度投票してください")
        page.locator(SEL["basic_vote"]).first.click()
        # 通常投票の画面では、まず場・レースの選択が出る。式別のプルダウンはレースを選んだ後に初めて現れる
        try:
            page.locator(f"{SEL['course_button']}, {SEL['course_select']}").first.wait_for(state="attached")
        except Exception:
            raise VoteError("通常投票の画面（場・レースの選択）が表示されませんでした") from None

    def _select_race(self, page, bet: ValidatedBet) -> None:
        self._step = "場・レースの選択"
        race = bet.race
        course_name = COURSE_BY_ALPHA[race.course][1]
        wd = WEEKDAYS[race.date.weekday()]

        buttons = page.locator(f"{SEL['course_button']}:visible")
        if buttons.count() > 0:
            target = buttons.filter(has_text=re.compile(rf"{course_name}\s*[（(]{wd}[）)]"))
            if target.count() != 1:
                raise VoteError(f"即PAT に {course_name}（{wd}）の開催が見つかりませんでした（発売前か、日付が違います）")
            target.first.click()
            race_buttons = page.locator(f"{SEL['race_button']}:visible").filter(
                has_text=re.compile(rf"^\s*{race.number}\s*R\b")
            )
            if race_buttons.count() != 1:
                raise VoteError(f"即PAT に {course_name}{race.number}R が見つかりませんでした")
            label = race_buttons.first.inner_text()
            race_buttons.first.click()
        else:  # 画面幅によってはプルダウン表示になる
            cs = page.locator(SEL["course_select"]).first
            opts = [o for o in cs.locator("option").all_inner_texts() if re.search(rf"{course_name}\s*[（(]{wd}[）)]", o)]
            if len(opts) != 1:
                raise VoteError(f"即PAT に {course_name}（{wd}）の開催が見つかりませんでした（発売前か、日付が違います）")
            cs.select_option(label=opts[0].strip())
            rs = page.locator(SEL["race_select"]).first
            ropts = [o for o in rs.locator("option").all_inner_texts() if re.match(rf"^\s*{race.number}R", o)]
            if len(ropts) != 1:
                raise VoteError(f"即PAT に {course_name}{race.number}R が見つかりませんでした")
            rs.select_option(label=ropts[0].strip())
            label = ""

        # 締切時刻（ボタンに表示）と JRA の発走時刻のずれが大きければ、別のレースとみなして中止
        m = re.search(r"(\d{1,2}):(\d{2})", label)
        if m:
            shown = race.post_time.replace(hour=int(m.group(1)), minute=int(m.group(2)))
            if abs((shown - race.post_time).total_seconds()) > 10 * 60:
                raise VoteError(
                    f"即PAT の {race.number}R の時刻（{m.group(0)}）が出馬表の発走時刻（{race.post_time:%H:%M}）と合わないため中止しました"
                )

    # ------------------------------------------------------------------ 券種・馬番・金額

    def _enter_bet(self, page, bet: ValidatedBet) -> None:
        self._step = "券種・馬番・金額の入力"
        bt = bet.bet_type
        type_select = page.locator(SEL["type_select"]).first
        type_select.wait_for(state="visible")
        type_select.select_option(label=TYPE_LABEL[bt])
        method = page.locator(f"{SEL['method_select']}:visible")
        if method.count() > 0:
            method.first.select_option(label="通常")

        if bt.ordered and bt.size > 1:  # 馬単・３連単：着順ごとのラジオボタン
            for pos, horse in enumerate(bet.horses, start=1):
                self._tick(page, f"horse{pos}_no{horse}")
        else:  # 単勝・複勝・馬連・ワイド・３連複：チェックボックス
            for horse in bet.horses:
                self._tick(page, f"no{horse}")

        units = bet.amount // 100
        unit = page.locator(f"{SEL['unit']}:visible").first
        unit.fill(str(units))
        unit.press("Tab")
        page.locator(f"{SEL['set']}:visible").first.click()
        page.locator(f"{SEL['finish_input']}:visible").first.click()
        page.locator(SEL["cart"]).first.wait_for(state="visible")

    def _tick(self, page, element_id: str) -> None:
        inp = page.locator(f"input#{element_id}")
        if inp.count() != 1:
            raise VoteError(f"即PAT の馬番の選択欄（{element_id}）が見つかりませんでした")
        if inp.is_disabled():
            raise VoteError(f"即PAT で馬番（{element_id}）が選べません（取消・除外の可能性）")
        label = page.locator(f"label[for='{element_id}']")
        if label.count() > 0:
            label.first.click()
        else:
            inp.check(force=True)
        if not inp.is_checked():
            raise VoteError(f"即PAT で馬番（{element_id}）を選択できませんでした")

    # ------------------------------------------------------------------ 照合

    def _verify_cart(self, page, bet: ValidatedBet) -> dict[str, Any]:
        self._step = "購入予定リストの照合"
        rows = page.locator(SEL["cart_rows"])
        if rows.count() != 1:
            raise VoteError(f"購入予定リストの件数が想定と違います（{rows.count()} 件）。購入せずに中止しました")
        row = rows.first
        text = re.sub(r"\s+", "", row.locator("td.vote-content").inner_text())
        race = bet.race
        course_name = COURSE_BY_ALPHA[race.course][1]
        wd = WEEKDAYS[race.date.weekday()]
        ok = (
            course_name in text
            and re.search(rf"[（(]{wd}[）)]", text)
            and re.search(rf"(?<!\d){race.number}R", text)
            and TYPE_LABEL[bet.bet_type] in text
        )
        if not ok:
            raise VoteError(f"購入予定リストの内容（{text}）が買い目と一致しないため中止しました")

        combo = _digits(row.locator(".horse-combi-list").inner_text())
        expected = list(bet.horses)
        ok = combo == expected if bet.bet_type.ordered else sorted(combo) == sorted(expected)
        if not ok:
            raise VoteError(f"購入予定リストの馬番 {combo} が買い目 {expected} と一致しないため中止しました")

        unit_val = row.locator("input[ng-model='item.unit']").input_value().strip()
        if unit_val != str(bet.amount // 100):
            raise VoteError(f"購入予定リストの金額（{unit_val}00円）が買い目（{bet.amount}円）と一致しないため中止しました")
        total = _yen(page.locator(SEL["cart_total"]).first.inner_text())
        if total != bet.amount:
            raise VoteError(f"購入予定リストの合計金額（{total}円）が買い目（{bet.amount}円）と一致しないため中止しました")
        return {
            "course": course_name,
            "weekday": wd,
            "race_number": race.number,
            "bet_type": TYPE_LABEL[bet.bet_type],
            "horses": combo,
            "amount": int(unit_val) * 100,
            "total": total,
            "screen_text": text,
        }

    def _open_confirm_dialog(self, page, bet: ValidatedBet) -> None:
        self._step = "合計金額の入力と確認画面"
        total_input = page.locator(SEL["cart_total_input"]).first
        total_input.fill(str(bet.amount))
        total_input.press("Tab")
        page.locator(SEL["purchase"]).first.click()
        dialog = page.locator(SEL["dialog"]).first
        dialog.wait_for(state="visible")
        body = re.sub(r"\s+", " ", page.locator(SEL["dialog_body"]).first.inner_text())
        if CONFIRM_TEXT not in body:
            raise VoteError(f"購入確認ではなく次のメッセージが出たため中止しました: {body[:200]}")

    # ------------------------------------------------------------------ 確定

    def _confirm(self, page, bet: ValidatedBet) -> VoteReceipt:
        """最終確認。購入していないことが確実なら VoteError、送信した可能性があれば受付結果を返す。"""
        o = self.options
        dialog = page.locator(SEL["dialog"]).first
        if o.confirm == "auto":
            self._submitted = True
            page.locator(SEL["dialog_ok"]).first.click()
        else:
            logger.info("ipat: waiting for human to press OK")
            deadline = self.clock() + o.manual_confirm_timeout_sec
            # ここからは人間が OK を押した可能性がある
            self._submitted = True
            while dialog.is_visible():
                if self.clock() > deadline:
                    page.locator(SEL["dialog_cancel"]).first.click()
                    raise VoteError("購入の最終確認（OK）が時間内に押されなかったため、キャンセルしました")
                page.wait_for_timeout(500)
            # OK とキャンセルのどちらが押されたかは、完了表示が出るかと購入予定リストで判断する
            if not self._wait_result(page, timeout_sec=o.result_timeout_sec):
                if page.locator(SEL["cart_rows"]).count() == 1 and self._result_text(page) == "":
                    raise VoteError("購入の最終確認でキャンセルされたため、購入しませんでした")
                self._save_debug(page, "unconfirmed")
                return VoteReceipt(receipt="UNCONFIRMED", dry_run=False, confirmed=False)
            return self._read_receipt(page)

        if not self._wait_result(page, timeout_sec=o.result_timeout_sec):
            self._save_debug(page, "unconfirmed")
            return VoteReceipt(receipt="UNCONFIRMED", dry_run=False, confirmed=False)
        return self._read_receipt(page)

    def _wait_result(self, page, *, timeout_sec: int) -> bool:
        """購入完了の表示（受付票）が出るまで待つ。"""
        deadline = self.clock() + timeout_sec
        while self.clock() <= deadline:
            if self._result_text(page):
                return True
            page.wait_for_timeout(500)
        return False

    @staticmethod
    def _result_text(page) -> str:
        for key in ("ticket", "result"):
            loc = page.locator(SEL[key]).first
            if loc.count() and loc.is_visible():
                text = loc.inner_text()
                if "受付" in text:
                    return text
        return ""

    def _read_receipt(self, page) -> VoteReceipt:
        text = self._result_text(page)
        self._save_debug(page, "receipt")
        m = re.search(r"受付番号\D{0,10}(\d+)", text)
        if not m:
            return VoteReceipt(receipt="UNCONFIRMED", dry_run=False, confirmed=False)
        return VoteReceipt(receipt=f"IPAT-{m.group(1)}", dry_run=False, confirmed=True)

    # ------------------------------------------------------------------ 調査用の画面保存


    def _save_debug(self, page, kind: str) -> str | None:
        """失敗時・完了時の画面を加工して保存する（認証情報・残高などは消す）。保存先を返す。"""
        if self.options.debug_dir is None:
            return None
        try:
            from tools.capture_ipat import save_screen

            frames = [(f"0.{i}", fr.url, fr.content()) for i, fr in enumerate(page.frames)]
            d = save_screen(frames, self.options.debug_dir / datetime.now().strftime("%Y%m%d_%H%M%S_%f"), 1, kind)
            return str(d)
        except Exception:
            logger.exception("ipat: failed to save debug screen")
            return None

    # ------------------------------------------------------------------ 二段階投票（confirm = "external"）

    def prepare(self, bet: ValidatedBet, pending_id: str) -> PreparedVote:
        """確認ダイアログまで進めて止める。購入はしない。失敗したら VoteError（何も買っていない）。"""
        with self._lock:
            if self._sessions:
                raise VoteError("保留中の買い目があります。先に確定か取り消しをしてください")
        thread = _BrowserThread()
        timeout = self.options.step_timeout_ms / 1000 * 20

        def start():
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise VoteError("Playwright が入っていません（uv sync を実行してください）") from None
            pw = sync_playwright().start()
            try:
                browser, context, page = self._launch(pw)
            except BaseException:
                pw.stop()
                raise
            return pw, browser, context, page

        try:
            pw, browser, context, page = thread.call(start, timeout)
        except BaseException:
            thread.stop()
            raise
        session = _Session(thread=thread, pw=pw, browser=browser, context=context, page=page, bet=bet)

        def steps():
            try:
                self._login(page)
                self._open_basic_vote(page)
                self._select_race(page, bet)
                self._enter_bet(page, bet)
                cart = self._verify_cart(page, bet)
                self._open_confirm_dialog(page, bet)
            except VoteError:
                self._save_debug(page, "error")
                raise
            except Exception as e:
                self._save_debug(page, "error")
                logger.exception("ipat: unexpected error in prepare")
                raise VoteError(f"即PAT の「{getattr(self, '_step', '?')}」で想定外のエラーが起きたため、購入せずに中止しました（{type(e).__name__}）") from None
            return PreparedVote(cart=cart, capture_path=self._save_debug(page, "confirm"))

        try:
            prepared = thread.call(steps, timeout)
        except BaseException:
            self._close(session)
            raise
        with self._lock:
            self._sessions[pending_id] = session
        return prepared

    def commit(self, pending_id: str) -> VoteReceipt:
        """保留中の確認ダイアログで「OK」を押す。押す前の失敗は VoteError、押した後は例外を出さない。"""
        with self._lock:
            session = self._sessions.pop(pending_id, None)
        if session is None:
            raise VoteError("保留中の確認画面が残っていません（期限切れか、サーバの再起動）。購入していません")
        page = session.page

        def do():
            dialog = page.locator(SEL["dialog"]).first
            body = page.locator(SEL["dialog_body"]).first
            if not dialog.is_visible() or CONFIRM_TEXT not in body.inner_text():
                raise VoteError("即PAT の確認画面が閉じていたため、購入していません")
            session.submitted = True
            page.locator(SEL["dialog_ok"]).first.click()
            if not self._wait_result(page, timeout_sec=self.options.result_timeout_sec):
                self._save_debug(page, "unconfirmed")
                return VoteReceipt(receipt="UNCONFIRMED", dry_run=False, confirmed=False)
            return self._read_receipt(page)

        try:
            return session.thread.call(do, self.options.result_timeout_sec + 60)
        except VoteError:
            if session.submitted:  # 念のため（OK を押した後は VoteError を出さない）
                return VoteReceipt(receipt="UNCONFIRMED", dry_run=False, confirmed=False)
            raise
        except BaseException:
            if session.submitted:
                logger.exception("ipat: error after submit (external)")
                return VoteReceipt(receipt="UNCONFIRMED", dry_run=False, confirmed=False)
            logger.exception("ipat: error before submit (external)")
            raise VoteError("即PAT の操作中にエラーが起きたため、購入していません") from None
        finally:
            self._close(session)

    def abort(self, pending_id: str) -> None:
        """保留を破棄する（確認ダイアログでキャンセルして閉じる）。購入はしない。"""
        with self._lock:
            session = self._sessions.pop(pending_id, None)
        if session is None:
            return

        def do():
            cancel = session.page.locator(SEL["dialog_cancel"]).first
            if cancel.count() and cancel.is_visible():
                cancel.click()

        try:
            session.thread.call(do, 30)
        except BaseException:
            logger.exception("ipat: failed to cancel dialog (closing browser anyway)")
        finally:
            self._close(session)

    def has_pending(self, pending_id: str) -> bool:
        with self._lock:
            return pending_id in self._sessions

    @staticmethod
    def _close(session: _Session) -> None:
        def do():
            for closer in (session.context.close, session.browser.close, session.pw.stop):
                try:
                    closer()
                except Exception:
                    pass

        try:
            session.thread.call(do, 60)
        except BaseException:
            logger.exception("ipat: failed to close browser")
        finally:
            session.thread.stop()


def build_browser_voter(config) -> BrowserVoter:
    """config（AppConfig）から即PAT 投票を組み立てる。認証情報の取り出し方もここで決める（server.py には持ち込まない）。"""
    from credentials import build_credential_provider

    return BrowserVoter(
        build_credential_provider(config),
        IpatOptions(
            confirm=config.browser_confirm,
            headless=config.browser_headless,
            browser_channel=config.browser_channel,
            manual_confirm_timeout_sec=config.browser_manual_confirm_timeout_sec,
            debug_dir=config.browser_debug_dir,
            proxy_server=config.browser_proxy_server,
            tls_ca_file=config.browser_tls_ca_file,
            ignore_https_errors=config.browser_ignore_https_errors,
        ),
    )

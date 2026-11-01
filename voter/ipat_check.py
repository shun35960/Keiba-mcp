"""本物の即PAT で、確認画面まで進めて必ずキャンセルする確認ツール（人間専用。admin.py ipat-check から使う）。

- ログイン → 通常投票 → 場・レース → 券種・馬番・金額 → 購入予定リストの照合 → 確認画面 を、本物の画面で1つずつ確かめる
- **最後の「OK」は絶対に押さない。** CheckOnlyVoter は購入を確定する処理を持たず、呼ばれたら例外にする
- ガード層・投票の記録（DB）には触れない。購入しないので上限の計算にも影響しない
- 失敗した手順の画面は、加工（入力値・セッション情報・4桁以上の数字を消す）して captures/voter/ に保存する
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from guard import ValidatedBet
from models import BetType, Race, combination_key, normalize_horses, week_start
from voter.base import VoteError, VoteReceipt
from voter.browser import BrowserVoter, IpatOptions


class CheckOnlyVoter(BrowserVoter):
    """確認画面まで進めるだけの投票クラス。購入を確定する経路をすべて塞いでいる。"""

    def vote(self, bet: ValidatedBet) -> VoteReceipt:  # noqa: ARG002
        raise RuntimeError("ipat-check では購入しません（vote は使えません）")

    def commit(self, pending_id: str) -> VoteReceipt:  # noqa: ARG002
        raise RuntimeError("ipat-check では購入しません（commit は使えません）")

    def _confirm(self, page, bet):  # noqa: ARG002
        raise RuntimeError("ipat-check では購入しません（_confirm は使えません）")


@dataclass
class CheckResult:
    bet_type: BetType
    horses: tuple[int, ...]
    ok: bool
    message: str
    cart: dict | None = None
    capture_path: str | None = None


def sample_horses(race: Race, bet_type: BetType) -> tuple[int, ...]:
    """確認用の馬番。取消・除外でない馬を馬番の小さい順に必要な頭数だけ選ぶ。"""
    runners = sorted(race.runner_numbers())
    if len(runners) < bet_type.size:
        raise VoteError(f"{bet_type.label}を試すには出走馬が足りません")
    return tuple(runners[: bet_type.size])


def make_bet(race: Race, bet_type: BetType, horses: Iterable[int], amount: int = 100) -> ValidatedBet:
    h = normalize_horses(bet_type, tuple(horses))
    return ValidatedBet(
        race=race,
        bet_type=bet_type,
        horses=h,
        combination=combination_key(bet_type, h),
        amount=amount,
        week_start=week_start(race.date).isoformat(),
    )


def run_check(
    voter: CheckOnlyVoter,
    race: Race,
    bet_types: Iterable[BetType],
    *,
    amount: int = 100,
    pause_sec: float = 5.0,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> list[CheckResult]:
    """券種ごとに、確認画面まで進めてキャンセルする。購入はしない。"""
    if not isinstance(voter, CheckOnlyVoter):
        raise TypeError("ipat-check には CheckOnlyVoter を使ってください")
    results: list[CheckResult] = []
    types = list(bet_types)
    for i, bt in enumerate(types):
        if i:
            sleep(pause_sec)  # 続けてログインしすぎないよう間を空ける
        try:
            horses = sample_horses(race, bt)
            bet = make_bet(race, bt, horses, amount)
        except VoteError as e:
            results.append(CheckResult(bt, (), False, e.reason))
            out(f"[NG] {bt.label}: {e.reason}")
            continue
        label = f"{bt.label} {bet.combination} {amount}円"
        out(f"[..] {label}: 確認画面まで進めます（購入はしません）")
        pid = "check-" + secrets.token_hex(8)
        try:
            prepared = voter.prepare(bet, pid)
        except VoteError as e:
            results.append(CheckResult(bt, bet.horses, False, e.reason))
            out(f"[NG] {label}: {e.reason}")
            continue
        finally:
            # prepare が成功していても失敗していても、保留が残っていれば必ずキャンセルして閉じる
            if voter.has_pending(pid):
                voter.abort(pid)
        cart = prepared.cart
        shown = f"{cart.get('course')}（{cart.get('weekday')}）{cart.get('race_number')}R {cart.get('bet_type')} {cart.get('horses')} {cart.get('total')}円"
        results.append(CheckResult(bt, bet.horses, True, "確認画面まで進めてキャンセルしました", cart, prepared.capture_path))
        out(f"[OK] {label}: 確認画面に「{shown}」と表示 → キャンセルしました")
    return results


def build_check_voter(config) -> CheckOnlyVoter:
    """config から確認用の投票クラスを組み立てる。voter.mode に関係なく使える（購入しないため）。"""
    from credentials import build_credential_provider

    return CheckOnlyVoter(
        build_credential_provider(config),
        IpatOptions(
            confirm="external",  # prepare / abort だけを使う
            headless=config.browser_headless,
            browser_channel=config.browser_channel,
            debug_dir=config.browser_debug_dir,
            proxy_server=config.browser_proxy_server,
            tls_ca_file=config.browser_tls_ca_file,
            ignore_https_errors=config.browser_ignore_https_errors,
        ),
    )

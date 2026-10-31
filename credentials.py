"""認証情報（ネット投票の ID・暗証番号など）の取り出し。

- 認証情報はリポジトリ・config.toml・DB・ログに一切置かない
- 既定は Bitwarden Secrets Manager。`bws` CLI で、使う瞬間にだけ取り出す
- bws のアクセストークンは macOS のキーチェーンに保存し、実行時に読む（ファイルに平文で置かない）
- 取り出した値は Secret で包み、print やログに出しても伏せ字になるようにする
- MCP（AI）には渡さない。このモジュールを使うのは投票層（voter/browser.py）と admin.py だけ
"""

from __future__ import annotations

import json
import os
import subprocess
from abc import ABC, abstractmethod
from typing import Callable

# 投票に使う認証情報の名前（即PAT の場合）
CREDENTIAL_NAMES = ("inet_id", "subscriber_number", "pin", "pars_number")
CREDENTIAL_LABELS = {
    "inet_id": "INET-ID",
    "subscriber_number": "加入者番号",
    "pin": "暗証番号",
    "pars_number": "P-ARS番号",
}


class CredentialError(Exception):
    """認証情報を取り出せなかった。メッセージに認証情報の値は含めない。"""


class Secret:
    """秘密の値。表示すると伏せ字になる。値は reveal() でだけ取り出す。"""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(********)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:  # テスト用
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


class CredentialProvider(ABC):
    @abstractmethod
    def get(self, name: str) -> Secret:
        """name（CREDENTIAL_NAMES のいずれか）の値を取り出す。"""


Runner = Callable[..., subprocess.CompletedProcess]


def keychain_token(service: str, account: str, *, runner: Runner = subprocess.run) -> str:
    """macOS キーチェーンから bws のアクセストークンを読む。環境変数 BWS_ACCESS_TOKEN があればそちらを優先。"""
    token = os.environ.get("BWS_ACCESS_TOKEN")
    if token:
        return token
    try:
        p = runner(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CredentialError(f"キーチェーンを読めませんでした（{type(e).__name__}）") from None
    if p.returncode != 0 or not p.stdout.strip():
        raise CredentialError(
            f"キーチェーンに bws のアクセストークンが見つかりません（サービス名 {service}、アカウント名 {account}）"
        )
    return p.stdout.strip()


class BitwardenCredentialProvider(CredentialProvider):
    """Bitwarden Secrets Manager から `bws secret get <id>` で取り出す。"""

    def __init__(
        self,
        secret_ids: dict[str, str],
        token_source: Callable[[], str],
        *,
        bws_path: str = "bws",
        runner: Runner = subprocess.run,
    ):
        self.secret_ids = secret_ids
        self.token_source = token_source
        self.bws_path = bws_path
        self.runner = runner

    def get(self, name: str) -> Secret:
        label = CREDENTIAL_LABELS.get(name, name)
        secret_id = self.secret_ids.get(name, "").strip()
        if not secret_id:
            raise CredentialError(f"{label} の Secret ID が config.toml に設定されていません")
        env = {**os.environ, "BWS_ACCESS_TOKEN": self.token_source()}
        try:
            p = self.runner(
                [self.bws_path, "secret", "get", secret_id, "--output", "json"],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
        except FileNotFoundError:
            raise CredentialError("bws コマンドが見つかりません（brew install bws でインストールしてください）") from None
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CredentialError(f"{label} を取り出せませんでした（{type(e).__name__}）") from None
        if p.returncode != 0:
            # stderr には秘密は含まれないはずだが、念のため内容は出さない
            raise CredentialError(
                f"{label} を取り出せませんでした（bws の終了コード {p.returncode}）。"
                "Secret ID・アクセストークン・マシンアカウントの権限を確認してください"
            )
        try:
            value = json.loads(p.stdout)["value"]
        except (ValueError, KeyError, TypeError):
            raise CredentialError(f"{label} の取り出し結果を読めませんでした") from None
        if not isinstance(value, str) or not value:
            raise CredentialError(f"{label} の値が空です")
        return Secret(value)


def build_credential_provider(config) -> CredentialProvider:
    """config（AppConfig）から認証情報の取り出し方を組み立てる。"""
    if config.credentials_provider == "env":
        return EnvCredentialProvider()
    return BitwardenCredentialProvider(
        config.bws_secret_ids,
        lambda: keychain_token(config.bws_keychain_service, config.bws_keychain_account),
        bws_path=config.bws_path,
    )


class EnvCredentialProvider(CredentialProvider):
    """環境変数（KEIBA_INET_ID など）から取り出す。Bitwarden を使わない場合の代替。"""

    def __init__(self, prefix: str = "KEIBA_"):
        self.prefix = prefix

    def get(self, name: str) -> Secret:
        var = self.prefix + name.upper()
        value = os.environ.get(var)
        if not value:
            raise CredentialError(f"{CREDENTIAL_LABELS.get(name, name)} の環境変数 {var} が設定されていません")
        return Secret(value)

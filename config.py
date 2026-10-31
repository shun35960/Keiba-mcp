"""config.toml の読み込みと検証。読み込んだ設定は MCP ツールには一切公開しない。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"


@dataclass(frozen=True)
class GuardConfig:
    per_race_limit: int
    weekly_limit: int
    loss_stop_threshold: int
    bet_unit: int
    allowed_race_numbers: frozenset[int]  # 空なら全レース許可


@dataclass(frozen=True)
class AppConfig:
    guard: GuardConfig
    data_source: str
    dummy_instant_results: bool
    voter_mode: str
    db_path: Path
    jra_request_interval: float = 3.0
    jra_cache_path: Path = Path("cache.sqlite")
    jra_user_agent: str = "keiba-mcp (personal use)"
    credentials_provider: str = "bitwarden"
    bws_keychain_service: str = "keiba-mcp"
    bws_keychain_account: str = "bws-access-token"
    bws_path: str = "bws"
    bws_secret_ids: dict[str, str] = field(default_factory=dict)
    bws_token_file: Path | None = None
    browser_confirm: str = "manual"
    browser_manual_confirm_timeout_sec: int = 180
    browser_channel: str | None = "chrome"
    browser_headless: bool = False
    browser_debug_dir: Path | None = None
    browser_external_confirm_timeout_sec: int = 300
    browser_proxy_server: str | None = None
    browser_tls_ca_file: Path | None = None
    browser_ignore_https_errors: bool = False


def _positive_int(section: dict, key: str) -> int:
    v = section.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise ValueError(f"config.toml: guard.{key} は正の整数で指定してください（現在: {v!r}）")
    return v


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    """設定を読む。path 省略時は環境変数 KEIBA_CONFIG、なければリポジトリ直下の config.toml。"""
    p = Path(path or os.environ.get("KEIBA_CONFIG") or DEFAULT_CONFIG_PATH).resolve()
    with p.open("rb") as f:
        raw = tomllib.load(f)

    g = raw.get("guard", {})
    numbers = g.get("allowed_race_numbers", [])
    if not isinstance(numbers, list) or not all(isinstance(n, int) and 1 <= n <= 12 for n in numbers):
        raise ValueError("config.toml: guard.allowed_race_numbers は 1〜12 の整数の配列で指定してください")
    guard = GuardConfig(
        per_race_limit=_positive_int(g, "per_race_limit"),
        weekly_limit=_positive_int(g, "weekly_limit"),
        loss_stop_threshold=_positive_int(g, "loss_stop_threshold"),
        bet_unit=_positive_int(g, "bet_unit"),
        allowed_race_numbers=frozenset(numbers),
    )

    data = raw.get("data", {})
    voter = raw.get("voter", {})
    db = raw.get("db", {})
    db_path = Path(db.get("path", "db.sqlite"))
    if not db_path.is_absolute():
        db_path = p.parent / db_path

    jra = data.get("jra", {})
    interval = jra.get("request_interval_sec", 3)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval < 1:
        raise ValueError("config.toml: data.jra.request_interval_sec は 1 秒以上で指定してください（取得元に負荷をかけないため）")
    cache_path = Path(jra.get("cache_path", "cache.sqlite"))
    if not cache_path.is_absolute():
        cache_path = p.parent / cache_path

    cred = raw.get("credentials", {})
    provider = cred.get("provider", "bitwarden")
    if provider not in ("bitwarden", "env"):
        raise ValueError("config.toml: credentials.provider は \"bitwarden\" か \"env\" を指定してください")
    bw = cred.get("bitwarden", {})
    secret_ids = {str(k): str(v) for k, v in bw.get("secret_ids", {}).items()}
    token_file = str(bw.get("token_file", "") or "").strip()
    token_path = None
    if token_file:
        token_path = Path(token_file).expanduser()
        if not token_path.is_absolute():
            token_path = p.parent / token_path

    vb = voter.get("browser", {})
    confirm = vb.get("confirm", "manual")
    if confirm not in ("manual", "auto", "external"):
        raise ValueError("config.toml: voter.browser.confirm は \"manual\"・\"auto\"・\"external\" のいずれかを指定してください")
    if "proxy_username" in vb or "proxy_password" in vb:
        raise ValueError(
            "config.toml: プロキシのユーザー名・パスワードは config.toml に書かず、"
            "環境変数 KEIBA_PROXY_USERNAME / KEIBA_PROXY_PASSWORD（または HTTPS_PROXY の URL）で渡してください"
        )
    ca = str(vb.get("tls_ca_file", "") or "").strip()
    ca_path = None
    if ca:
        ca_path = Path(ca).expanduser()
        if not ca_path.is_absolute():
            ca_path = p.parent / ca_path
    external_timeout = vb.get("external_confirm_timeout_sec", 300)
    if not isinstance(external_timeout, int) or isinstance(external_timeout, bool) or external_timeout <= 0:
        raise ValueError("config.toml: voter.browser.external_confirm_timeout_sec は正の整数で指定してください")
    if voter.get("mode", "dry_run") not in ("dry_run", "browser"):
        raise ValueError("config.toml: voter.mode は \"dry_run\" か \"browser\" を指定してください")
    debug_dir = vb.get("debug_dir", "captures/voter")
    debug_path = None if not debug_dir else (Path(debug_dir) if Path(debug_dir).is_absolute() else p.parent / debug_dir)

    return AppConfig(
        guard=guard,
        data_source=data.get("source", "dummy"),
        dummy_instant_results=bool(data.get("dummy", {}).get("instant_results", False)),
        voter_mode=voter.get("mode", "dry_run"),
        db_path=db_path,
        jra_request_interval=float(interval),
        jra_cache_path=cache_path,
        jra_user_agent=str(jra.get("user_agent", "keiba-mcp (personal use)")),
        credentials_provider=provider,
        bws_keychain_service=str(bw.get("keychain_service", "keiba-mcp")),
        bws_keychain_account=str(bw.get("keychain_account", "bws-access-token")),
        bws_path=str(bw.get("bws_path", "bws")),
        bws_secret_ids=secret_ids,
        bws_token_file=token_path,
        browser_confirm=confirm,
        browser_manual_confirm_timeout_sec=int(vb.get("manual_confirm_timeout_sec", 180)),
        browser_channel=(vb.get("browser_channel", "chrome") or None),
        # 人間がブラウザで OK を押す manual のときは headless にしない（auto・external は画面が無くてもよい）
        browser_headless=bool(vb.get("headless", False)) and confirm in ("auto", "external"),
        browser_debug_dir=debug_path,
        browser_external_confirm_timeout_sec=external_timeout,
        browser_proxy_server=(str(vb.get("proxy_server", "") or "").strip() or None),
        browser_tls_ca_file=ca_path,
        browser_ignore_https_errors=bool(vb.get("ignore_https_errors", False)),
    )

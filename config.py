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
    )

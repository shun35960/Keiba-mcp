"""認証情報の取り出し（Bitwarden Secrets Manager / 環境変数）のテスト。本物の bws・キーチェーンは使わない。"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

import admin
from config import load_config
from credentials import (
    BitwardenCredentialProvider,
    CredentialError,
    EnvCredentialProvider,
    Secret,
    build_credential_provider,
    keychain_token,
)

IDS = {
    "inet_id": "11111111-1111-1111-1111-111111111111",
    "subscriber_number": "22222222-2222-2222-2222-222222222222",
    "pin": "33333333-3333-3333-3333-333333333333",
    "pars_number": "44444444-4444-4444-4444-444444444444",
}
VALUES = {IDS["inet_id"]: "FAKEINET", IDS["subscriber_number"]: "12345678", IDS["pin"]: "9999", IDS["pars_number"]: "8888"}


class FakeBws:
    def __init__(self, returncode=0, stdout=None):
        self.calls: list[dict] = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, args, **kw):
        self.calls.append({"args": args, "env_token": kw["env"].get("BWS_ACCESS_TOKEN")})
        sid = args[3]
        out = self.stdout if self.stdout is not None else json.dumps({"id": sid, "key": "k", "value": VALUES.get(sid, "")})
        return subprocess.CompletedProcess(args, self.returncode, stdout=out, stderr="error: something")


def provider(fake, ids=IDS):
    return BitwardenCredentialProvider(ids, lambda: "TOKEN-xyz", runner=fake)


def test_secret_is_masked():
    s = Secret("9999")
    assert "9999" not in repr(s) and "9999" not in str(s) and "9999" not in f"{s}"
    assert "9999" not in repr({"pin": s})
    assert s.reveal() == "9999"


def test_bitwarden_get(monkeypatch):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    fake = FakeBws()
    p = provider(fake)
    assert p.get("pin").reveal() == "9999"
    assert fake.calls[0]["args"] == ["bws", "secret", "get", IDS["pin"]]
    assert fake.calls[0]["env_token"] == "TOKEN-xyz"  # トークンは引数ではなく環境変数で渡す
    assert "TOKEN-xyz" not in fake.calls[0]["args"]


def test_bitwarden_missing_secret_id():
    with pytest.raises(CredentialError, match="暗証番号 の Secret ID"):
        provider(FakeBws(), ids={**IDS, "pin": ""}).get("pin")


def test_bitwarden_failure_shows_bws_error_line():
    with pytest.raises(CredentialError) as e:
        provider(FakeBws(returncode=2)).get("pin")
    assert "終了コード 2: error: something" in str(e.value)


def test_bitwarden_error_masks_token():
    class Leaky(FakeBws):
        def __call__(self, args, **kw):
            r = super().__call__(args, **kw)
            r.stderr = "error: bad token TOKEN-xyz"
            return r

    with pytest.raises(CredentialError) as e:
        provider(Leaky(returncode=1)).get("pin")
    assert "TOKEN-xyz" not in str(e.value) and "********" in str(e.value)


@pytest.mark.parametrize("bad", ["keiba-pin", " 3333", "'33333333-3333-3333-3333-333333333333'", "33333333333333333333333333333333"])
def test_bitwarden_rejects_non_uuid_secret_id(bad):
    fake = FakeBws()
    with pytest.raises(CredentialError, match="形式が正しくありません"):
        provider(fake, ids={**IDS, "pin": bad}).get("pin")
    assert fake.calls == []  # bws は呼ばない


@pytest.mark.parametrize("stdout", ["not json", json.dumps({"no_value": 1}), json.dumps({"value": ""})])
def test_bitwarden_bad_output(stdout):
    with pytest.raises(CredentialError):
        provider(FakeBws(stdout=stdout)).get("pin")


def test_bitwarden_bws_not_installed():
    def missing(*a, **k):
        raise FileNotFoundError("bws")

    with pytest.raises(CredentialError, match="bws コマンドが見つかりません"):
        provider(missing).get("pin")


def test_error_message_never_contains_value():
    # 値を取れた後にエラーになる経路は無いが、例外メッセージに値が入らないことを全ケースで確認
    for fake in (FakeBws(returncode=2), FakeBws(stdout="{}")):
        with pytest.raises(CredentialError) as e:
            provider(fake).get("pin")
        assert "9999" not in str(e.value)


def test_keychain_token(monkeypatch):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    calls = []

    def runner(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="tok-from-keychain\n", stderr="")

    assert keychain_token("keiba-mcp", "bws-access-token", runner=runner) == "tok-from-keychain"
    assert calls[0] == ["security", "find-generic-password", "-s", "keiba-mcp", "-a", "bws-access-token", "-w"]


def test_keychain_token_missing(monkeypatch):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    runner = lambda args, **kw: subprocess.CompletedProcess(args, 44, stdout="", stderr="not found")  # noqa: E731
    with pytest.raises(CredentialError, match="キーチェーン"):
        keychain_token("keiba-mcp", "bws-access-token", runner=runner)


def test_keychain_token_env_override(monkeypatch):
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "from-env")
    assert keychain_token("x", "y", runner=lambda *a, **k: pytest.fail("should not call")) == "from-env"


def test_env_provider(monkeypatch):
    monkeypatch.setenv("KEIBA_PIN", "1234")
    monkeypatch.delenv("KEIBA_INET_ID", raising=False)
    p = EnvCredentialProvider()
    assert p.get("pin").reveal() == "1234"
    with pytest.raises(CredentialError, match="KEIBA_INET_ID"):
        p.get("inet_id")


def test_repository_config_has_no_secret_values():
    """config.toml には Secret ID の欄だけがあり、値は空（または UUID）であること。"""
    cfg = load_config()
    assert cfg.credentials_provider == "bitwarden"
    assert set(cfg.bws_secret_ids) == {"inet_id", "subscriber_number", "pin", "pars_number"}
    import re

    for v in cfg.bws_secret_ids.values():
        assert v == "" or re.fullmatch(r"[0-9a-f-]{36}", v)


def test_build_provider(tmp_path):
    cfg_path = tmp_path / "c.toml"
    text = Path("config.toml").read_text(encoding="utf-8").replace('provider = "bitwarden"', 'provider = "env"')
    cfg_path.write_text(text, encoding="utf-8")
    assert isinstance(build_credential_provider(load_config(cfg_path)), EnvCredentialProvider)
    assert isinstance(build_credential_provider(load_config()), BitwardenCredentialProvider)


def test_admin_check_credentials_never_prints_values(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    assert admin.check_credentials(provider(FakeBws())) == 0
    out = capsys.readouterr().out + caplog.text
    assert "取り出しOK" in out
    for v in VALUES.values():
        assert v not in out
    assert admin.check_credentials(provider(FakeBws(), ids={**IDS, "pars_number": ""})) == 1


def test_credentials_not_reachable_from_mcp():
    """MCP サーバ（server.py）は認証情報のモジュールを読み込まない。"""
    src = Path("server.py").read_text(encoding="utf-8")
    assert "credentials" not in src


# ---------------------------------------------------------------- bws トークンの探す順番（環境変数 → ファイル → キーチェーン）

from credentials import resolve_bws_token  # noqa: E402


def _no_keychain(*a, **k):
    raise FileNotFoundError("security")  # Linux などキーチェーンの無い環境


def test_token_file(tmp_path, monkeypatch):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    f = tmp_path / "token"
    f.write_text("  tok-from-file\n", encoding="utf-8")
    assert resolve_bws_token("s", "a", token_file=f, runner=_no_keychain) == "tok-from-file"


def test_env_beats_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "tok-env")
    f = tmp_path / "token"
    f.write_text("tok-file", encoding="utf-8")
    assert resolve_bws_token("s", "a", token_file=f, runner=_no_keychain) == "tok-env"


def test_token_file_beats_keychain(tmp_path, monkeypatch):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    f = tmp_path / "token"
    f.write_text("tok-file", encoding="utf-8")
    called = []
    assert resolve_bws_token("s", "a", token_file=f, runner=lambda *a, **k: called.append(1)) == "tok-file"
    assert called == []  # キーチェーンは試さない


def test_keychain_used_when_no_env_or_file(monkeypatch):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    runner = lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="tok-kc\n", stderr="")  # noqa: E731
    assert resolve_bws_token("s", "a", token_file=None, runner=runner) == "tok-kc"


def test_no_keychain_environment_gives_helpful_error(monkeypatch):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    with pytest.raises(CredentialError, match="token_file"):
        resolve_bws_token("s", "a", token_file=None, runner=_no_keychain)


@pytest.mark.parametrize("content", ["", "   \n"])
def test_empty_token_file(tmp_path, monkeypatch, content):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    f = tmp_path / "token"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(CredentialError, match="空"):
        resolve_bws_token("s", "a", token_file=f, runner=_no_keychain)


def test_missing_token_file(tmp_path, monkeypatch):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    with pytest.raises(CredentialError, match="読めませんでした"):
        resolve_bws_token("s", "a", token_file=tmp_path / "nope", runner=_no_keychain)


def test_token_errors_never_contain_value(tmp_path, monkeypatch):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    f = tmp_path / "token"
    f.write_bytes(b"SECRET-TOKEN-\xff\xfe")  # 文字コード不正
    with pytest.raises(CredentialError) as e:
        resolve_bws_token("s", "a", token_file=f, runner=_no_keychain)
    assert "SECRET-TOKEN" not in str(e.value)


def test_config_token_file_relative_to_config(tmp_path):
    text = Path("config.toml").read_text(encoding="utf-8").replace('token_file = ""', 'token_file = "secrets/bws_token"')
    (tmp_path / "c.toml").write_text(text, encoding="utf-8")
    assert load_config(tmp_path / "c.toml").bws_token_file == tmp_path / "secrets" / "bws_token"

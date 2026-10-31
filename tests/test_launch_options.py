"""即PAT 投票のブラウザ起動設定（プロキシ・CA）のテスト。ブラウザは起動しない。"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from config import load_config
from voter.base import VoteError
from voter.browser import BrowserVoter, IpatOptions, build_launch_options, proxy_settings, spki_hashes


def make_ca(tmp_path: Path, n: int = 1) -> tuple[Path, list[str]]:
    pems, expected = [], []
    for i in range(n):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Test Proxy CA {i}")])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        pems.append(cert.public_bytes(serialization.Encoding.PEM))
        spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        expected.append(base64.b64encode(hashlib.sha256(spki).digest()).decode())
    f = tmp_path / "proxy-ca.pem"
    f.write_bytes(b"".join(pems))
    return f, expected


# ---------------------------------------------------------------- プロキシ


def test_no_proxy_by_default():
    launch, _ = build_launch_options(IpatOptions(), env={})
    assert "proxy" not in launch and "args" not in launch


def test_proxy_from_env():
    assert proxy_settings(None, {"HTTPS_PROXY": "http://proxy.local:3128"}) == {"server": "http://proxy.local:3128"}
    assert proxy_settings(None, {"https_proxy": "proxy.local:8080"}) == {"server": "http://proxy.local:8080"}


def test_config_proxy_beats_env():
    p = proxy_settings("http://cfg:1", {"HTTPS_PROXY": "http://env:2"})
    assert p == {"server": "http://cfg:1"}


def test_proxy_credentials_from_url_userinfo_are_split_out():
    p = proxy_settings(None, {"HTTPS_PROXY": "http://us%40er:p%3Ass@proxy.local:3128"})
    assert p == {"server": "http://proxy.local:3128", "username": "us@er", "password": "p:ss"}


def test_proxy_credentials_from_keiba_env():
    env = {"HTTPS_PROXY": "http://u:p@proxy.local:3128", "KEIBA_PROXY_USERNAME": "u2", "KEIBA_PROXY_PASSWORD": "p2"}
    assert proxy_settings(None, env) == {"server": "http://proxy.local:3128", "username": "u2", "password": "p2"}


def test_no_proxy_bypass():
    p = proxy_settings(None, {"HTTPS_PROXY": "http://proxy:1", "NO_PROXY": "localhost,127.0.0.1"})
    assert p["bypass"] == "localhost,127.0.0.1"


def test_invalid_proxy():
    with pytest.raises(VoteError, match="形式が不正"):
        proxy_settings("http://", {})


def test_launch_options_include_proxy():
    launch, context = build_launch_options(IpatOptions(proxy_server="http://proxy:3128"), env={})
    assert launch["proxy"] == {"server": "http://proxy:3128"}
    assert "ignore_https_errors" not in context


# ---------------------------------------------------------------- CA（SPKI の指紋）


def test_spki_hashes(tmp_path):
    f, expected = make_ca(tmp_path, 2)
    assert spki_hashes(f) == expected


def test_launch_options_pin_ca_spki(tmp_path):
    f, expected = make_ca(tmp_path)
    launch, context = build_launch_options(IpatOptions(tls_ca_file=f), env={})
    assert launch["args"] == [f"--ignore-certificate-errors-spki-list={expected[0]}"]
    assert "ignore_https_errors" not in context  # 検証の全面無効化はしない


def test_too_many_ca_certs_rejected(tmp_path):
    f, _ = make_ca(tmp_path, 6)
    with pytest.raises(VoteError, match="プロキシの CA だけ"):
        spki_hashes(f)


@pytest.mark.parametrize("content", [b"", b"not a cert"])
def test_bad_ca_file(tmp_path, content):
    f = tmp_path / "ca.pem"
    f.write_bytes(content)
    with pytest.raises(VoteError):
        spki_hashes(f)


def test_missing_ca_file(tmp_path):
    with pytest.raises(VoteError, match="読めませんでした"):
        spki_hashes(tmp_path / "nope.pem")


# ---------------------------------------------------------------- 検証の全面無効化（明示したときだけ・警告つき）


def test_ignore_https_errors_only_when_explicit(caplog):
    caplog.set_level(logging.WARNING)
    _, context = build_launch_options(IpatOptions(ignore_https_errors=True), env={})
    assert context["ignore_https_errors"] is True
    assert "ignore_https_errors" in caplog.text


# ---------------------------------------------------------------- Playwright への受け渡し（モック）


class FakePage:
    def set_default_timeout(self, ms):
        self.timeout = ms


class FakeContext:
    def new_page(self):
        return FakePage()


class FakeBrowser:
    def __init__(self):
        self.context_kwargs = None

    def new_context(self, **kw):
        self.context_kwargs = kw
        return FakeContext()


class FakeChromium:
    def __init__(self):
        self.launch_kwargs = None
        self.browser = FakeBrowser()

    def launch(self, **kw):
        self.launch_kwargs = kw
        return self.browser


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


def test_launch_passes_options_to_playwright(tmp_path, monkeypatch):
    for var in ("NO_PROXY", "no_proxy", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("KEIBA_PROXY_PASSWORD", "pw")
    monkeypatch.setenv("KEIBA_PROXY_USERNAME", "user")
    f, expected = make_ca(tmp_path)
    pw = FakePlaywright()
    v = BrowserVoter(credentials=None, options=IpatOptions(tls_ca_file=f, headless=True, browser_channel="chrome"))
    v._launch(pw)
    kw = pw.chromium.launch_kwargs
    assert kw["proxy"] == {"server": "http://proxy.local:3128", "username": "user", "password": "pw"}
    assert kw["args"] == [f"--ignore-certificate-errors-spki-list={expected[0]}"]
    assert kw["headless"] is True and kw["channel"] == "chrome"
    assert pw.chromium.browser.context_kwargs["locale"] == "ja-JP"


# ---------------------------------------------------------------- config


def _cfg(tmp_path, replace: dict[str, str]):
    text = Path("config.toml").read_text(encoding="utf-8")
    for a, b in replace.items():
        assert a in text
        text = text.replace(a, b)
    (tmp_path / "c.toml").write_text(text, encoding="utf-8")
    return load_config(tmp_path / "c.toml")


def test_config_rejects_proxy_password_in_file(tmp_path):
    with pytest.raises(ValueError, match="KEIBA_PROXY_PASSWORD"):
        _cfg(tmp_path, {'proxy_server = ""': 'proxy_server = ""\nproxy_password = "x"'})


def test_config_external_and_headless(tmp_path):
    c = _cfg(tmp_path, {'confirm = "manual"': 'confirm = "external"', "headless = false": "headless = true",
                        'tls_ca_file = ""': 'tls_ca_file = "ca.pem"', 'proxy_server = ""': 'proxy_server = "http://p:1"'})
    assert c.browser_confirm == "external" and c.browser_headless is True
    assert c.browser_tls_ca_file == tmp_path / "ca.pem" and c.browser_proxy_server == "http://p:1"
    assert c.browser_external_confirm_timeout_sec == 300


def test_config_manual_never_headless(tmp_path):
    assert _cfg(tmp_path, {"headless = false": "headless = true"}).browser_headless is False


def test_config_rejects_unknown_confirm(tmp_path):
    with pytest.raises(ValueError, match="external"):
        _cfg(tmp_path, {'confirm = "manual"': 'confirm = "yes"'})

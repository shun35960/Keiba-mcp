"""即PAT 画面保存スクリプトの加工処理のテスト（ブラウザは使わない）。"""

from __future__ import annotations

import json

from tools.capture_ipat import mask_numbers, sanitize_html, save_screen, strip_query, summarize

PAGE = """<html><head>
<meta name="csrf-token" content="SECRET-CSRF">
<script>var session = "SESSION-XYZ";</script>
<script src="/js/app.js?v=123&sid=SESSION-XYZ"></script>
</head><body>
<!-- comment with SESSION-XYZ -->
<form id="login" action="/login?sid=SESSION-XYZ">
  <input type="text" name="inetid" value="MYINETID">
  <input type="password" name="pin" value="9876">
  <input type="hidden" name="token" value="HIDDEN-TOKEN">
  <input type="radio" name="bet_type" value="tansho" checked>
  <input type="submit" value="ログイン">
  <textarea name="memo">私的メモ</textarea>
  <select name="race"><option value="11" selected>11R</option></select>
</form>
<div class="balance">購入限度額 12,345円 加入者番号 12345678</div>
<div class="race">東京 11R 馬番 7 100円</div>
<a href="/vote?sid=SESSION-XYZ#top" ng-click="next()">次へ</a>
<button ng-click="buy()">購入する</button>
</body></html>"""


def test_sanitize_removes_secrets():
    out = sanitize_html(PAGE)
    for secret in ("MYINETID", "9876", "HIDDEN-TOKEN", "SESSION-XYZ", "SECRET-CSRF", "私的メモ", "12,345", "12345678"):
        assert secret not in out, secret


def test_sanitize_keeps_structure():
    out = sanitize_html(PAGE)
    for keep in ('name="inetid"', 'name="pin"', 'type="password"', 'value="tansho"', 'value="ログイン"',
                 'ng-click="buy()"', "購入する", "11R", "馬番 7 100円", 'id="login"', 'action="/login?[removed]"',
                 "##,###円", 'src="/js/app.js?[removed]"'):
        assert keep in out, keep
    assert "selected" not in out


def test_strip_query():
    assert strip_query("https://x.jp/a/b?sid=1#f") == "https://x.jp/a/b?[removed]"
    assert strip_query("/a#frag") == "/a"
    assert strip_query("/a") == "/a"
    assert strip_query("javascript:void(0)") == "javascript:void(0)"


def test_mask_numbers():
    assert mask_numbers("残高 10,000円 番号 1234 馬番 12 金額 100円") == "残高 ##,###円 番号 #### 馬番 12 金額 100円"


def test_summarize_lists_controls():
    lines = summarize(sanitize_html(PAGE))
    text = "\n".join(lines)
    assert "FORM form#login" in text
    assert "[name='pin']" in text and "[type='password']" in text
    assert "ng-click='buy()'" in text and "「購入する」" in text
    assert "MYINETID" not in text


def test_save_screen(tmp_path):
    d = save_screen([("0.0", "https://www.ipat.jra.go.jp/pw_080_i.cgi?sid=SESSION-XYZ", PAGE)], tmp_path, 3, "最終 確認/画面")
    assert d.name == "03_最終_確認_画面"
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["frames"][0]["url"] == "https://www.ipat.jra.go.jp/pw_080_i.cgi?[removed]"
    saved = (d / "frame_0.0.html").read_text(encoding="utf-8") + (d / "summary.txt").read_text(encoding="utf-8")
    assert "SESSION-XYZ" not in saved and "9876" not in saved


def test_capture_tool_does_not_touch_credentials():
    from pathlib import Path

    src = Path("tools/capture_ipat.py").read_text(encoding="utf-8")
    assert "credentials" not in src.split('"""', 2)[2]  # docstring 以外で認証情報モジュールを使わない
    assert ".click(" not in src and ".fill(" not in src and ".type(" not in src  # 画面操作をしない

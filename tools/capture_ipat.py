"""即PAT の画面構造を保存する開発用スクリプト（あなたの Mac で実行する）。

全自動投票（voter/browser.py）を作るために、各画面の HTML 構造を保存する。

    uv run --group capture playwright install chromium   # 初回だけ（Google Chrome を使う場合は不要）
    uv run --group capture tools/capture_ipat.py          # Playwright の Chromium で開く
    uv run --group capture tools/capture_ipat.py --chrome # インストール済みの Google Chrome で開く

安全のための約束:
- このスクリプトは何もクリック・入力しない。ログインも画面の移動も、あなたが手で行う
- 認証情報は読まない・記録しない（credentials.py も使わない）
- 保存する前に、入力欄の値・hidden の値・インラインスクリプト・URL のクエリを消し、4桁以上の数字を伏せる
- スクリーンショットは撮らない（残高などが写るため）
- 保存先 captures/ は .gitignore 済み。リポジトリには入れない
- **購入ボタンは絶対に押さないこと**（最終確認画面は表示するだけ）
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, NavigableString

IPAT_URL = "https://www.ipat.jra.go.jp/"
OUT_ROOT = Path(__file__).resolve().parent.parent / "captures"

# 値を残してよい input の type（選択肢の識別に使うもの。利用者の入力値ではない）
KEEP_VALUE_TYPES = {"button", "submit", "reset", "radio", "checkbox", "image"}
# 4桁以上の数字・桁区切りの金額（例: 12345678, 10,000）を伏せる。馬番・レース番号・100円などは残る
_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d{4,}")
URL_ATTRS = ("href", "src", "action", "data-href", "data-url")


def strip_query(url: str) -> str:
    """URL からクエリとフラグメントを消す（セッション ID などが入ることがあるため）。"""
    if not url or url.startswith(("javascript:", "#", "data:")):
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query and not parts.fragment:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "[removed]" if parts.query else "", ""))


def mask_numbers(text: str) -> str:
    return _NUM_RE.sub(lambda m: re.sub(r"\d", "#", m.group(0)), text)


def sanitize_html(html: str) -> str:
    """共有しても安全なように HTML を加工する。構造（タグ・id・class・name・ラベル）は残す。"""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all("input"):
        itype = (tag.get("type") or "text").lower()
        if itype not in KEEP_VALUE_TYPES and tag.has_attr("value"):
            tag["value"] = ""  # placeholder は入力の説明なので残す
    for tag in soup.find_all("textarea"):
        tag.string = ""
    for tag in soup.find_all("option"):
        if tag.has_attr("selected"):
            del tag["selected"]
    for tag in soup.find_all("script"):
        if tag.get("src"):
            tag["src"] = strip_query(tag["src"])
            tag.string = ""
        else:
            tag.string = "/* inline script removed */"
    for tag in soup.find_all("meta"):
        # CSRF トークンなどを含む meta を消す
        name = (tag.get("name") or tag.get("property") or "").lower()
        if "csrf" in name or "token" in name:
            tag["content"] = ""
    for tag in soup.find_all(True):
        for attr in URL_ATTRS:
            if tag.has_attr(attr):
                tag[attr] = strip_query(str(tag[attr]))
        for attr in list(tag.attrs):
            if attr.lower() in ("data-token", "data-session", "data-csrf"):
                tag[attr] = ""
    for node in soup.find_all(string=True):
        if isinstance(node, Comment):
            node.extract()
            continue
        if isinstance(node, NavigableString) and node.parent and node.parent.name not in ("script", "style"):
            masked = mask_numbers(str(node))
            if masked != str(node):
                node.replace_with(masked)
    return str(soup)


def summarize(html: str) -> list[str]:
    """画面の操作部品（フォーム・入力欄・ボタン・リンク）を一覧にする。私が構造を把握するための要約。"""
    soup = BeautifulSoup(html, "html.parser")
    lines: list[str] = []

    def ident(t) -> str:
        head = t.name
        if t.get("id"):
            head += f"#{t['id']}"
        if t.get("class"):
            head += "." + ".".join(t["class"])
        bits = [head]
        for a in ("name", "type", "value", "ng-model", "ng-click", "onclick", "role", "aria-label", "title", "href"):
            if t.get(a):
                bits.append(f"[{a}={str(t[a])[:80]!r}]")
        text = re.sub(r"\s+", " ", t.get_text(" ", strip=True))[:60]
        if text:
            bits.append(f"「{text}」")
        return " ".join(bits)

    for form in soup.find_all("form"):
        lines.append("FORM " + ident(form))
    for t in soup.find_all(["input", "select", "textarea", "button"]):
        lines.append("  " + ident(t))
    for t in soup.find_all("a"):
        if t.get("href") or t.get("onclick") or t.get("ng-click"):
            lines.append("  " + ident(t))
    for t in soup.find_all(attrs={"ng-click": True}):
        if t.name not in ("a", "button", "input"):
            lines.append("  " + ident(t))
    for t in soup.find_all("iframe"):
        lines.append("IFRAME " + ident(t))
    return lines


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w\-]+", "_", s.strip())[:40] or "screen"


def save_screen(page_frames: list[tuple[str, str, str]], out_dir: Path, index: int, label: str) -> Path:
    """page_frames: [(ページ番号.フレーム番号, URL, HTML)]。加工してから保存する。"""
    d = out_dir / f"{index:02d}_{_safe_name(label)}"
    d.mkdir(parents=True, exist_ok=True)
    meta = {"label": label, "saved_at": datetime.now().isoformat(timespec="seconds"), "frames": []}
    summary: list[str] = []
    for key, url, html in page_frames:
        clean = sanitize_html(html)
        fname = f"frame_{key}.html"
        (d / fname).write_text(clean, encoding="utf-8")
        meta["frames"].append({"file": fname, "url": strip_query(url)})
        summary.append(f"===== {fname}  {strip_query(url)}")
        summary.extend(summarize(clean))
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="即PAT の画面構造を保存する（クリック・入力はしない）")
    ap.add_argument("--chrome", action="store_true", help="インストール済みの Google Chrome を使う")
    ap.add_argument("--url", default=IPAT_URL, help="最初に開く URL")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright  # 開発用の依存（--group capture）

    out_dir = OUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    print(__doc__.split("安全のための約束:")[1])
    print(f"保存先: {out_dir}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome" if args.chrome else None)
        context = browser.new_context(locale="ja-JP")
        page = context.new_page()
        page.goto(args.url)
        print("ブラウザでログインし、保存したい画面を表示してください。")
        print("保存する画面の例: ログイン後のトップ / レース選択 / 券種・馬番選択 / 金額入力 / 最終確認（購入は押さない）")
        index = 1
        while True:
            try:
                label = input(f"\n[{index}] 画面の名前を入力して Enter で保存（何も入力せず Enter で終了）: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not label:
                break
            frames: list[tuple[str, str, str]] = []
            for pi, pg in enumerate(context.pages):
                for fi, fr in enumerate(pg.frames):
                    try:
                        frames.append((f"{pi}.{fi}", fr.url, fr.content()))
                    except Exception as e:  # 読み込み中のフレームなど
                        print(f"  フレーム {pi}.{fi} は保存できませんでした（{type(e).__name__}）")
            d = save_screen(frames, out_dir, index, label)
            print(f"  保存しました: {d}（フレーム {len(frames)} 個）")
            index += 1
        context.close()
        browser.close()

    if index > 1:
        print(f"\n保存先: {out_dir}")
        print("共有する前に、中身（特に summary.txt）を見て、気になる情報があれば消してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# 即PAT の偽サイト（テスト用）

`voter/browser.py` をブラウザで通しテストするための、即PAT と同じ部品（name・id・ng-click などの属性）を持つ**架空の**サイト。
本物の即PAT のページをコピーしたものではない。本物の画面構造が変わったら、`tools/capture_ipat.py` で確認してから、ここと `voter/browser.py` を合わせて直す。

URL のクエリでテスト用の動きを切り替える（login1.html → login2.html → menu.html に引き継がれる）:

- `prefill=1` 最初から購入予定リストに買い目がある
- `wrongunit=1` 購入予定リストの金額を間違えて表示する
- `noticket=1` OK を押しても完了表示（受付票）を出さない
- `autook=ミリ秒` 確認ダイアログが出てから指定時間後に OK を押す（人間の操作のまね）
- `autocancel=ミリ秒` 同じくキャンセルを押す
- `notice=1` ログイン後に約定の確認画面を出す

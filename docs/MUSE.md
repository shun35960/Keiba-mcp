# Muse（VM 上のエージェント）向け 手引き

keiba-mcp を Linux の VM から使うための準備と、二段階投票（`confirm = "external"`）の運用手順。
全体の設計は README.md、守るべきルールは CLAUDE.md を参照。

---

## 0. いちばん大事な約束

1. **`confirm_vote` は、人間（ユーザー）がその買い目の購入をチャットで承認した後にだけ呼ぶ。**
   承認が取れていない・あいまい・返事が無いときは呼ばない。プログラムはこれを強制できないので、Muse が必ず守る。
2. **承認されなかったら `cancel_vote` を呼ぶ。** 放っておいても有効期限で自動的に取り消されるが、明示的に取り消す。
3. **同じ買い目を自分から出し直さない。** 「購入できたか分からない」ときは、再送信せず人間に投票履歴の確認を頼む（二重購入の防止）。
4. **認証情報・トークンの値を、人間に聞かない・表示しない・ログに残さない。**
5. **`config.toml` の上限額・停止ライン・`voter.mode`・`confirm` を変えない。** 変えるのは人間だけ。

---

## 1. VM の準備（最初の1回）

### 1-1. リポジトリと依存関係

```bash
git clone https://github.com/shun35960/Keiba-mcp.git
cd Keiba-mcp
uv sync
uv run playwright install chromium   # Google Chrome が無い VM では Playwright の Chromium を使う
```

### 1-2. bws（Bitwarden Secrets Manager の CLI）

```bash
sudo mkdir -p /usr/local/bin          # インストール先が無い場合
curl https://bws.bitwarden.com/install | sh
bws --version
```

### 1-3. bws のアクセストークン（キーチェーンの代わりにファイル）

人間がマシンアカウントのアクセストークンを、**リポジトリの外**のファイルに置く（Muse は値を見ない）。

```bash
mkdir -p ~/.config/keiba-mcp
# 人間がトークンを書き込む
chmod 600 ~/.config/keiba-mcp/bws_token
```

探す順番は **環境変数 `BWS_ACCESS_TOKEN` → `token_file` → macOS キーチェーン**。

### 1-4. 手元の `config.toml` を VM 用に書き換える（コミットしない）

```toml
[credentials.bitwarden]
token_file = "~/.config/keiba-mcp/bws_token"

[voter]
mode = "browser"                 # 実際に購入する（リポジトリの既定は "dry_run"）

[voter.browser]
confirm = "external"             # 二段階投票
external_confirm_timeout_sec = 300
headless = true                  # 画面の無い VM ではヘッドレス
browser_channel = ""             # Playwright の Chromium を使う
proxy_server = ""                # 空なら環境変数 HTTPS_PROXY / https_proxy を使う
tls_ca_file = "/path/to/proxy-ca.pem"   # TLS を中継するプロキシの CA だけを入れたファイル（最大5個）
```

- プロキシのユーザー名・パスワードは `config.toml` に書かない（書くと起動時にエラー）。必要なら環境変数 `KEIBA_PROXY_USERNAME` / `KEIBA_PROXY_PASSWORD`、または `HTTPS_PROXY=http://user:pass@host:port` で渡す。
- `tls_ca_file` には **プロキシの CA だけ** を入れる。OS の証明書束（`/etc/ssl/certs/ca-certificates.crt` など）をそのまま指定すると拒否される（全部を無条件に信頼してしまうため）。
  Chromium は CA ファイルを直接読めないので、この CA の公開鍵の指紋（SPKI）だけを信頼させている。
- `git pull` で `config.toml` がぶつかったら `git stash` → `git pull` → `git stash pop`。

### 1-5. 動作確認

```bash
uv run admin.py check-credentials    # 4つとも「取り出しOK」になること（値は表示されない）
uv run admin.py status               # 投票: 受付中 になっていること
uv run pytest                        # すべて通ること（本物の即PAT には接続しない）
```

### 1-6. MCP サーバとして登録

使っている MCP クライアントに、次のコマンドを stdio サーバとして登録する。

```bash
uv run --directory /path/to/Keiba-mcp server.py
```

`config.toml` を別の場所に置く場合は、環境変数 `KEIBA_CONFIG` にそのパスを渡す。

---

## 2. 使えるツール

| ツール | 用途 |
| --- | --- |
| `get_race_card(date, course?, race_number?)` | 出馬表。`race_number` を付けると近走成績・血統つき |
| `get_odds(race_id, bet_type?, limit?)` | オッズ（既定は単勝・複勝） |
| `place_bet(race_id, bet_type, horses, amount, note?)` | 買い目を出す。external では**購入せず保留**にして `pending_id` を返す |
| `confirm_vote(pending_id)` | 保留を確定（即PAT で購入）。**人間の承認後にだけ** |
| `cancel_vote(pending_id)` | 保留を取り消す（購入しない） |
| `get_race_result(race_id)` | 結果を取得して精算 |
| `settle_pending_bets()` | 未精算をまとめて精算 |
| `get_stats(since?, until?)` | 成績（回収率・的中率など） |

- `race_id` の形式：`YYYYMMDD-競馬場コード-R番号`（例 `20261108-TOKYO-11`）
- 券種：`win`(単勝)・`place`(複勝)・`quinella`(馬連)・`wide`(ワイド)・`exacta`(馬単)・`trio`(3連複)・`trifecta`(3連単)。馬単・3連単は着順どおりに並べる
- 投票できるのは 11R のみ。金額は 100円単位。上限額は Muse には分からない（超えると理由つきで拒否される）

---

## 3. 二段階投票の手順

```
get_race_card / get_odds で検討
        │
place_bet(..., note="理由")
        │  ガード層で検証 → 金額を仮押さえ → 即PAT の確認画面で止める（まだ買っていない）
        ▼
status: "pending_confirmation", pending_id, confirmation_screen, expires_at
        │
人間に承認を求める（下の「承認を求めるときの書き方」）
        │
   ┌────┴─────┐
 承認された     承認されない／返事が無い
   │              │
confirm_vote     cancel_vote
   │
confirmed: true → 受付番号を人間に報告
```

### 承認を求めるときの書き方

`confirmation_screen`（即PAT の画面から読み取り、ガード層の内容と照合済み）をそのまま示す。

> 即PAT の確認画面まで進めました。**まだ購入していません。**
> - 東京（日）11R 単勝 7番 300円（合計 300円）
> - 理由：〇〇
> - 期限：15:32 まで（過ぎると自動で取り消し）
>
> この内容で購入してよいですか？「購入して」と返事をもらえたら確定します。

- 承認とみなすのは、人間がこの買い目に対して**はっきり購入を了承した**返事だけ。
- 期限（`expires_at`）を過ぎた後に承認が来たら、`confirm_vote` は拒否される。必要なら `place_bet` からやり直す（そのときも改めて承認を取る）。

### 結果の扱い

| 返ってきたもの | 意味 | Muse がすること |
| --- | --- | --- |
| `place_bet` → `accepted: false` | 購入していない（ガード層・即PAT の画面で中止） | `reason` を人間に伝える。直せる内容なら直して出し直してよい |
| `place_bet` → `status: "pending_confirmation"` | 確認画面で止めている。**未購入** | 人間に承認を求める |
| `confirm_vote` → `confirmed: true` | 購入できた | 受付番号（`receipt`）を報告 |
| `confirm_vote` → `confirmed: false` かつ `warning` あり | 送信したが成立を確認できない。**投票は自動停止** | 再送信しない。人間に「即PAT の投票履歴を確認して、`uv run admin.py resume --note "投票履歴を確認"` で再開して」と頼む |
| `confirm_vote` → `confirmed: false`（`warning` なし） | 期限切れ・取り消し済み・不明な ID・二重確定など。**未購入** | `reason` を人間に伝える |
| `place_bet` → 「保留中の買い目があります」 | 保留は同時に1件だけ | 先に確定か取り消しをする |
| 「自動停止中」 | 損失が停止ラインに達した、または結果不明の投票がある | 投票しない。人間に伝える（再開は人間が `admin.py` で行う） |

### 知っておくこと

- 保留中の金額は、上限・重複・損失の計算にすでに入っている（取り消すと解放される）。
- サーバが再起動すると保留は消え、「未購入」として解放される。再送信はされない。
- 失敗時・確認時の画面は、加工（入力値・セッション情報・4桁以上の数字を消す）して `captures/voter/` に保存される。不具合の報告に使う。

---

## 4. 困ったとき

| 症状 | 確認すること |
| --- | --- |
| `check-credentials` で「キーチェーンを使えない環境」 | `token_file` のパス、または `BWS_ACCESS_TOKEN` |
| `bws の終了コード …` | Secret ID が UUID か、マシンアカウントに `keiba-mcp` プロジェクトの読み取り権限があるか |
| ブラウザを起動できない | `uv run playwright install chromium` 済みか、`browser_channel = ""` か |
| `ERR_CERT_AUTHORITY_INVALID` など証明書のエラー | `tls_ca_file` にプロキシの CA が入っているか（OS の証明書束ではなく CA だけ） |
| 「tls_ca_file に証明書が N 個あります」 | プロキシの CA だけを取り出したファイルにする |
| 即PAT の画面で中止される | `reason` と `captures/voter/` の保存画面を人間に渡す（画面の構造が変わった可能性） |

# keiba-mcp

競馬の買い目をAIが決めて投票までつなぐための MCP サーバ。AI（Claude 等）はツール経由でデータを読み、買い目を送るだけ。認証情報・上限額・停止条件はすべてサーバ側が持ち、AIには渡さない。

**開発フェーズでは実購入を行わない（ドライランのみ）。** 実投票への接続は、検証結果を見てから別途判断する。

## 目的と方針

- AI が出馬表・オッズを読み、買い目（レース・券種・馬番・金額）を決める
- MCP サーバが買い目を受け取り、ガード層で検証してから投票層に渡す
- 投票層は差し替え可能にする。最初は記録だけの `dry_run`、後から `browser`（Playwright）
- 控除率（約20〜25%）があるため、回収率100%超えは簡単ではない。**ドライランで十分に検証してから**実弾を検討する

## 構成

```
keiba-mcp/
├─ README.md
├─ pyproject.toml
├─ config.toml        # 上限値などの固定設定（AIから変更不可）
├─ server.py          # MCPサーバ本体（MCPServer、ツール定義）
├─ guard.py           # ガード層（上限額・停止条件・重複拒否）
├─ db.py              # SQLite（買い目・結果・損益の記録）
├─ models.py          # 共通の型（券種・race_id・週の区切り・レース・結果）
├─ config.py          # config.toml の読み込みと検証
├─ stats.py           # 成績集計（回収率・的中率・収支）
├─ report.py          # 成績レポート（人間向け CLI）
├─ data/
│  ├─ base.py         # データ取得のインターフェース
│  ├─ dummy.py        # ダミーデータ（最初はこれ）
│  ├─ scraper.py      # JRA 公式サイトからの取得（間隔制限・キャッシュつき）
│  └─ jra_parse.py    # JRA のページ（HTML）の解析
├─ voter/
│  ├─ base.py         # 投票のインターフェース
│  ├─ dry_run.py      # 記録だけ（最初はこれ）
│  └─ browser.py      # Playwright 投票（後で実装、今はスタブ）
├─ tests/
└─ db.sqlite          # 実行時に生成（git 管理しない）
```

## MCP ツール

| ツール | 内容 |
| --- | --- |
| `get_race_card(date, course?, race_number?)` | 指定日の出馬表（レース一覧・出走馬）を返す。`race_number` を指定すると近走成績・血統つきの詳細版 |
| `get_odds(race_id)` | 指定レースの券種別オッズを返す |
| `place_bet(race_id, bet_type, horses, amount, note?)` | 買い目を送る。ガード層を通過したものだけ投票層へ。結果（受理/拒否と理由）を返す。`note` は買い目の理由（成績の振り返り用） |
| `get_race_result(race_id)` | 結果（着順・払戻）をデータ層から取得し、そのレースの買い目を精算して返す |
| `settle_pending_bets()` | 未精算の買い目があるレースの結果をまとめて取得・精算する |
| `get_stats(since?, until?)` | 成績（回収率・的中率・収支・最大ドローダウン）を全体・券種別・週別・投票時オッズ帯別に返す |
| `confirm_vote(pending_id)` | 二段階投票で保留中の買い目を確定する（即PAT で購入）。**人間の承認が取れた後にだけ呼ぶ** |
| `cancel_vote(pending_id)` | 二段階投票で保留中の買い目を取り消す（購入しない） |

`place_bet` は常にガード層を通し、拒否理由は AI に分かる文言で返す。

- `get_odds(race_id, bet_type?, limit?)`：`bet_type` 省略時は単勝・複勝。組み合わせ券種は人気順に `limit` 件（最大500）
- `get_race_result` の結果は**必ずデータ層から取る**。AI から結果を受け取る経路は作らない（損益をごまかせないように）
- `race_id` の形式：`YYYYMMDD-競馬場コード-R番号`（例 `20261101-TOKYO-11`）
- 券種：`win`(単勝)・`place`(複勝)・`quinella`(馬連)・`wide`(ワイド)・`exacta`(馬単)・`trio`(3連複)・`trifecta`(3連単)。日本語名でも可。枠連は扱わない

## ガード層（最重要）

`config.toml` に固定し、MCP ツールからは**読み取りも変更も不可**にする。

- 1レースあたりの上限額（500円。この範囲で金額は AI が判断する）
- 1週間あたりの上限額（1,000円）。週は**火曜〜翌月曜**で区切る（土日と祝日の月曜開催を同じ週に入れるため）
- 損失が停止ライン（3,000円）に達したら自動停止（打ち止め。以降の `place_bet` は全拒否）。停止は DB に記録され、精算や再起動では解除されない
  - 再開は人間が補充して行う：`uv run admin.py resume --note "3000円補充"`。補充した時点の損失を基準にし、そこから新たに停止ラインまで投票できる
  - 状態の確認は `uv run admin.py status`、補充の履歴は `uv run admin.py history`。これらは人間専用で、MCP（AI）からは使えない
  - 未精算の投票は全額損失として数える。外れたら停止ラインを超える投票も拒否する
- 投票対象のレース番号を制限（11R＝メインのみ）
- 同一レース・同一買い目の重複投票を拒否（馬連・ワイド・3連複は馬番の順不同で判定、馬単・3連単は順番どおり）
- 金額は 100 円単位など、券種ごとの頭数・馬番の重複などの妥当性チェック
- 存在しない race_id・馬番（取消・除外を含む）の拒否
- 締切（発走2分前）を過ぎたレースの拒否

拒否理由には上限額などの設定値そのものは含めない。

AI の出力は信用せず、必ずここで検証する。

## 認証情報の扱い

- ID・パスワード・暗証番号は**シークレットストア（Bitwarden Secrets Manager）または環境変数のみ**に置く
- リポジトリにコミットしない（`.env` は `.gitignore`）
- AI（MCP クライアント）には一切渡さない。`place_bet` の引数・戻り値にも含めない
- ログにも出さない
- 取り出した値は `credentials.Secret` で包み、表示しても伏せ字になるようにする

### Bitwarden Secrets Manager の準備（Mac）

1. Bitwarden で Secrets Manager を有効にする（無料プラン：2ユーザー・3プロジェクト・3マシンアカウントまで）
2. プロジェクト `keiba-mcp` を作り、シークレットを4つ登録する：INET-ID・加入者番号・暗証番号・P-ARS番号
3. マシンアカウントを作り、`keiba-mcp` プロジェクトに**読み取り専用**の権限を付けて、アクセストークンを発行する
4. `bws` CLI をインストールする（Homebrew には無い）：`curl https://bws.bitwarden.com/install | sh`
   または [GitHub のリリースページ](https://github.com/bitwarden/sdk-sm/releases) から Mac 用をダウンロードする。
   Claude Code から起動したサーバで見つからない場合は、`which bws` のフルパスを `config.toml` の `bws_path` に書く
5. アクセストークンを macOS のキーチェーンに保存する（実行するとトークンの入力を求められる）：
   `security add-generic-password -s keiba-mcp -a bws-access-token -w`
6. 4つのシークレットの ID（UUID）を `config.toml` の `[credentials.bitwarden.secret_ids]` に書く。ID 自体は秘密ではない
7. 取り出せるか確認する（値は表示されない）：`uv run admin.py check-credentials`

キーチェーンの無い環境（Linux の VM など）では、アクセストークンを書いたファイルを `config.toml` の `credentials.bitwarden.token_file` に指定する（リポジトリの外に置き、`chmod 600`）。環境変数 `BWS_ACCESS_TOKEN` でも渡せる。探す順番は **環境変数 → token_file → キーチェーン**。Linux への bws のインストールは `curl https://bws.bitwarden.com/install | sh`（インストール先が無ければ先に `sudo mkdir -p /usr/local/bin`）。

Bitwarden を使わない場合は `config.toml` の `credentials.provider` を `"env"` にして、環境変数 `KEIBA_INET_ID`・`KEIBA_SUBSCRIBER_NUMBER`・`KEIBA_PIN`・`KEIBA_PARS_NUMBER` に置く。

## 実装方針

- 言語: Python 3.12+
- MCP: 公式 Python SDK（mcp 2.x。旧 FastMCP は `MCPServer` に改名されている）
- パッケージ管理: uv
- データ取得: httpx + BeautifulSoup（JRA の JRADB ページは JavaScript なしで取れるため Playwright は使わない）
- ブラウザ操作（実投票、未実装）: Playwright
- 記録: SQLite
- テスト: pytest（ガード層は特に網羅する）

## 実装ステップ

1. プロジェクト雛形（pyproject.toml、ディレクトリ、`.gitignore`）
2. `db.py` と `config.toml`、`guard.py`（単体テスト付き）
3. `data/base.py` と `data/dummy.py`
4. `voter/base.py` と `voter/dry_run.py`
5. `server.py` で3ツールを公開し、MCP クライアントから動作確認
6. `data/scraper.py` で実データ取得。取得元の規約確認・アクセス間隔の制限・キャッシュを必ず入れる
7. ドライランで買い目と結果を蓄積し、回収率を集計する仕組みを作る
8. （検証後・別判断）`voter/browser.py` の実装

**ステップ1〜8は実装済み。** 実投票は `config.toml` で人間が有効にしたときだけ動く。

## 実データの取得（ステップ6）

取得元は JRA 公式サイト（`www.jra.go.jp/JRADB/`）。

- 利用規約（[ご利用に際して](https://www.jra.go.jp/use/)）を確認済み。自動取得を禁止する記載はなく、robots.txt も全ページ許可（2026年10月時点）。掲載内容は JRA の著作物なので**私的使用の範囲**で使い、取得したデータを再配布しない
- リクエストの間隔は最低 3 秒（`config.toml` の `data.jra.request_interval_sec`、1 秒未満は設定不可）
- 取得したページは `cache.sqlite` にキャッシュする。有効期限は開催一覧 10 分、レース一覧 5 分、出馬表 10 分、オッズ 1 分、結果 5 分
- 出馬表は開催週の分だけ取得できる（JRA の出馬表ページに載っている範囲）。結果は過去の開催も取得できる
- 出走取消・競走除外の馬を含む買い目は、精算時に返還（払戻＝投票額）として扱う
- ページの構造が変わったら `data/jra_parse.py` を直す。テストは架空データの見本 HTML（`tests/fixtures/jra/`）で行い、ネットワークには出ない

## 使い方

```bash
uv sync                 # 依存関係のインストール
uv run pytest           # テスト
uv run server.py        # MCP サーバ起動（stdio）
```

Claude Code に登録する例:

```bash
claude mcp add keiba -- uv run --directory /path/to/keiba-mcp server.py
```

別の設定ファイルを使う場合は環境変数 `KEIBA_CONFIG` にパスを指定する。

## 成績の集計（ステップ7）

- 投票時に、そのときのオッズ（サーバがデータ層から取得）と AI が書いた理由（`note`）を記録する
- 回収率 = 払戻 ÷ 投票額、的中率 = 的中した買い目 ÷ 買い目。どちらも**精算済み**の買い目だけで計算し、出走取消・競走除外による返還は集計から外す
- 人間はターミナルでレポートを見られる

```bash
uv run report.py --settle               # 未精算を精算してから全期間の成績を表示
uv run report.py --since 2026-11-01     # 期間を指定
```

## 実投票（ステップ8、即PAT）

`config.toml` の `[voter]` を書き換えると、即PAT で実際に購入する。

```toml
[voter]
mode = "browser"        # "dry_run" に戻せばドライラン

[voter.browser]
confirm = "manual"      # 最後の「OK」は人間が押す（"auto" にするとプログラムが押す）
```

- 事前に `uv sync` と `uv run admin.py check-credentials` を済ませておく
- `place_bet` が受理されると Google Chrome が開き、ログイン → レース選択 → 券種・馬番・金額の入力 → 購入予定リストの照合 → 合計金額の入力 → 「購入する」まで自動で進む。`manual` では確認ダイアログで止まるので、内容を見て **OK かキャンセルを押す**（3分以内。過ぎるとキャンセル）
- 購入予定リストに別の買い目が残っている・内容がガード層の内容と一致しない・約定の確認画面が出た、などのときは購入せずに中止し、理由を AI に返す
- 「OK」の後に完了表示（受付票）を確認できなかったときは、投票を記録したうえで**自動停止**する。即PAT の投票履歴を確認してから `uv run admin.py resume --note "投票履歴を確認"` で再開する
- 失敗時・完了時の画面は加工して `captures/voter/` に保存する（不具合の調査用）

### 二段階投票（`confirm = "external"`、画面の無い VM 上のエージェント向け）

目の前でブラウザを操作する人がいない環境で、チャットなどで人間の承認を取ってから購入するためのモード。

1. `place_bet` → ガード層で検証し、金額を**仮押さえ**（上限・重複・損失に数える）。即PAT の確認画面まで進めて止め、`status: "pending_confirmation"`・`pending_id`・確認画面から読み取った内容（ガード層の内容と照合済み）・加工済みの確認画面の保存先・有効期限を返す。**まだ購入していない**
2. 人間が承認したら `confirm_vote(pending_id)` → 「OK」を押して購入し、受付番号を返す。承認されなければ `cancel_vote(pending_id)` → 取り消し（仮押さえを解放）
3. 有効期限（`external_confirm_timeout_sec`、既定300秒。締切が先ならそこまで）を過ぎた保留は自動で取り消す。期限切れ・不明な `pending_id`・二重確定は拒否する
4. 保留は同時に1件だけ（同じアカウントで複数のブラウザからログインしないため）
5. サーバが再起動して保留が消えたら「未購入」として解放し、**再送信しない**。`confirm_vote` の途中で落ちた場合は結果不明として投票を停止する（人間が投票履歴を確認して `admin.py resume`）

**承認の確認は MCP クライアント（エージェント）側の運用で行う。** `confirm_vote` は AI が呼べるツールなので、プログラムで「人間が承認した」ことは強制できない（仕組みとしての強さは `auto` と同じ。ガード層の上限は効く）。

```toml
[voter]
mode = "browser"

[voter.browser]
confirm = "external"
headless = true             # 画面の無い VM ではヘッドレスで動かす（external・auto のときだけ有効）
browser_channel = ""        # Google Chrome が無ければ Playwright の Chromium（uv run playwright install chromium）
proxy_server = ""           # 空なら環境変数 HTTPS_PROXY / https_proxy
tls_ca_file = "/path/to/proxy-ca.pem"   # TLS を中継するプロキシの CA だけを入れたファイル（最大5個）
```

### プロキシ・証明書

- プロキシは `proxy_server`、空なら環境変数 `HTTPS_PROXY` / `https_proxy`。`NO_PROXY` は除外先として渡す
- ユーザー名・パスワードは config.toml に書かない（書くと起動時にエラー）。環境変数 `KEIBA_PROXY_USERNAME` / `KEIBA_PROXY_PASSWORD`、または `HTTPS_PROXY` の URL（`http://user:pass@host:port`）で渡す
- Chromium は CA ファイルを直接指定できず、Linux では OS の証明書ストアも読まない。そこで `tls_ca_file` の証明書の**公開鍵の指紋（SPKI）だけ**を信頼させる（`--ignore-certificate-errors-spki-list`）。OS の証明書束をそのまま指定すると全部を無条件に信頼してしまうため、6個以上入ったファイルは拒否する
- 代わりに、VM 側で `certutil` を使って NSS（`~/.pki/nssdb`）に CA を登録する方法もある
- `ignore_https_errors = true` は検証を全面的に無効にする。既定は無効で、有効にすると起動時に警告を出す

## 即PAT の画面保存（開発用）

全自動投票を作るために、即PAT の各画面の構造をあなたの Mac で保存する。

```bash
uv sync
uv run playwright install chromium   # 初回だけ（--chrome で Google Chrome を使うなら不要）
uv run tools/capture_ipat.py         # Google Chrome を使う場合は --chrome
```

- ブラウザが開くので、ログインと画面の移動は**自分の手で**行う。スクリプトはクリックも入力もしない
- 保存したい画面を表示したら、ターミナルで画面の名前を入れて Enter。何も入れずに Enter で終了
- 保存前に、入力欄の値・hidden の値・インラインスクリプト・URL のクエリを消し、4桁以上の数字を伏せる。スクリーンショットは撮らない
- 保存先は `captures/`（.gitignore 済み）。共有する前に `summary.txt` などを見て、気になる情報は消す
- **最終確認画面では購入ボタンを押さない**

## 注意事項

- スクレイピング・自動投票は、対象サービスの規約で禁止・制限されている可能性がある。実装前に必ず規約を確認する
- スクレイピングは低頻度（十分な待機時間）にし、取得結果をキャッシュして負荷をかけない
- 実購入に関わる処理（ログイン、購入確定）は、ドライランでの検証が済むまで実装しない
- ガード層の上限値は小さく始める

## Claude Code への指示

- このREADMEのスコープ（ステップ1〜6、実装済み）を守る
- 実購入・ログイン処理は実装しない（`voter/browser.py` はスタブのみ）
- ガード層には必ずテストを書く
- 設計に迷ったら、実装前に質問する

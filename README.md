# HoloGlish — ホロライブ版 YouGlish

[YouGlish](https://youglish.com/japanese) のように、単語・フレーズを入力すると
**その言葉が実際に話されている配信の該当タイムスタンプ**へジャンプして連続再生できる、
対象を **ホロライブ（JP / EN / ID）に限定した** 検索ツールです。

字幕を事前収集して SQLite の全文検索インデックスを作り、Web UI から検索・再生します。

**🌐 公開サイト: https://sonishimaru.github.io/HoloGlish/** （サーバ不要・ブラウザだけで検索）

> 非公式のファンツールです。カバー株式会社 / hololive production とは無関係です。
> 字幕は索引付け目的で取得し、動画の再生は YouTube 公式の埋め込みプレイヤー経由で行います。

## 主な機能

- **多言語の部分一致検索**（日本語・英語・インドネシア語）。並び順は新着順／一致度順。
- **表記ゆれに強い**: 索引・クエリを正規化（NFKC・小文字化・**単語途中の空白除去**・
  **カタカナ↔ひらがな**）して照合。自動字幕の「あり がとう」や「ペコラ↔ぺこら」も一致。
- **複数語 AND**（空白区切り）: 「みこ 歌」で両方を含む用例に絞り込み。
- **メンバー名は日本語表記を優先**（例: 兎田ぺこら）。EN/ID など日本語名が無い場合は英語表記。
- **学習向けの再生**: 該当秒へジャンプ／前後の用例を巡回／連続再生／再生速度(0.5〜1.5×)／
  ループ／**±5秒クリップ**（前後5秒を強調ループ）。既定は**該当語の2秒前から再生**。
- **前後トランスクリプト**表示とクリックでのジャンプ。
- **共有リンク**: 検索条件だけでなく、再生中の用例（クリップ状態含む）へも直接リンクできる。
- **2つの使い方**: 常駐サーバ不要の**静的サイト**（GitHub Pages）と、動的な **FastAPI サーバ**。
- **自動収集**: GitHub Actions が定期的に全アーカイブを収集し、索引をキャッシュとして蓄積・公開。

## 仕組み

```
[yt-dlp 収集] → [字幕パース] → [SQLite FTS5 インデックス] →┬→ [FastAPI 検索 API]        → [フロント + YouTube IFrame]
                                                         └→ [静的サイト書き出し(動画単位の索引)] → [ブラウザ内検索 (GitHub Pages)]
```

- **動画列挙・字幕取得は yt-dlp**（YouTube Data API のキー/クォータ不要）。
  列挙は各チャンネルの**「動画」タブ＋「ライブ」タブ（配信アーカイブ）**の両方を対象にし、
  重複排除のうえ両タブを新しい側から均等に処理する（`--tabs` で変更可、既定 `videos,streams`）。
- **字幕は手動字幕を優先、無ければ自動生成字幕にフォールバック**（`ja` / `en` / `id` を横断）
- **多言語の部分一致検索**は SQLite FTS5 の `trigram` トークナイザで実現
  （日本語は分かち書きが無いため形態素解析に依存しない）。1〜2文字の語は `LIKE` フォールバック。
- **静的サイト版**は同じ検索ロジックを `web/api.js` がブラウザ内で再現する。索引が
  「その gram をどの動画が含むか」まで持つため、**該当する動画のファイルだけ**を取得する
  （詳細は「ブラウザだけで使う」を参照）。

## ディレクトリ

```
config/channels.yaml   対象チャンネル定義（member / name_ja / branch / lang。編集可能な種データ）
pipeline/              収集・パース・インデックス構築・書き出し
  run.py               CLI（collect / catalog / coverage / ingest / export / backfill-names）
  fetch_videos.py      チャンネルの動画一覧列挙（「動画」＋「ライブ」タブをマージ）
  fetch_subtitles.py   字幕DL（手動優先→自動）
  parse_subs.py        json3 / vtt → セグメント
  build_index.py       セグメントを DB へ
  db.py                SQLite スキーマ（FTS5・冪等マイグレーション・収集台帳 catalog）
  export_static.py     索引を静的サイト（動画単位の索引 + フロント + coverage.json）へ書き出し
  coverage.py          収集状況（ライバー別 完了/未収集）を coverage.json へ
  _net.py              リトライ（指数バックオフ）と cookies オプション
server/                検索 API（FastAPI）: search.py / app.py
web/                   フロント。api.js（サーバ/静的の検索抽象）, app.js, index.html, style.css
data/fixtures/         オフライン検証用サンプル字幕
tools/                 coverage_sheet.gs（収集状況を表示する Google スプレッドシート用 Apps Script）
.github/workflows/     ci.yml（テスト）/ collect.yml（自動収集）/ pages.yml（Pages 公開）
tests/                 pytest スイート
```

## セットアップ

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**JavaScript ランタイム（Deno）を推奨**: 新しめの yt-dlp は YouTube 抽出に JS ランタイムが
必要です（無いと `No supported JavaScript runtime could be found` の警告が出て抽出が失敗しやすい）。
**Deno** を入れておくと安定します（yt-dlp が自動検出）。

```bash
brew install deno              # macOS (Homebrew)
# もしくは: curl -fsSL https://deno.land/install.sh | sh
deno --version                 # 確認
```

## 使い方

### 1. 字幕を収集してインデックスを作る（YouTube アクセスが必要）

```bash
# JP を各チャンネル直近5本ずつ
python -m pipeline.run collect --branch jp --limit 5

# 特定メンバーのみ、直近20本
python -m pipeline.run collect --members "Usada Pekora,Sakura Miko" --limit 20

# 日付で絞る
python -m pipeline.run collect --branch en --date-after 20240101 --limit 30
```

- 一度処理した動画は記録され、次回以降スキップされます（**再開可能**）。再取得は `--force`。
- `--sleep`（既定1秒）で動画間の待機を調整し、YouTube への負荷を抑えます。
- **レート制限を受けたら（`RequestBlocked` / `too many requests`）**: 住宅IPでも短時間に大量
  アクセスすると YouTube に一時ブロックされます。**15〜30分ほど待って**から、`--sleep` を上げ
  （例 `SLEEP=3`）、1回の本数を減らして（例 `LIMIT=20`）ゆっくり回すと回復します。Deno を
  入れて yt-dlp 本体で取得できるようにすると、フォールバック(transcript-api)への過剰アクセスが
  減り、レート制限も起きにくくなります。
- **レート制限（HTTP 429 / bot 確認要求）は一過性エラーとして指数バックオフで自動リトライ**します。
  回数は `--retries`（既定3）、基本待機秒は `--retry-base`（既定2秒）で調整できます。
  リトライしても回復しない動画は `error`（次回実行で再取得対象）として記録し、
  字幕が存在しない動画（`no_subs`）とは区別されます。
- **cookies 対応**: 環境変数 `HOLOGLISH_COOKIES` にブラウザから書き出した
  Netscape 形式の cookies ファイルパスを渡すと、bot 判定・年齢制限を緩和できます。
- **字幕取得サービス経路（`--subs-source service`）**: 有償の transcript API（Supadata）経由で
  字幕を取得します。**YouTube への直接アクセスを業者が肩代わりするため、IPブロック/レート制限の
  影響を受けず、クラウド（GitHub Actions）でも収集できます**。環境変数 `SUPADATA_API_KEY` が必要
  （[supadata.ai](https://supadata.ai) で無料100本/月・クレカ不要）。`mode=native`（既存字幕のみ・
  1本=1クレジット）を使い、高額な AI 生成は使いません。誤課金を防ぐため、この経路は
  `service` を明示したときだけ動きます。
- **bot 判定の回避策**（データセンターIP対策）:
  - `--subs-source`（既定 `both`）: 字幕取得経路を `ytdlp` / `api`（youtube-transcript-api・
    別経路の timedtext）/ `both`（yt-dlp→api フォールバック）から選べます。yt-dlp が
    「Sign in to confirm you're not a bot」で弾かれても api 経路なら通ることがあります。
  - `HOLOGLISH_PLAYER_CLIENTS`（既定 `tv,mweb,web_safari`）: yt-dlp の innertube クライアントを
    切り替えて bot 判定を回避します。`default` で yt-dlp 既定に戻します。
  - いずれも根本的にはIPレピュテーション依存です。安定運用は cookies + 住宅IP（プロキシ/
    セルフホスト）が確実です。

### 自宅（住宅IP）で収集する ★推奨

**GitHub のクラウドIPは YouTube に IP レベルでブロックされ、字幕取得ができません**
（yt-dlp・youtube-transcript-api とも「cloud provider の IP はブロック」と返る）。
そのため **収集は住宅回線の自分のPCで実行**します。索引はこれまで同様
`hologlish-data` へ公開され、**スプレッドシート／公開サイトはそのまま更新**されます。

#### かんたん実行（`scripts/collect_local.sh`）

```bash
# 初回のみ
git clone https://github.com/sonishimaru/HoloGlish.git && cd HoloGlish
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 収集（全メンバー・各30本ずつ → hologlish-data へ公開）
bash scripts/collect_local.sh

# 例: 本数を増やす / メンバーやブランチで絞る
LIMIT=50 bash scripts/collect_local.sh
MEMBERS="Usada Pekora,Sakura Miko" bash scripts/collect_local.sh
BRANCH=jp bash scripts/collect_local.sh
```

- 再開可能なので、**繰り返し実行するほど過去アーカイブへ前進**します（全アーカイブは数週間）。
- 定期化するなら **cron / タスクスケジューラ / launchd** から `scripts/collect_local.sh` を呼びます。
  例（毎日3時に実行, crontab）: `0 3 * * * cd /path/to/HoloGlish && bash scripts/collect_local.sh`
- **自宅ネット・ガード**（持ち出しノートPC向け）: `HOME_SSID` に自宅Wi-Fiの名前を設定すると
  **自宅ネットのときだけ収集**し、職場などでは自動スキップします（社内ネットでの誤実行を防止）。
  例: `HOME_SSID="MyHomeWiFi" bash scripts/collect_local.sh`。複数はカンマ区切り。`FORCE=1` で無効化。
- **台帳の全体更新は既定オフ**: 収集(collect)は処理する各チャンネルを全件列挙して台帳も更新するため、
  前段での全台帳列挙は二度手間で遅くなります。全チャンネルの母集合を最新化したいとき（**月1回程度**）
  だけ `CATALOG=1 bash scripts/collect_local.sh` で実行します。通常の収集は台帳全体更新を省いて高速。
- **公開（書き込み）は自動**: `collect_local.sh` は収集が終わると自動で `hologlish-data` へ公開します。
  途中で `Ctrl+C` しても、それまでに集めた分は `data/hologlish.db` に保存済みです。
  - **一定時間で区切って自動公開**: `TIME_BUDGET=3600 bash scripts/collect_local.sh`（秒。例は1時間）。
  - **集めた分だけ今すぐ書き込む**（Ctrl+C の後など、収集せず公開だけ）: `PUBLISH_ONLY=1 bash scripts/collect_local.sh`
- `HOLOGLISH_COOKIES` にブラウザから書き出した cookies を渡すと年齢制限動画も取得できます（任意）。

#### 完全自動化: セルフホストrunner（任意）

自宅マシンを **GitHub Actions のセルフホストrunner** として登録すると、`collect.yml` を
自宅IPで自動実行できます（クラウドの弱点を回避しつつ自動化）。

1. リポジトリ Settings → Actions → Runners → **New self-hosted runner** の手順で自宅PCに登録。
2. `collect.yml` の `runs-on: ubuntu-latest` を **`self-hosted`** に変更し、冒頭の
   `schedule:` cron のコメントを外す。
3. **セキュリティ（public リポジトリ必須）**: Settings → Actions → General →
   **Fork pull request workflows** を無効化（第三者PRが自宅runnerでコードを実行するのを防ぐ）。

> クラウドの `schedule` は無効化済みです（IPブロックで空振りするため）。手動の
> `workflow_dispatch` は残していますが、クラウドでは収集は通りません。

### （参考）GitHub Actions のワークフロー構成

`.github/workflows/collect.yml` は次の設計です（セルフホストrunner で使う場合に有効）。

- **列挙は全件**（`--list-depth 0`）で過去アーカイブまで見渡し、
  **1実行では各チャンネル最大30本の新規**（`--limit 30`）を新しい側から処理します。処理済みは
  スキップして次の実行でさらに古い方へ前進するため、**繰り返し実行で全アーカイブに到達**します。
  実行ごとにチャンネル順をシャッフルし、特定チャンネルに偏らず均等に進めます。
- **時間予算で区切り、必ず公開に到達**: 収集は既定で**5時間（`--time-budget 18000`）**に達すると
  区切りよく打ち切り、ジョブ上限（350分）で強制中断される前に公開ステップへ到達します。
  収集は再開可能なので、次回実行で続きから積み上がります（消えません）。
- 字幕が無いと確定した動画（`no_subs`）は再取得しません（`error` は次回再取得）。字幕の無い
  新しい動画で毎回上限を使い切らず、確実に過去へ前進させるためです。
- 手動実行（`workflow_dispatch`）では対象ブランチ・メンバー・本数・列挙深さ・待機秒・時間予算を指定できます。
- 生成した索引 `hologlish.db` は専用ブランチ **`hologlish-data`** に蓄積されます
  （毎回、前回分を復元してから追記するため**再開可能**）。`main` は汚しません。
- **クラウド実行の注意**: GitHub のクラウドIPは YouTube に**IPレベルでブロック**され、
  字幕取得が通りません（cookies でも回避不可）。**収集は住宅IP**（上記のローカル実行／
  セルフホストrunner）で行ってください。`YT_COOKIES` は年齢制限動画の対策として有効です。

収集済み索引を手元やサーバへ取り込むには:

```bash
git fetch origin hologlish-data:hologlish-data
bash .github/scripts/db_restore.sh hologlish-data data/hologlish.db
uvicorn server.app:app
```

### 収集状況スプレッドシート（ライバー別・自動更新）

**どの動画が収集済みか／未収集か**をライバー別に見られる Google スプレッドシートを、
収集のたびに自動更新できます。

- 収集ワークフローは各チャンネルの**全動画を軽量列挙して台帳(`catalog`)化**し、取得結果
  （`done`/`no_subs`/`error`）と突き合わせて、ライバー別の収集状況を
  **`coverage.json`** として `hologlish-data` ブランチと Pages に公開します。
  - ✅ 完了 / ⏳ 未収集 / — 字幕なし / ⚠ エラー を各動画に付与。
- Google スプレッドシート側は **Apps Script（[`tools/coverage_sheet.gs`](tools/coverage_sheet.gs)）** を
  一度貼るだけ。**ライバーごとにタブ**を作り、1時間ごとに `coverage.json` を取得して
  自動再生成します（サマリータブに全体進捗）。CI に認証情報を置かずに済みます。

セットアップ（初回だけ）:

1. Google スプレッドシートを新規作成 → **拡張機能 → Apps Script**
2. `tools/coverage_sheet.gs` の内容を貼り付けて保存
3. 関数 `installHourlyTrigger` を一度実行（権限承認）。以後1時間ごとに自動更新。
   手動更新はメニュー「HoloGlish → 今すぐ更新」

手元で `coverage.json` を作るには:

```bash
python -m pipeline.run catalog      # 各チャンネルの全動画を列挙して台帳更新
python -m pipeline.run coverage --out coverage.json
```

### 2. サーバを起動

```bash
uvicorn server.app:app --reload
# → http://localhost:8000
```

### ブラウザだけで使う（静的サイト / サーバ不要）

収集した索引を**静的サイトとして書き出し、ブラウザ内(クライアントサイド)で検索**できます。
サーバを常駐させずに、URL を開くだけで使える形です（収集した索引がそのままキャッシュになります）。

```bash
# 収集済みの索引から静的サイトを site/ に書き出す
python -m pipeline.run export --out site

# ローカル確認（任意の静的配信でよい）
python -m http.server --directory site 8000
# → http://localhost:8000
```

- 検索・フィルタ・並び順・前後トランスクリプト・連続再生などは、サーバ版と同じ UI が
  ブラウザ内で動きます（検索ロジックは `web/api.js`）。
- **スケーリング（索引 version 5）— 「その語を含む発話だけを取りに行く」**:
  索引は `static/idx/` に次の構成で書き出します。
  - `manifest.json` … 版・**投稿日の新しい順の動画ID一覧**・動画別のメンバー/ブランチ・facets・stats
  - `uni/<k>.json` `bi/<k>.json` `tri/<k>.json` … n-gram → `[group, mask, ...]`
  - `v/<video_id>.json` … その動画のメタと発話
  - `suggest.json` … 入力補完の候補語彙（実際に話されている言い回し）

  速度の要点は3つです。
  - **索引が「どの動画に在るか」まで持つ**。動画を新しい順に並べ、24本ずつの
    「グループ」に分けて、mask（ビット列）でグループ内のどの動画にその gram が
    在るかを表します。クエリの全 gram でマスクを AND すれば**候補動画が動画単位で
    確定**するため、**その語を含まない動画はダウンロードしません**。
    動画IDを直接並べるより桁違いに小さく、実データ（777万発話）で索引は
    シャード単位版の約2倍に収まります。
  - **n-gram 索引自体もバケット分割**。全語彙をまとめた1ファイルは数十MBに達し、
    検索前に必ず読む起動コストになるため、gram のハッシュで分割して
    **クエリに出てくる gram のバケットだけ**を取得します。
  - **必要件数が埋まったら打ち切る**。候補動画を新しい順に取得し、そのページに
    必要な件数が揃った時点で止めます（1本から始め、ここまでの命中率から
    次に取る本数を見積もる）。打ち切った場合の件数は下限なので「37+ 件」の
    ように表示します。
  1文字クエリ用の `uni` 索引もあるため、1文字でも全走査しません。
  **一致度順**は全体を見ないと厳密な最良が決まらないため、新しい側から十分な
  候補を集めた時点で順位付けします（結果は partial 扱い）。
- **検索中の見せ方**: 索引と本文を都度ダウンロードするため、回線によっては数秒
  かかります。何も起きていないように見せないよう、結果行の骨組み（スケルトン）を
  出したうえで「候補 1,803 本を新しい順に確認中… 3 本目・用例 4 件」のように
  取得の進み具合を表示します（`web/app.js` の `progressLabel`）。
- **入力補完**: 「何を検索できるのか分からない」を減らすため、実際に配信で
  話されている言い回しを前方一致で出します（↑↓ で選択、Enter で検索）。
  候補は字幕本文から作るので、選べば必ずヒットします。語彙づくりは
  `pipeline/suggest.py`（区切り文字で切った文字 n-gram のうち、前後の伸び方から
  「語として完結している」ものだけを残す）。候補ファイルは最初の入力があって
  初めて取得するので、起動は遅くなりません。
- 動画再生は従来どおり YouTube 公式 IFrame プレイヤー経由です。

#### GitHub Pages で公開（自動）

`.github/workflows/pages.yml` が、収集済み索引（`hologlish-data` ブランチ。無ければ
フィクスチャ）から静的サイトを生成し **GitHub Pages に公開**します。定期収集の完了後や
`web/` の変更時に自動で再公開されます。

> 初回のみ、リポジトリ Settings → Pages → Source を **GitHub Actions** に設定してください。

これで「収集 → 索引を蓄積（キャッシュ）→ ブラウザから URL で検索」までが自動で回ります。

### 3. サーバ版フロントで検索

メンバー名は**日本語ユーザー向けに日本語表記を優先表示**します（例: 兎田ぺこら）。
`config/channels.yaml` の `name_ja` を表示に使い、無いメンバー（EN/ID など）は英語表記のままです。
既存 DB は `python -m pipeline.run backfill-names` で日本語名を補完できます。

検索ボックスに日本語（例:「おはよ」「ぺこ」）や英語（`hello`）を入力。
結果をクリックすると該当秒から再生され、**前／次の用例**ボタンや連続再生で用例を巡回できます。
ブランチ・メンバー・言語で絞り込め、並び順は **新着順／一致度順** を切り替えられます。

YouGlish のような学習向けの操作に対応しています。

- **再生速度**（0.5×〜1.5×）: 聞き取り練習用にゆっくり再生。
- **リプレイ／ループ**: 同じ用例を繰り返し再生。
- **±5秒クリップ**: 該当箇所の前後5秒だけを強調ループ再生（`C` キー）。共有リンクに
  クリップ状態が載り、リンクを開くとその区間から再生されます（動画DLはせず埋め込み再生のまま）。
- **前後トランスクリプト**: いま再生中の場面の前後の発話を表示し、クリックでその行へジャンプ。
- **キーボード操作**: `←`／`→` で用例移動、`Space` で再生・停止、`R` でリプレイ、`L` でループ、`C` で±5秒クリップ。
- **共有可能なURL**: 検索語・フィルタ・並び順が URL のハッシュに反映され、リロードや共有で復元されます。
- **用例への深いリンク**: 再生中の用例（動画＋秒）も URL に含まれ、「🔗 共有」ボタンや
  リンクのコピーで、その用例へ直接ジャンプできる共有リンクになります。
- **最近の検索**: 直近の検索語を端末内（localStorage）に保存し、トップページから
  ワンクリックで再検索できます（「クリア」で消去可能）。

## 対象範囲について（重要）

`config/channels.yaml` は JP / EN / ID の全ブランチを設定できる構造ですが、
**全メンバー・全動画の字幕を一括取得するのは長時間かかり、レート制限の対象**になります。
そのためパイプラインは再開可能・件数制御可能に作っています。

推奨運用: まず `--branch` / `--members` / `--limit` で範囲を絞って動くものを用意し、
以後の実行で対象を段階的に広げてください。

`channels.yaml` の `channel_id` は代表例です。デビュー・卒業に合わせて随時更新してください。

## オフラインでの動作確認（YouTube 不要）

サンプル字幕を取り込んで一連の流れを確認できます。

```bash
python -m pipeline.run ingest --manifest data/fixtures/manifest.json
uvicorn server.app:app --port 8000
# ブラウザで「おはよう」「ぺこ」「hello」を検索
```

`ingest` は手元にある字幕ファイル（json3 / vtt）を取り込む汎用コマンドでもあります。

## API

| エンドポイント | 説明 |
| --- | --- |
| `GET /api/search?q=&member=&branch=&lang=&sort=&page=&page_size=` | 字幕検索（JSON）。`sort` は `date`（既定）/ `relevance` |
| `GET /api/context?video_id=&start=&window=` | 用例の前後トランスクリプト（その場面の周辺発話） |
| `GET /api/facets` | フィルタ候補（メンバー・ブランチ・言語） |
| `GET /api/stats` | インデックスのカバレッジ統計（動画数・発話数・メンバー数） |
| `GET /` | 検索フロント |

## 環境変数

- `HOLOGLISH_DB` : 使用する SQLite DB パス（既定 `data/hologlish.db`）

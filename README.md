# HoloGlish — ホロライブ版 YouGlish

[YouGlish](https://youglish.com/japanese) のように、単語・フレーズを入力すると
**その言葉が実際に話されている配信の該当タイムスタンプ**へジャンプして連続再生できる、
対象を **ホロライブ（JP / EN / ID）に限定した** 検索ツールです。

字幕を事前収集して SQLite の全文検索インデックスを作り、Web UI から検索・再生します。

> 非公式のファンツールです。カバー株式会社 / hololive production とは無関係です。
> 字幕は索引付け目的で取得し、動画の再生は YouTube 公式の埋め込みプレイヤー経由で行います。

## 仕組み

```
[yt-dlp 収集] → [字幕パース] → [SQLite FTS5 インデックス] → [FastAPI 検索] → [静的フロント + YouTube IFrame]
```

- **動画列挙・字幕取得は yt-dlp**（YouTube Data API のキー/クォータ不要）
- **字幕は手動字幕を優先、無ければ自動生成字幕にフォールバック**（`ja` / `en` / `id` を横断）
- **多言語の部分一致検索**は SQLite FTS5 の `trigram` トークナイザで実現
  （日本語は分かち書きが無いため形態素解析に依存しない）。1〜2文字の語は `LIKE` フォールバック。

## ディレクトリ

```
config/channels.yaml   対象チャンネル定義（編集可能な種データ）
pipeline/              収集・パース・インデックス構築
  run.py               CLI（collect / ingest）
  fetch_videos.py      チャンネルの動画一覧列挙
  fetch_subtitles.py   字幕DL（手動優先→自動）
  parse_subs.py        json3 / vtt → セグメント
  build_index.py       セグメントを DB へ
  db.py                SQLite スキーマ（FTS5）
server/                検索 API（FastAPI）
web/                   フロント（検索UI + YouTube IFrame プレイヤー）
data/fixtures/         オフライン検証用サンプル字幕
```

## セットアップ

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
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
- **レート制限（HTTP 429 / bot 確認要求）は一過性エラーとして指数バックオフで自動リトライ**します。
  回数は `--retries`（既定3）、基本待機秒は `--retry-base`（既定2秒）で調整できます。
  リトライしても回復しない動画は `error`（次回実行で再取得対象）として記録し、
  字幕が存在しない動画（`no_subs`）とは区別されます。
- **cookies 対応**: 環境変数 `HOLOGLISH_COOKIES` にブラウザから書き出した
  Netscape 形式の cookies ファイルパスを渡すと、bot 判定・年齢制限を緩和できます。

### 自動収集（スケジュール実行）

GitHub Actions のワークフロー `.github/workflows/collect.yml` で**定期的に自動収集**できます。

- 既定で毎日 1 回動作（`schedule` の cron はリポジトリ側で調整可）。手動実行
  （`workflow_dispatch`）では対象ブランチ・メンバー・本数・待機秒を指定できます。
- 生成した索引 `hologlish.db` は専用ブランチ **`hologlish-data`** に蓄積されます
  （毎回、前回分を復元してから追記するため**再開可能**）。`main` は汚しません。
- **重要**: GitHub ランナーの IP は YouTube に bot 判定されやすいため、安定運用には
  リポジトリ Secret **`YT_COOKIES`**（Netscape 形式 cookies の中身）の設定を推奨します。
  未設定でも動きますが、一部の動画が 429 / サインイン要求で失敗しえます。

収集済み索引を手元やサーバへ取り込むには:

```bash
git fetch origin hologlish-data
git show hologlish-data:hologlish.db > data/hologlish.db
uvicorn server.app:app
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
  `static/data.json` を読んで**すべてブラウザ内で**動きます（検索ロジックは `web/api.js`）。
- 動画再生は従来どおり YouTube 公式 IFrame プレイヤー経由です。

#### GitHub Pages で公開（自動）

`.github/workflows/pages.yml` が、収集済み索引（`hologlish-data` ブランチ。無ければ
フィクスチャ）から静的サイトを生成し **GitHub Pages に公開**します。定期収集の完了後や
`web/` の変更時に自動で再公開されます。

> 初回のみ、リポジトリ Settings → Pages → Source を **GitHub Actions** に設定してください。

これで「収集 → 索引を蓄積（キャッシュ）→ ブラウザから URL で検索」までが自動で回ります。

### 3. サーバ版フロントで検索

検索ボックスに日本語（例:「おはよ」「ぺこ」）や英語（`hello`）を入力。
結果をクリックすると該当秒から再生され、**前／次の用例**ボタンや連続再生で用例を巡回できます。
ブランチ・メンバー・言語で絞り込め、並び順は **新着順／一致度順** を切り替えられます。

YouGlish のような学習向けの操作に対応しています。

- **再生速度**（0.5×〜1.5×）: 聞き取り練習用にゆっくり再生。
- **リプレイ／ループ**: 同じ用例を繰り返し再生。
- **前後トランスクリプト**: いま再生中の場面の前後の発話を表示し、クリックでその行へジャンプ。
- **キーボード操作**: `←`／`→` で用例移動、`Space` で再生・停止、`R` でリプレイ、`L` でループ切り替え。
- **共有可能なURL**: 検索語・フィルタ・並び順が URL のハッシュに反映され、リロードや共有で復元されます。

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

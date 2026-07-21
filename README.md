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

### 2. サーバを起動

```bash
uvicorn server.app:app --reload
# → http://localhost:8000
```

### 3. ブラウザで検索

検索ボックスに日本語（例:「おはよ」「ぺこ」）や英語（`hello`）を入力。
結果をクリックすると該当秒から再生され、**前／次の用例**ボタンや連続再生で用例を巡回できます。
ブランチ・メンバー・言語で絞り込めます。

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
| `GET /api/search?q=&member=&branch=&lang=&page=&page_size=` | 字幕検索（JSON） |
| `GET /api/facets` | フィルタ候補（メンバー・ブランチ・言語） |
| `GET /` | 検索フロント |

## 環境変数

- `HOLOGLISH_DB` : 使用する SQLite DB パス（既定 `data/hologlish.db`）

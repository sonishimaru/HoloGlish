# HoloGlish 収集索引（自動更新）

このブランチは `Scheduled Collect` ワークフローが自動生成する
SQLite 索引（`hologlish.db.gz.part-*`: gzip 圧縮を 90MB で分割）と
収集状況 `coverage.json` を保持します。手で編集しないでください。
（分割は GitHub の 100MB/ファイル制限を回避するため）

利用側:
```bash
git fetch origin hologlish-data:hologlish-data
bash .github/scripts/db_restore.sh hologlish-data data/hologlish.db
uvicorn server.app:app
```

`coverage.json` はライバー別の収集状況（完了/未収集）を持ち、
Google スプレッドシートの Apps Script から取得して自動更新に使います。

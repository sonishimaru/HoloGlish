# HoloGlish 収集索引（自動更新）

このブランチは `Scheduled Collect` ワークフローが自動生成する
SQLite 索引 `hologlish.db` と収集状況 `coverage.json` を保持します。
手で編集しないでください。

利用側:
```bash
git fetch origin hologlish-data
git show hologlish-data:hologlish.db > data/hologlish.db
uvicorn server.app:app
```

`coverage.json` はライバー別の収集状況（完了/未収集）を持ち、
Google スプレッドシートの Apps Script から取得して自動更新に使います。

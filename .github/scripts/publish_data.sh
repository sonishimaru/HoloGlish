#!/usr/bin/env bash
# hologlish.db と coverage.json を専用ブランチ hologlish-data へ公開する。
# 収集ワークフローの「早期公開（収集状況を先に出す）」と「最終公開（収集分を反映）」の
# 両方から呼ばれる共通スクリプト。GH_TOKEN 環境変数が必要。
set -euo pipefail

if [ ! -f data/hologlish.db ]; then
  echo "索引ファイルが無いため公開をスキップします"
  exit 0
fi

pub="$RUNNER_TEMP/pub"
rm -rf "$pub" && mkdir -p "$pub"
cp data/hologlish.db "$pub/hologlish.db"
# 収集状況（あれば）も一緒に公開。Google スプレッドシートがこの JSON を読む。
[ -f data/coverage.json ] && cp data/coverage.json "$pub/coverage.json" || true

cat > "$pub/README.md" <<'EOF'
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
EOF

cd "$pub"
git init -q
# 新規に git init したこのリポジトリに author 情報を設定する
# （checkout 側で設定しても $pub には効かず commit が empty ident で失敗する）
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git checkout -q -b hologlish-data
git add hologlish.db README.md
[ -f coverage.json ] && git add coverage.json || true
git commit -q -m "索引を更新 ($(date -u +%Y-%m-%dT%H:%MZ))"
git remote add origin "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
git push -q -f origin hologlish-data
echo "hologlish-data ブランチへ公開しました"

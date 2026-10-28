#!/usr/bin/env bash
# hologlish.db と coverage.json を専用ブランチ hologlish-data へ公開する。
# GH_TOKEN 環境変数（または checkout の認証）が必要。
#
# サブコマンド:
#   split <dir>     DB を gzip 圧縮して 90MB で分割し <dir> へ用意する（push しない）
#   push <dir>      用意済みの <dir> を hologlish-data へ段階 push + 原子的スワップ
#   coverage-only   coverage.json だけを差し替えた軽量コミットを push（数秒で終わる。
#                   収集前の「早期公開」用。DB の再 push はしない）
#   （引数なし）    split + push（従来互換。手動利用向け）
#
# 背景: DB 全体の公開（split+push）は索引の成長で 2 時間級に伸びた。収集と同じ
# ジョブで行うとジョブ上限 350 分を食い合い、最終公開の途中で強制終了して収集分を
# 失う事故が繰り返された（2026-09-28 Run#149 / 2026-10-28 Run#262, #263）。
# そのため collect.yml は「収集ジョブで split → artifact 受け渡し → 公開専用ジョブで
# push」と分離し、早期公開は coverage-only で済ませる。
set -euo pipefail

split_db() {
  local dir="$1"
  if [ ! -f data/hologlish.db ]; then
    echo "索引ファイルが無いため公開をスキップします"
    exit 0
  fi
  rm -rf "$dir" && mkdir -p "$dir"
  # DB は gzip 圧縮して 90MB で分割する。GitHub は 100MB 超のファイルの push を
  # 拒否する（収集が進み 776MB に達した索引が弾かれ、収集分を失った実績あり）。
  # 復元は .github/scripts/db_restore.sh（連結→gunzip）で行う。
  gzip -6 -c data/hologlish.db | split -b 90m - "$dir/hologlish.db.gz.part-"
  # 収集状況（あれば）も一緒に公開。Google スプレッドシートがこの JSON を読む。
  [ -f data/coverage.json ] && cp data/coverage.json "$dir/coverage.json" || true

  cat > "$dir/README.md" <<'EOF'
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
EOF
  echo "公開用ディレクトリを用意しました: $dir ($(ls "$dir" | wc -l | tr -d ' ') ファイル)"
}

push_dir() {
  local dir="$1"
  if ! ls "$dir"/hologlish.db.gz.part-* >/dev/null 2>&1; then
    echo "公開するパートが無いためスキップします"
    exit 0
  fi
  cd "$dir"
  git init -q
  # 新規に git init したこのリポジトリに author 情報を設定する
  # （checkout 側で設定しても $dir には効かず commit が empty ident で失敗する）
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git checkout -q -b hologlish-data
  git remote add origin "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"

  # 【重要】全パートを1コミット・1pushで送ると、pack が 1GB を超えたあたりで
  # GitHub 側が HTTP 500 で切断し公開に失敗する（2026-08-28〜29 に6ラン連続で
  # 発生し、各ランの収集結果を失った）。そこでパートを1つずつ commit/push して
  # オブジェクトを ~90MB ずつサーバへ積み上げ（staging ブランチ宛）、最後に
  # 本ブランチ hologlish-data へ原子的に切り替える。切り替え push は転送済み
  # オブジェクトを指すだけなので一瞬で終わり、読者が「パートが揃っていない
  # 途中状態」を見ることもない。
  local staging="refs/heads/hologlish-data-staging"
  local first=1
  for p in hologlish.db.gz.part-*; do
    git add "$p"
    git commit -q -m "stage $p"
    if [ "$first" = 1 ]; then
      git push -q -f origin "HEAD:$staging"   # 前回の残骸があってもリセット
      first=0
    else
      git push -q origin "HEAD:$staging"
    fi
    echo "  staged: $p"
  done
  git add README.md
  [ -f coverage.json ] && git add coverage.json || true
  git commit -q -m "索引を更新 ($(date -u +%Y-%m-%dT%H:%MZ))"
  git push -q origin "HEAD:$staging"
  # 原子的スワップ（オブジェクトは既にサーバへ転送済み）
  git push -q -f origin "HEAD:refs/heads/hologlish-data"
  git push -q origin ":$staging" || true   # 後片付け（失敗しても無害）
  echo "hologlish-data ブランチへ公開しました"
}

coverage_only() {
  # DB はそのまま、coverage.json だけ差し替えたコミットを既存ブランチへ積む。
  # restore ステップが取得済みのローカル ref (hologlish-data) を親にするため、
  # 転送されるのは新しい JSON の blob だけで数秒で終わる。
  if [ ! -f data/coverage.json ]; then
    echo "coverage.json が無いため早期公開をスキップします"
    exit 0
  fi
  if ! git rev-parse -q --verify hologlish-data >/dev/null; then
    echo "hologlish-data ブランチが未取得のため早期公開をスキップします（最終公開で作成）"
    exit 0
  fi
  local blob tree commit tmpidx
  blob=$(git hash-object -w data/coverage.json)
  tmpidx=$(mktemp)
  GIT_INDEX_FILE="$tmpidx" git read-tree hologlish-data
  GIT_INDEX_FILE="$tmpidx" git update-index --add --cacheinfo "100644,$blob,coverage.json"
  tree=$(GIT_INDEX_FILE="$tmpidx" git write-tree)
  rm -f "$tmpidx"
  commit=$(git -c user.name="github-actions[bot]" \
               -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
               commit-tree "$tree" -p hologlish-data -m "coverage を更新 ($(date -u +%Y-%m-%dT%H:%MZ))")
  git push -q origin "$commit:refs/heads/hologlish-data"
  git update-ref refs/heads/hologlish-data "$commit"
  echo "coverage.json のみを hologlish-data へ公開しました"
}

case "${1:-full}" in
  split)         split_db "${2:?split には出力ディレクトリが必要}";;
  push)          push_dir "${2:?push には入力ディレクトリが必要}";;
  coverage-only) coverage_only;;
  full)          dir="${RUNNER_TEMP:-/tmp}/pub"; split_db "$dir"; push_dir "$dir";;
  *) echo "不明なサブコマンド: $1" >&2; exit 2;;
esac

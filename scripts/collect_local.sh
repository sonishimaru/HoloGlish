#!/usr/bin/env bash
# HoloGlish — 自宅（住宅IP）で収集して hologlish-data ブランチへ公開するスクリプト。
#
# GitHub Actions のクラウドIPは YouTube に bot 判定されるため収集できない。
# このスクリプトを「住宅回線の自分のPC」で実行すると収集が通り、結果を
# hologlish-data へ公開する（＝スプレッドシート／公開サイトに反映）。
#
# 使い方:
#   bash scripts/collect_local.sh                 # 全メンバー・各30本ずつ収集
#   LIMIT=50 bash scripts/collect_local.sh        # 1回の本数を増やす
#   MEMBERS="Usada Pekora,Sakura Miko" bash scripts/collect_local.sh   # 絞り込み
#   BRANCH=jp bash scripts/collect_local.sh       # ブランチで絞り込み
#
# 任意の環境変数:
#   LIMIT(30) / MEMBERS / BRANCH / SLEEP(1.5) / SUBS(both) / TIME_BUDGET(0=無制限)
#   HOLOGLISH_COOKIES … ブラウザから書き出した cookies ファイルパス（年齢制限対策・任意）
#
# 定期実行するなら cron / タスクスケジューラ / launchd から本スクリプトを呼ぶ。
set -euo pipefail

cd "$(dirname "$0")/.."            # リポジトリのルートへ
ORIGIN_URL="$(git remote get-url origin)"

DB="data/hologlish.db"
LIMIT="${LIMIT:-30}"
SLEEP="${SLEEP:-1.5}"
SUBS="${SUBS:-both}"
TIME_BUDGET="${TIME_BUDGET:-0}"   # 自宅なら時間制限不要（0=無制限）
MEMBERS="${MEMBERS:-}"
BRANCH="${BRANCH:-}"

mkdir -p data

echo "==> 既存の索引を hologlish-data から復元"
if git fetch origin hologlish-data 2>/dev/null; then
  git show origin/hologlish-data:hologlish.db > "$DB" 2>/dev/null \
    && echo "    既存索引を復元しました" \
    || echo "    索引ファイルが無いため新規作成します"
else
  echo "    hologlish-data ブランチが無いため新規作成します"
fi

echo "==> 台帳(catalog)を更新（未収集の母集合）"
python -m pipeline.run catalog \
  ${BRANCH:+--branch "$BRANCH"} ${MEMBERS:+--members "$MEMBERS"} \
  --sleep 1 --retries 3 --retry-base 5 || echo "    catalog 更新をスキップ（続行）"

echo "==> 字幕を収集"
python -m pipeline.run collect \
  ${BRANCH:+--branch "$BRANCH"} ${MEMBERS:+--members "$MEMBERS"} \
  --limit "$LIMIT" --list-depth 0 --subs-source "$SUBS" \
  --sleep "$SLEEP" --time-budget "$TIME_BUDGET" --retries 4 --retry-base 5

echo "==> 収集状況 coverage.json を生成"
python -m pipeline.run coverage --out data/coverage.json || true

echo "==> hologlish-data へ公開"
pub="$(mktemp -d)"
cp "$DB" "$pub/hologlish.db"
[ -f data/coverage.json ] && cp data/coverage.json "$pub/coverage.json" || true
(
  cd "$pub"
  git init -q
  git checkout -q -b hologlish-data
  git add hologlish.db
  [ -f coverage.json ] && git add coverage.json || true
  git -c user.name="hololish-local" -c user.email="local@hololish" \
      commit -q -m "索引を更新 (local $(date -u +%Y-%m-%dT%H:%MZ))"
  git remote add origin "$ORIGIN_URL"
  git push -q -f origin hologlish-data
)
rm -rf "$pub"
echo "==> 完了: hologlish-data へ公開しました（スプレッドシート／公開サイトに反映されます）"

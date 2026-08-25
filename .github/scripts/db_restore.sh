#!/usr/bin/env bash
# hologlish-data ブランチから索引DB(hologlish.db)を復元する共通スクリプト。
# 使い方: db_restore.sh <git-ref> <出力先パス>
#   例: db_restore.sh hologlish-data data/hologlish.db
#
# レイアウト両対応:
#   新: hologlish.db.gz.part-aa, -ab, ...（gzip圧縮を90MBで分割。GitHubの
#       100MB/ファイル制限を回避するため。連結→gunzip で復元する）
#   旧: hologlish.db（単一ファイル。過去の公開分との後方互換）
# 復元できなければ非0で終了する（呼び出し側でフォールバック判断）。
set -euo pipefail

ref="$1"
out="$2"

parts=$(git ls-tree --name-only "$ref" 2>/dev/null | grep '^hologlish\.db\.gz\.part-' | sort || true)
if [ -n "$parts" ]; then
  for p in $parts; do git show "$ref:$p"; done | gunzip > "$out"
  [ -s "$out" ] || { rm -f "$out"; exit 1; }
  echo "分割圧縮レイアウトから索引を復元しました ($(echo "$parts" | wc -l | tr -d ' ') パート)"
elif git show "$ref:hologlish.db" > "$out" 2>/dev/null && [ -s "$out" ]; then
  echo "単一ファイルレイアウトから索引を復元しました"
else
  rm -f "$out"
  exit 1
fi

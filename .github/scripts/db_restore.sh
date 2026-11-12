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
#
# 【重要】一時ファイルに展開してから mv する。出力先へ直接書くと、途中失敗で
# 不完全なDBが残り、後続の公開ステップがそれを本物として配ってしまう
# （2026-11-12 Run#311: 復元失敗で残った切断DBがブランチを上書きした）。
set -euo pipefail

ref="$1"
out="$2"
tmp="$out.restoring"
trap 'rm -f "$tmp"' EXIT

parts=$(git ls-tree --name-only "$ref" 2>/dev/null | grep '^hologlish\.db\.gz\.part-' | sort || true)
if [ -n "$parts" ]; then
  for p in $parts; do git show "$ref:$p"; done | gunzip > "$tmp"
  [ -s "$tmp" ] || exit 1
  # 完全性チェック: 途中で切れたDBを本物として扱わない。SQLiteヘッダの
  # page_size × page_count と実ファイルサイズを突き合わせる（O(1)で切断を検出）
  python3 - "$tmp" <<'EOF'
import os, struct, sys
path = sys.argv[1]
with open(path, "rb") as f:
    h = f.read(100)
assert h[:16] == b"SQLite format 3\x00", "SQLiteヘッダではない"
page_size = struct.unpack(">H", h[16:18])[0]
if page_size == 1:
    page_size = 65536
page_count = struct.unpack(">I", h[28:32])[0]
actual = os.path.getsize(path)
expected = page_size * page_count
assert actual >= expected, f"DBが切断されている: {actual} < {expected}"
EOF
  mv "$tmp" "$out"
  echo "分割圧縮レイアウトから索引を復元しました ($(echo "$parts" | wc -l | tr -d ' ') パート)"
elif git show "$ref:hologlish.db" > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
  mv "$tmp" "$out"
  echo "単一ファイルレイアウトから索引を復元しました"
else
  exit 1
fi

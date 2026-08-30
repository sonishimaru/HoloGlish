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
#   HOME_SSID … 自宅Wi-Fiのネットワーク名。設定すると「自宅ネットのときだけ」収集し、
#               それ以外（職場など）では自動スキップする。複数はカンマ区切り。
#   FORCE=1  … 自宅判定を無視して強制実行（HOME_SSID ガードを無効化）。
#   CATALOG=1 … 全チャンネルの台帳(未収集の母集合)を最新化してから収集する（月1回程度で十分）。
#               既定は行わない（収集が触れた分は自動更新されるため速い）。
#   PUBLISH_ONLY=1 … 収集はせず、手元の索引(data/hologlish.db)をそのまま公開だけする。
#               収集を Ctrl+C で止めた後などに「集めた分だけ書き込みたい」ときに使う。
#
# 補足: 通常は収集完了時に自動で公開します。一定時間で区切って自動公開したいときは
#       TIME_BUDGET（秒）を指定（例 TIME_BUDGET=3600 で1時間収集して公開）。
#
# 定期実行するなら cron / タスクスケジューラ / launchd から本スクリプトを呼ぶ。
set -euo pipefail

cd "$(dirname "$0")/.."            # リポジトリのルートへ
ORIGIN_URL="$(git remote get-url origin)"

# 前後の空白を除去（ipconfig 等の出力は末尾に空白が付くことがある）
_trim() { sed 's/^[[:space:]]*//; s/[[:space:]]*$//'; }

# --- 現在接続中の Wi-Fi ネットワーク名(SSID)を返す（macOS / Linux / Windows対応） ---
current_ssid() {
  s=""
  # macOS: 新しめのOSでは networksetup/airport が SSID を返さないため
  # ipconfig getsummary を最優先で使う（BSSID 行は除外し、'SSID : ' 以降を採用）。
  if command -v ipconfig >/dev/null 2>&1; then
    for i in en0 en1 en2; do
      s=$(ipconfig getsummary "$i" 2>/dev/null | awk -F 'SSID : ' '/ SSID : /{print $2; exit}' | _trim)
      [ -n "$s" ] && { printf '%s' "$s"; return 0; }
    done
  fi
  if command -v networksetup >/dev/null 2>&1; then
    for i in en0 en1 en2; do
      s=$(networksetup -getairportnetwork "$i" 2>/dev/null | sed -n 's/^Current Wi-Fi Network: //p' | _trim)
      [ -n "$s" ] && { printf '%s' "$s"; return 0; }
    done
  fi
  # Linux
  if command -v nmcli >/dev/null 2>&1; then
    s=$(nmcli -t -f active,ssid dev wifi 2>/dev/null | awk -F: '/^yes:/{print $2; exit}' | _trim)
    [ -n "$s" ] && { printf '%s' "$s"; return 0; }
  fi
  if command -v iwgetid >/dev/null 2>&1; then
    s=$(iwgetid -r 2>/dev/null | _trim); [ -n "$s" ] && { printf '%s' "$s"; return 0; }
  fi
  # Windows (Git Bash)
  if command -v netsh >/dev/null 2>&1; then
    s=$(netsh wlan show interfaces 2>/dev/null \
        | sed -n 's/^[[:space:]]*SSID[[:space:]]*:[[:space:]]*//p' | head -1 | _trim)
    [ -n "$s" ] && { printf '%s' "$s"; return 0; }
  fi
  return 1
}

# --- 自宅ネット・ガード（職場での誤発動を防ぐ） ---
# PUBLISH_ONLY（書き込みのみ）や FORCE=1 のときはガードを適用しない。
if [ -n "${HOME_SSID:-}" ] && [ "${FORCE:-0}" != "1" ] && [ "${PUBLISH_ONLY:-0}" != "1" ]; then
  ssid="$(current_ssid || true)"
  if [ -z "$ssid" ]; then
    echo "現在の Wi-Fi 名を取得できませんでした。安全のため収集をスキップします。"
    echo "（自宅で実行しているのにスキップされる場合は macOS の「位置情報サービス」で"
    echo "  ターミナルに許可を与えるか、FORCE=1 を付けて実行してください）"
    exit 0
  fi
  case ",${HOME_SSID}," in
    *",${ssid},"*) echo "自宅ネット「${ssid}」を確認。収集を続行します。" ;;
    *) echo "現在の Wi-Fi「${ssid}」は自宅(HOME_SSID=${HOME_SSID})ではないためスキップします。"
       exit 0 ;;
  esac
fi

DB="data/hologlish.db"
LIMIT="${LIMIT:-30}"
SLEEP="${SLEEP:-1.5}"
SUBS="${SUBS:-both}"
TIME_BUDGET="${TIME_BUDGET:-0}"   # 自宅なら時間制限不要（0=無制限）
MEMBERS="${MEMBERS:-}"
BRANCH="${BRANCH:-}"

mkdir -p data

if [ "${PUBLISH_ONLY:-0}" = "1" ]; then
  # 書き込み専用モード: 収集はせず、手元の索引をそのまま公開する
  # （収集を Ctrl+C で止めた後などに、集めた分だけ書き込みたいとき用）。
  echo "==> 書き込み専用モード（収集はスキップ）。手元の索引をそのまま公開します"
  [ -f "$DB" ] || { echo "    $DB が見つかりません。先に収集してください。" >&2; exit 1; }
else
  echo "==> 既存の索引を hologlish-data から復元"
  if git fetch origin hologlish-data 2>/dev/null; then
    bash .github/scripts/db_restore.sh origin/hologlish-data "$DB" \
      && echo "    既存索引を復元しました" \
      || echo "    索引ファイルが無いため新規作成します"
  else
    echo "    hologlish-data ブランチが無いため新規作成します"
  fi

  # 台帳(未収集の母集合)の全体更新は既定では行わない。
  # 収集(collect)ステップが処理する各チャンネルを全件列挙して台帳も更新するため、
  # 前段での全台帳列挙は多くが二度手間になり時間がかかる。全チャンネルの母集合を
  # きっちり最新化したいとき（月1回など）だけ CATALOG=1 で実行する。
  if [ "${CATALOG:-0}" = "1" ]; then
    echo "==> 台帳(catalog)を全体更新（未収集の母集合・時間がかかります）"
    python -m pipeline.run catalog \
      ${BRANCH:+--branch "$BRANCH"} ${MEMBERS:+--members "$MEMBERS"} \
      --sleep 1 --retries 3 --retry-base 5 || echo "    catalog 更新をスキップ（続行）"
  else
    echo "==> 台帳の全体更新はスキップ（CATALOG=1 で実行可）。収集が触れた分は自動更新されます"
  fi

  echo "==> 字幕を収集"
  python -m pipeline.run collect \
    ${BRANCH:+--branch "$BRANCH"} ${MEMBERS:+--members "$MEMBERS"} \
    --limit "$LIMIT" --list-depth 0 --subs-source "$SUBS" \
    --sleep "$SLEEP" --time-budget "$TIME_BUDGET" --retries 4 --retry-base 5
fi

echo "==> 収集状況 coverage.json を生成"
python -m pipeline.run coverage --out data/coverage.json || true

echo "==> hologlish-data へ公開"
pub="$(mktemp -d)"
# GitHub の 100MB/ファイル制限を回避するため gzip 圧縮を 90MB で分割して公開する
# （復元は .github/scripts/db_restore.sh）。
gzip -6 -c "$DB" | split -b 90m - "$pub/hologlish.db.gz.part-"
[ -f data/coverage.json ] && cp data/coverage.json "$pub/coverage.json" || true
(
  cd "$pub"
  git init -q
  git config user.name "hololish-local"
  git config user.email "local@hololish"
  git checkout -q -b hologlish-data
  git remote add origin "$ORIGIN_URL"
  # 大きな一括pushは GitHub が HTTP 500 で切断するため、パートを1つずつ
  # staging へ push し、最後に本ブランチへ原子的に切り替える
  # （publish_data.sh と同じ方式）。
  staging="refs/heads/hologlish-data-staging"
  first=1
  for p in hologlish.db.gz.part-*; do
    git add "$p"
    git commit -q -m "stage $p"
    if [ "$first" = 1 ]; then
      git push -q -f origin "HEAD:$staging"
      first=0
    else
      git push -q origin "HEAD:$staging"
    fi
  done
  [ -f coverage.json ] && git add coverage.json || true
  git commit -q --allow-empty -m "索引を更新 (local $(date -u +%Y-%m-%dT%H:%MZ))"
  git push -q origin "HEAD:$staging"
  git push -q -f origin "HEAD:refs/heads/hologlish-data"
  git push -q origin ":$staging" || true
)
rm -rf "$pub"
echo "==> 完了: hologlish-data へ公開しました（スプレッドシート／公開サイトに反映されます）"

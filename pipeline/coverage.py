"""収集状況（カバレッジ）を coverage.json として書き出す。

台帳(catalog: チャンネルに存在する全動画) と processed(取得結果) / videos(取得済み)
を突き合わせ、ライバー別に各動画の状態を判定する:

  done     … 字幕を取得しインデックス済み（✅ 完了）
  no_subs  … 字幕が存在しないと確定（— 対象外）
  error    … 一過性エラーで未取得（⚠ 次回再取得）
  pending  … 台帳にあるがまだ着手していない（⏳ 未収集）

Google スプレッドシート（Apps Script）や静的サイトが読み取り、ライバー別タブ／表に
展開する。個々のライバーの母集合は台帳に依存するため、収集/カタログ更新のたびに育つ。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List


def _status_map(conn: sqlite3.Connection) -> Dict[str, str]:
    return {
        r["video_id"]: (r["status"] or "")
        for r in conn.execute("SELECT video_id, status FROM processed")
    }


def build_coverage(conn: sqlite3.Connection) -> Dict[str, Any]:
    """DB からライバー別の収集状況を構築して返す。"""
    status = _status_map(conn)

    # 台帳（母集合）に、台帳に無いが取得済みの動画も補って統合する。
    rows: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT video_id, member, member_ja, branch, title, url FROM catalog"
    ):
        rows[r["video_id"]] = dict(r)
    for r in conn.execute(
        "SELECT video_id, member, member_ja, branch, title, url FROM videos"
    ):
        # 台帳に無くても取得済みなら収集状況に含める（母集合の穴埋め）。
        rows.setdefault(r["video_id"], dict(r))

    def classify(vid: str) -> str:
        st = status.get(vid, "")
        if st == "done":
            return "done"
        if st in ("no_subs", "error"):
            return st
        return "pending"

    members: Dict[str, Dict[str, Any]] = {}
    for vid, r in rows.items():
        member = r.get("member") or "(unknown)"
        m = members.setdefault(member, {
            "member": member,
            "member_ja": r.get("member_ja") or "",
            "branch": r.get("branch") or "",
            "videos": [],
        })
        if not m["member_ja"] and r.get("member_ja"):
            m["member_ja"] = r["member_ja"]
        m["videos"].append({
            "video_id": vid,
            "title": r.get("title") or "",
            "url": r.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "status": classify(vid),
        })

    def counts(videos: List[Dict[str, Any]]) -> Dict[str, int]:
        c = {"total": len(videos), "done": 0, "no_subs": 0, "error": 0, "pending": 0}
        for v in videos:
            c[v["status"]] = c.get(v["status"], 0) + 1
        return c

    member_list: List[Dict[str, Any]] = []
    summary = {"total": 0, "done": 0, "no_subs": 0, "error": 0, "pending": 0}
    for member in sorted(members):
        m = members[member]
        # 未収集→エラー→完了→字幕なし の順に並べ、未着手を上に出す
        order = {"pending": 0, "error": 1, "done": 2, "no_subs": 3}
        m["videos"].sort(key=lambda v: (order.get(v["status"], 9), v["title"]))
        m["counts"] = counts(m["videos"])
        for k in summary:
            summary[k] += m["counts"][k]
        member_list.append(m)

    return {"summary": summary, "members": member_list}


def write_coverage(data: Dict[str, Any], out_path: str) -> None:
    d = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(d, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

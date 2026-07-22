"""パース済みセグメントを SQLite に投入する。"""

from __future__ import annotations

import sqlite3
from typing import List, Dict, Any

from .normalize import normalize


def upsert_video(conn: sqlite3.Connection, video: Dict[str, Any]) -> None:
    video = {"member_ja": "", **video}  # 未指定でも欠損しないように既定を補う
    conn.execute(
        """
        INSERT INTO videos(video_id, member, member_ja, branch, lang, title, published_at, url, sub_kind)
        VALUES(:video_id, :member, :member_ja, :branch, :lang, :title, :published_at, :url, :sub_kind)
        ON CONFLICT(video_id) DO UPDATE SET
            member=excluded.member, member_ja=excluded.member_ja, branch=excluded.branch,
            lang=excluded.lang, title=excluded.title, published_at=excluded.published_at,
            url=excluded.url, sub_kind=excluded.sub_kind
        """,
        video,
    )


def replace_segments(
    conn: sqlite3.Connection, video_id: str, lang: str, segments: List[Dict[str, Any]]
) -> int:
    """既存セグメントを消してから入れ直す（再実行で重複しないように）。"""
    conn.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
    conn.executemany(
        "INSERT INTO segments(video_id, lang, start, dur, text, norm) VALUES(?,?,?,?,?,?)",
        [(video_id, lang, s["start"], s["dur"], s["text"], normalize(s["text"])) for s in segments],
    )
    return len(segments)

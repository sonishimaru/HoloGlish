"""字幕インデックスの検索ロジック。

- 3文字以上: FTS5(trigram) の MATCH（高速な部分一致）
- 1〜2文字: LIKE フォールバック（trigram は3文字未満を索引化しないため）
member / branch / lang でフィルタ、投稿日の新しい順にページング。
"""

from __future__ import annotations

import sqlite3
from typing import List, Dict, Any, Optional


def _highlight_snippet(text: str, query: str, radius: int = 40) -> str:
    """マッチ位置を中心に前後を切り出したスニペットを返す（大小文字無視）。"""
    idx = text.lower().find(query.lower())
    if idx < 0:
        return text[: radius * 2]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(query) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def _fts_query(query: str) -> str:
    """FTS5 のクエリ文字列へエスケープ。フレーズとして厳密一致させる。"""
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


def search(
    conn: sqlite3.Connection,
    query: str,
    member: Optional[str] = None,
    branch: Optional[str] = None,
    lang: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"query": query, "page": page, "total": 0, "results": []}

    filters: List[str] = []
    params: List[Any] = []
    if member:
        filters.append("v.member = ?")
        params.append(member)
    if branch:
        filters.append("v.branch = ?")
        params.append(branch)
    if lang:
        filters.append("s.lang = ?")
        params.append(lang)

    use_fts = len(query) >= 3
    if use_fts:
        where_core = "segments_fts MATCH ?"
        core_param: List[Any] = [_fts_query(query)]
        from_clause = (
            "FROM segments_fts "
            "JOIN segments s ON s.id = segments_fts.rowid "
            "JOIN videos v ON v.video_id = s.video_id"
        )
    else:
        where_core = "s.text LIKE ? ESCAPE '\\'"
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        core_param = [like]
        from_clause = "FROM segments s JOIN videos v ON v.video_id = s.video_id"

    where = " AND ".join([where_core] + filters)

    count_sql = f"SELECT COUNT(*) AS n {from_clause} WHERE {where}"
    total = conn.execute(count_sql, core_param + params).fetchone()["n"]

    offset = max(0, (page - 1) * page_size)
    rows_sql = f"""
        SELECT v.video_id, v.member, v.branch, v.title, v.url, v.sub_kind,
               s.start, s.dur, s.text, s.lang
        {from_clause}
        WHERE {where}
        ORDER BY v.published_at DESC, s.start ASC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(rows_sql, core_param + params + [page_size, offset]).fetchall()

    results = []
    for r in rows:
        results.append(
            {
                "video_id": r["video_id"],
                "member": r["member"],
                "branch": r["branch"],
                "title": r["title"],
                "url": r["url"],
                "lang": r["lang"],
                "sub_kind": r["sub_kind"],
                "start": r["start"],
                "dur": r["dur"],
                "text": r["text"],
                "snippet": _highlight_snippet(r["text"], query),
            }
        )
    return {"query": query, "page": page, "page_size": page_size, "total": total, "results": results}


def facets(conn: sqlite3.Connection) -> Dict[str, Any]:
    """フィルタ用の候補（メンバー・ブランチ・言語）を返す。"""
    members = [r["member"] for r in conn.execute(
        "SELECT DISTINCT member FROM videos WHERE member <> '' ORDER BY member"
    )]
    branches = [r["branch"] for r in conn.execute(
        "SELECT DISTINCT branch FROM videos WHERE branch <> '' ORDER BY branch"
    )]
    langs = [r["lang"] for r in conn.execute(
        "SELECT DISTINCT lang FROM segments WHERE lang <> '' ORDER BY lang"
    )]
    return {"members": members, "branches": branches, "langs": langs}

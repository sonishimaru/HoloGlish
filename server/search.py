"""字幕インデックスの検索ロジック。

- 3文字以上: FTS5(trigram) の MATCH（高速な部分一致）
- 1〜2文字: LIKE フォールバック（trigram は3文字未満を索引化しないため）
member / branch / lang でフィルタ、投稿日の新しい順にページング。

ソート:
- ``date``（既定）: 投稿日の新しい順。「最近の用例」を優先する。
- ``relevance``: FTS 経路は bm25 スコア順（一致の良い順）。
  LIKE 経路はマッチが目立つ短い発話を優先する近似。

YouGlish の「用例トランスクリプト」に相当する前後文脈は :func:`context` で返す。
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
    sort: str = "date",
) -> Dict[str, Any]:
    query = (query or "").strip()
    sort = sort if sort in ("date", "relevance") else "date"
    if not query:
        return {"query": query, "page": page, "page_size": page_size,
                "total": 0, "sort": sort, "results": []}

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

    if sort == "relevance":
        # FTS は bm25（値が小さいほど良い一致）。LIKE 経路は bm25 が無いので、
        # 語が相対的に目立つ「短い発話」を優先する近似で代用する。
        order_by = "bm25(segments_fts) ASC" if use_fts else "length(s.text) ASC, v.published_at DESC"
    else:
        order_by = "v.published_at DESC, s.start ASC"

    count_sql = f"SELECT COUNT(*) AS n {from_clause} WHERE {where}"
    total = conn.execute(count_sql, core_param + params).fetchone()["n"]

    offset = max(0, (page - 1) * page_size)
    rows_sql = f"""
        SELECT v.video_id, v.member, v.member_ja, v.branch, v.title, v.url, v.sub_kind,
               s.start, s.dur, s.text, s.lang
        {from_clause}
        WHERE {where}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(rows_sql, core_param + params + [page_size, offset]).fetchall()

    results = []
    for r in rows:
        results.append(
            {
                "video_id": r["video_id"],
                "member": r["member"],
                "member_ja": r["member_ja"] or "",
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
    return {"query": query, "page": page, "page_size": page_size,
            "total": total, "sort": sort, "results": results}


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    """インデックスのカバレッジ統計（トップページ・空状態の表示用）。

    YouGlish の「◯◯本の動画から検索」に相当する規模感を返す。
    """
    videos = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]
    segments = conn.execute("SELECT COUNT(*) AS n FROM segments").fetchone()["n"]
    members = conn.execute(
        "SELECT COUNT(DISTINCT member) AS n FROM videos WHERE member <> ''"
    ).fetchone()["n"]
    by_branch = {
        r["branch"]: r["n"]
        for r in conn.execute(
            "SELECT branch, COUNT(*) AS n FROM videos WHERE branch <> '' GROUP BY branch"
        )
    }
    return {
        "videos": videos,
        "segments": segments,
        "members": members,
        "by_branch": by_branch,
    }


def context(
    conn: sqlite3.Connection,
    video_id: str,
    start: float,
    window: int = 3,
) -> Dict[str, Any]:
    """YouGlish 風の「用例トランスクリプト」用に、ある時点の前後の発話を返す。

    ``start`` に最も近いセグメントを中心に、前後 ``window`` 件ずつを返す。
    現在行は ``is_current=True`` を立てる。動画メタ（member/title 等）も併せて返す。
    """
    window = max(0, min(int(window), 20))
    video = conn.execute(
        "SELECT video_id, member, branch, lang, title, url, sub_kind "
        "FROM videos WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    if video is None:
        return {"video_id": video_id, "video": None, "segments": []}

    rows = conn.execute(
        "SELECT id, start, dur, text FROM segments WHERE video_id = ? ORDER BY start ASC",
        (video_id,),
    ).fetchall()
    if not rows:
        return {"video_id": video_id, "video": dict(video), "segments": []}

    # start に最も近いセグメントの位置を探す
    center = min(range(len(rows)), key=lambda i: abs(rows[i]["start"] - start))
    lo = max(0, center - window)
    hi = min(len(rows), center + window + 1)

    segments = [
        {
            "start": rows[i]["start"],
            "dur": rows[i]["dur"],
            "text": rows[i]["text"],
            "is_current": i == center,
        }
        for i in range(lo, hi)
    ]
    return {"video_id": video_id, "video": dict(video), "segments": segments}


def facets(conn: sqlite3.Connection) -> Dict[str, Any]:
    """フィルタ用の候補（メンバー・ブランチ・言語）を返す。

    メンバーは絞り込みの値（正規名 value）と表示名（日本語優先の label）の
    組で返す。フロントはフィルタ値に value を使い、表示に label を使う。
    """
    members = [
        {"value": r["member"], "label": (r["member_ja"] or r["member"])}
        for r in conn.execute(
            "SELECT member, MAX(member_ja) AS member_ja FROM videos "
            "WHERE member <> '' GROUP BY member ORDER BY member"
        )
    ]
    branches = [r["branch"] for r in conn.execute(
        "SELECT DISTINCT branch FROM videos WHERE branch <> '' ORDER BY branch"
    )]
    langs = [r["lang"] for r in conn.execute(
        "SELECT DISTINCT lang FROM segments WHERE lang <> '' ORDER BY lang"
    )]
    return {"members": members, "branches": branches, "langs": langs}

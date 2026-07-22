"""HoloGlish FastAPI アプリ。

  uvicorn server.app:app --reload

- GET /api/search : 字幕検索
- GET /api/facets : フィルタ候補
- /               : 静的フロント（web/）
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pipeline import db as _db
from . import search as _search

WEB_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "web")

app = FastAPI(title="HoloGlish", description="ホロライブ版 YouGlish")


def _conn():
    # DB パスはリクエスト時に解決（テストや運用で HOLOGLISH_DB を切り替えられるように）
    db_path = os.environ.get("HOLOGLISH_DB", _db.DEFAULT_DB)
    conn = _db.connect(db_path)
    _db.init_db(conn)  # DB 未生成でも 500 にせず空結果を返せるように
    return conn


@app.get("/api/search")
def api_search(
    q: str = Query("", description="検索語（日本語・英語・インドネシア語）"),
    member: str | None = None,
    branch: str | None = None,
    lang: str | None = None,
    page: int = 1,
    page_size: int = 20,
    sort: str = Query("date", description="date（新着順）/ relevance（一致度順）"),
):
    conn = _conn()
    try:
        return _search.search(
            conn, q, member=member, branch=branch, lang=lang,
            page=page, page_size=min(max(page_size, 1), 100), sort=sort,
        )
    finally:
        conn.close()


@app.get("/api/context")
def api_context(
    video_id: str = Query(..., description="対象動画ID"),
    start: float = Query(0.0, description="中心にする時点（秒）"),
    window: int = Query(3, description="前後に返す発話数"),
):
    """用例の前後文脈（トランスクリプト）を返す。"""
    conn = _conn()
    try:
        return _search.context(conn, video_id, start=start, window=window)
    finally:
        conn.close()


@app.get("/api/facets")
def api_facets():
    conn = _conn()
    try:
        return _search.facets(conn)
    finally:
        conn.close()


@app.get("/api/stats")
def api_stats():
    """インデックスのカバレッジ統計（動画数・セグメント数・メンバー数）。"""
    conn = _conn()
    try:
        return _search.stats(conn)
    finally:
        conn.close()


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


# 静的アセット（app.js / style.css 等）
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

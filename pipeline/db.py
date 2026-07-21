"""SQLite スキーマと接続ヘルパ。

多言語（日本語は分かち書きが無い）を統一的に部分一致検索するため、
FTS5 の trigram トークナイザを使う。3文字以上は MATCH、1〜2文字は LIKE で検索する
（search.py 側で分岐）。
"""

from __future__ import annotations

import os
import sqlite3

DEFAULT_DB = os.path.join(os.path.dirname(__file__), os.pardir, "data", "hologlish.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id     TEXT PRIMARY KEY,
    member       TEXT,
    branch       TEXT,
    lang         TEXT,          -- 採用した字幕の言語
    title        TEXT,
    published_at TEXT,
    url          TEXT,
    sub_kind     TEXT           -- manual / auto
);

CREATE TABLE IF NOT EXISTS segments (
    id       INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    lang     TEXT,
    start    REAL,
    dur      REAL,
    text     TEXT
);
CREATE INDEX IF NOT EXISTS idx_segments_video ON segments(video_id);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text,
    content='segments',
    content_rowid='id',
    tokenize='trigram'
);

-- segments と FTS を同期
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;

-- 再開用: 処理済み video_id を記録
CREATE TABLE IF NOT EXISTS processed (
    video_id   TEXT PRIMARY KEY,
    status     TEXT,            -- done / no_subs / error
    updated_at TEXT
);
"""


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def is_processed(conn: sqlite3.Connection, video_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed WHERE video_id = ? AND status = 'done'", (video_id,)
    ).fetchone()
    return row is not None


def mark_processed(conn: sqlite3.Connection, video_id: str, status: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO processed(video_id, status, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(video_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
        (video_id, status, ts),
    )

"""SQLite スキーマと接続ヘルパ。

多言語（日本語は分かち書きが無い）を統一的に部分一致検索するため、
FTS5 の trigram トークナイザを使う。3文字以上は MATCH、1〜2文字は LIKE で検索する
（search.py 側で分岐）。

FTS は表示用の生テキスト `text` ではなく、正規化テキスト `norm`
（NFKC・小文字化・空白除去・カナ→かな畳み込み。normalize.py）で索引する。
これにより自動字幕の表記ゆれ・単語途中の空白を吸収して一致率を上げる。
"""

from __future__ import annotations

import os
import sqlite3

from .normalize import normalize

DEFAULT_DB = os.path.join(os.path.dirname(__file__), os.pardir, "data", "hologlish.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id     TEXT PRIMARY KEY,
    member       TEXT,          -- 内部キー兼英語表記（フィルタ・保存に使う正規名）
    member_ja    TEXT,          -- UI 表示用の日本語名（無ければ member を表示）
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
    text     TEXT,
    norm     TEXT           -- 検索照合用の正規化テキスト（FTS が索引する）
);
CREATE INDEX IF NOT EXISTS idx_segments_video ON segments(video_id);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    norm,
    content='segments',
    content_rowid='id',
    tokenize='trigram'
);

-- segments と FTS を同期（索引対象は norm）
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, norm) VALUES (new.id, new.norm);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, norm) VALUES ('delete', old.id, old.norm);
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, norm) VALUES ('delete', old.id, old.norm);
    INSERT INTO segments_fts(rowid, norm) VALUES (new.id, new.norm);
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
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """既存 DB に後から入った列を冪等に追加する（後方互換）。"""
    vcols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    if "member_ja" not in vcols:
        conn.execute("ALTER TABLE videos ADD COLUMN member_ja TEXT")

    scols = {r["name"] for r in conn.execute("PRAGMA table_info(segments)")}
    if "norm" not in scols:
        # 旧DB: norm 列を追加し、既存テキストから正規化を backfill、
        # FTS を norm ベースへ作り直す。
        conn.execute("ALTER TABLE segments ADD COLUMN norm TEXT")
        for row in conn.execute("SELECT id, text FROM segments").fetchall():
            conn.execute(
                "UPDATE segments SET norm = ? WHERE id = ?",
                (normalize(row["text"] or ""), row["id"]),
            )
        _rebuild_fts(conn)


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    """segments_fts を norm ベースで作り直す（トリガも張り直す）。"""
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS segments_ai;
        DROP TRIGGER IF EXISTS segments_ad;
        DROP TRIGGER IF EXISTS segments_au;
        DROP TABLE IF EXISTS segments_fts;
        CREATE VIRTUAL TABLE segments_fts USING fts5(
            norm, content='segments', content_rowid='id', tokenize='trigram'
        );
        CREATE TRIGGER segments_ai AFTER INSERT ON segments BEGIN
            INSERT INTO segments_fts(rowid, norm) VALUES (new.id, new.norm);
        END;
        CREATE TRIGGER segments_ad AFTER DELETE ON segments BEGIN
            INSERT INTO segments_fts(segments_fts, rowid, norm) VALUES ('delete', old.id, old.norm);
        END;
        CREATE TRIGGER segments_au AFTER UPDATE ON segments BEGIN
            INSERT INTO segments_fts(segments_fts, rowid, norm) VALUES ('delete', old.id, old.norm);
            INSERT INTO segments_fts(rowid, norm) VALUES (new.id, new.norm);
        END;
        """
    )
    conn.execute("INSERT INTO segments_fts(rowid, norm) SELECT id, norm FROM segments")


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

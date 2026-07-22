"""テスト共通フィクスチャ。

サンプル字幕（data/fixtures）を一時 DB に取り込み、検索・API テストで共有する。
YouTube アクセスは不要（オフラインで完結）。
"""

import os

import pytest

from pipeline import db, run

FIXTURE_MANIFEST = os.path.join(
    os.path.dirname(__file__), os.pardir, "data", "fixtures", "manifest.json"
)


@pytest.fixture
def built_db(tmp_path):
    """フィクスチャを取り込んだ一時 DB のパスを返す。"""
    db_path = str(tmp_path / "test.db")
    run.main(["--db", db_path, "ingest", "--manifest", FIXTURE_MANIFEST])
    return db_path


@pytest.fixture
def conn(built_db):
    c = db.connect(built_db)
    yield c
    c.close()

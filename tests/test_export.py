"""静的サイト書き出し（export_static）のテスト。"""

import json
import os

from pipeline import db, export_static, run


def test_export_site_structure(built_db, tmp_path):
    out = str(tmp_path / "site")
    conn = db.connect(built_db)
    info = export_static.export_site(conn, out)
    conn.close()

    # 必要なファイルが揃っている
    assert os.path.isfile(os.path.join(out, "index.html"))
    for name in ("app.js", "api.js", "style.css", "config.js", "data.json"):
        assert os.path.isfile(os.path.join(out, "static", name)), name

    # index.html の /static/ 参照は相対パスへ書き換わっている
    html = open(os.path.join(out, "index.html"), encoding="utf-8").read()
    assert "/static/" not in html
    assert "static/app.js" in html

    # config.js は静的モードを有効化する
    cfg = open(os.path.join(out, "static", "config.js"), encoding="utf-8").read()
    assert "HOLOGLISH_DATA_URL" in cfg

    # data.json は検索に必要な構造を持つ
    data = json.load(open(os.path.join(out, "static", "data.json"), encoding="utf-8"))
    assert data["videos"] and data["segments"]
    seg = data["segments"][0]
    assert set(seg) == {"v", "l", "s", "d", "t"}
    assert data["stats"]["videos"] == info["videos"]
    assert "members" in data["facets"]


def test_export_via_cli(built_db, tmp_path):
    out = str(tmp_path / "site2")
    rc = run.main(["--db", built_db, "export", "--out", out])
    assert rc == 0
    assert os.path.isfile(os.path.join(out, "static", "data.json"))


def test_backfill_names(built_db):
    """backfill-names が既存DBの member_ja を channels.yaml から補完する。"""
    from pipeline import db, run

    run.main(["--db", built_db, "backfill-names"])
    conn = db.connect(built_db)
    row = conn.execute(
        "SELECT member_ja FROM videos WHERE member = 'Sakura Miko' LIMIT 1"
    ).fetchone()
    conn.close()
    # channels.yaml で Sakura Miko → さくらみこ
    assert row["member_ja"] == "さくらみこ"


def test_export_data_matches_search(built_db, tmp_path):
    """data.json の内容がサーバ検索の母集合と一致する。"""
    from server import search

    conn = db.connect(built_db)
    data = export_static.build_data(conn)
    total_segments = search.stats(conn)["segments"]
    conn.close()
    assert len(data["segments"]) == total_segments

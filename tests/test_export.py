"""静的サイト書き出し（シャード化トリグラム索引）のテスト。"""

import json
import os

from pipeline import db, export_static, run


def _load(path):
    return json.load(open(path, encoding="utf-8"))


def test_export_site_structure(built_db, tmp_path):
    out = str(tmp_path / "site")
    conn = db.connect(built_db)
    info = export_static.export_site(conn, out)
    conn.close()

    # 必要なファイルが揃っている
    assert os.path.isfile(os.path.join(out, "index.html"))
    for name in ("app.js", "api.js", "style.css", "config.js"):
        assert os.path.isfile(os.path.join(out, "static", name)), name
    idx = os.path.join(out, "static", "idx")
    assert os.path.isfile(os.path.join(idx, "manifest.json"))
    assert os.path.isfile(os.path.join(idx, "tri-index.json"))

    # index.html の /static/ 参照は相対パスへ書き換わっている
    html = open(os.path.join(out, "index.html"), encoding="utf-8").read()
    assert "/static/" not in html
    assert "static/app.js" in html

    # config.js は静的モード（シャード索引）を有効化する
    cfg = open(os.path.join(idx, os.pardir, "config.js"), encoding="utf-8").read()
    assert "HOLOGLISH_INDEX_BASE" in cfg

    # manifest とシャード
    manifest = _load(os.path.join(idx, "manifest.json"))
    assert manifest["version"] == 2
    assert manifest["shards"] >= 1
    assert manifest["stats"]["videos"] == info["videos"]
    assert "members" in manifest["facets"]
    for b in range(manifest["shards"]):
        assert os.path.isfile(os.path.join(idx, f"shard-{b}.json")), b


def test_trigram_index_points_to_segment(built_db, tmp_path):
    """「おはよう」のトリグラムが該当シャードを指し、実体で部分一致する。"""
    out = str(tmp_path / "site")
    conn = db.connect(built_db)
    export_static.export_site(conn, out)
    conn.close()
    idx = os.path.join(out, "static", "idx")
    tri_index = _load(os.path.join(idx, "tri-index.json"))

    tri = "おはよ"
    assert tri in tri_index, "収集済みの語のトリグラムが索引にある"
    found = False
    for b in tri_index[tri]:
        shard = _load(os.path.join(idx, f"shard-{b}.json"))
        for vi, si in shard["tri"].get(tri, []):
            text = shard["segs"][vi][si][2]
            if "おはよ" in text:
                found = True
    assert found


def test_export_via_cli(built_db, tmp_path):
    out = str(tmp_path / "site2")
    rc = run.main(["--db", built_db, "export", "--out", out])
    assert rc == 0
    assert os.path.isfile(os.path.join(out, "static", "idx", "manifest.json"))


def test_backfill_names(built_db):
    """backfill-names が既存DBの member_ja を channels.yaml から補完する。"""
    run.main(["--db", built_db, "backfill-names"])
    conn = db.connect(built_db)
    row = conn.execute(
        "SELECT member_ja FROM videos WHERE member = 'Sakura Miko' LIMIT 1"
    ).fetchone()
    conn.close()
    assert row["member_ja"] == "さくらみこ"


def _sharded_search(idx, query):
    """クライアント(api.js)と同じロジックの Python 版（テスト用の照合）。"""
    q = query.lower()
    shards = idx["shards"]
    hits = 0
    if len(query) >= 3:
        tris = {q[i:i + 3] for i in range(len(q) - 2)}
        # 全トリグラムを含むシャードの積集合
        buckets = None
        for t in tris:
            bs = set(idx["tri_index"].get(t, []))
            buckets = bs if buckets is None else (buckets & bs)
            if not buckets:
                break
        for b in (buckets or set()):
            shard = shards[b]
            # シャード内で posting を積集合
            postings = None
            for t in tris:
                s = {(vi, si) for vi, si in shard["tri"].get(t, [])}
                postings = s if postings is None else (postings & s)
                if not postings:
                    break
            for vi, si in (postings or set()):
                if q in shard["segs"][vi][si][2].lower():
                    hits += 1
    else:
        for shard in shards.values():
            for vi, segs in enumerate(shard["segs"]):
                for seg in segs:
                    if q in seg[2].lower():
                        hits += 1
    return hits


def test_sharded_search_matches_server_multishard(built_db, monkeypatch):
    """シャードを強制分割しても、シャード索引検索の件数がサーバ検索と一致する。"""
    from server import search

    # 動画1本ごとに別シャードへ（マルチシャードを強制）
    monkeypatch.setattr(export_static, "VIDEOS_PER_SHARD", 1)
    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    assert idx["manifest"]["shards"] >= 2, "複数シャードになっている"
    for q in ["おはよう", "ありがとう", "歌", "ぺこ", "hello", "です"]:
        server_total = search.search(conn, q, page_size=100)["total"]
        assert _sharded_search(idx, q) == server_total, f"{q}: {server_total}"
    conn.close()


def test_index_segment_count_matches(built_db):
    """全シャードのセグメント数の合計が索引の総数と一致する。"""
    from server import search

    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    total = search.stats(conn)["segments"]
    conn.close()
    counted = sum(
        len(segs) for shard in idx["shards"].values() for segs in shard["segs"]
    )
    assert counted == total == idx["manifest"]["segments"]

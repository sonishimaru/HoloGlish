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

    # manifest・索引・シャード
    manifest = _load(os.path.join(idx, "manifest.json"))
    assert manifest["version"] == 3
    assert manifest["shards"] >= 1
    assert manifest["stats"]["videos"] == info["videos"]
    assert "members" in manifest["facets"]
    assert os.path.isfile(os.path.join(idx, "bi-index.json"))
    for b in range(manifest["shards"]):
        assert os.path.isfile(os.path.join(idx, f"shard-{b}.json")), b


def test_trigram_index_points_to_shard(built_db, tmp_path):
    """「おはよ」の 3-gram が、その語を正規化テキストに含むシャードを指す。"""
    from pipeline.normalize import normalize

    out = str(tmp_path / "site")
    conn = db.connect(built_db)
    export_static.export_site(conn, out)
    conn.close()
    idx = os.path.join(out, "static", "idx")
    tri_index = _load(os.path.join(idx, "tri-index.json"))

    tri = "おはよ"
    assert tri in tri_index
    found = False
    for b in tri_index[tri]:
        shard = _load(os.path.join(idx, f"shard-{b}.json"))
        for vsegs in shard["segs"]:
            for seg in vsegs:
                if "おはよ" in normalize(seg[2]):
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
    from pipeline.normalize import normalize, terms

    tl = terms(query)
    if not tl:
        return 0
    tri, bi, shards = idx["tri_index"], idx["bi_index"], idx["shards"]

    def term_shards(t):
        if len(t) >= 3:
            s = None
            for g in {t[i:i + 3] for i in range(len(t) - 2)}:
                bs = set(tri.get(g, []))
                s = bs if s is None else (s & bs)
                if not s:
                    return set()
            return s or set()
        if len(t) == 2:
            return set(bi.get(t, []))
        return None  # 1文字は絞れない

    shardset = None
    for t in tl:
        ts = term_shards(t)
        if ts is None:
            continue
        shardset = ts if shardset is None else (shardset & ts)
        if shardset is not None and not shardset:
            break
    buckets = range(idx["manifest"]["shards"]) if shardset is None else shardset
    hits = 0
    for b in buckets:
        for vsegs in shards[b]["segs"]:
            for seg in vsegs:
                nt = normalize(seg[2])
                if all(t in nt for t in tl):
                    hits += 1
    return hits


def test_sharded_search_matches_server_multishard(built_db, monkeypatch):
    """シャードを強制分割しても、シャード索引検索の件数がサーバ検索と一致する。"""
    from server import search

    monkeypatch.setattr(export_static, "VIDEOS_PER_SHARD", 1)
    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    assert idx["manifest"]["shards"] >= 2
    for q in ["おはよう", "ありがとう", "歌", "ぺこ", "hello", "です", "おはよう 歌"]:
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

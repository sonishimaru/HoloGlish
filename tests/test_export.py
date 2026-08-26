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
    # n-gram 索引はバケット分割（起動時に全語彙を読まないため）
    assert os.path.isdir(os.path.join(idx, "tri"))
    assert os.path.isdir(os.path.join(idx, "bi"))

    # index.html の /static/ 参照は相対パスへ書き換わっている
    html = open(os.path.join(out, "index.html"), encoding="utf-8").read()
    assert "/static/" not in html
    assert "static/app.js" in html

    # config.js は静的モード（シャード索引）を有効化する
    cfg = open(os.path.join(idx, os.pardir, "config.js"), encoding="utf-8").read()
    assert "HOLOGLISH_INDEX_BASE" in cfg

    # manifest・索引・シャード
    manifest = _load(os.path.join(idx, "manifest.json"))
    assert manifest["version"] == 4
    assert manifest["shards"] >= 1
    assert manifest["stats"]["videos"] == info["videos"]
    assert "members" in manifest["facets"]
    assert manifest["order"] == "date_desc"
    assert manifest["tri_buckets"] == export_static.TRI_BUCKETS
    assert manifest["bi_buckets"] == export_static.BI_BUCKETS
    assert len(manifest["shard_members"]) == manifest["shards"]
    assert len(manifest["shard_branches"]) == manifest["shards"]
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

    tri = "おはよ"
    k = export_static.gram_bucket(tri, export_static.TRI_BUCKETS)
    bucket = _load(os.path.join(idx, "tri", f"{k}.json"))
    assert tri in bucket, "gram は自分のバケットファイルから引ける"
    found = False
    for b in bucket[tri]:
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


def test_shards_are_date_ordered(built_db, tmp_path, monkeypatch):
    """shard 0 が最新。新着順の検索が「先頭シャードから見て打ち切れる」前提。"""
    monkeypatch.setattr(export_static, "VIDEOS_PER_SHARD", 1)
    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    conn.close()

    assert idx["manifest"]["order"] == "date_desc"
    shards = idx["shards"]
    dates = [shards[b]["meta"][0]["published_at"] for b in range(idx["manifest"]["shards"])]
    assert dates == sorted(dates, reverse=True), dates
    # フィクスチャは 0112 > 0111 > 0110
    assert shards[0]["vids"] == ["vid_jp002"]
    assert shards[len(dates) - 1]["vids"] == ["vid_jp001"]


def test_gram_buckets_partition_whole_index(built_db, tmp_path):
    """全 gram がちょうど自分のバケットに1回だけ現れる（取りこぼし/重複なし）。"""
    out = str(tmp_path / "site")
    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    export_static.export_site(conn, out)
    conn.close()
    idx_dir = os.path.join(out, "static", "idx")

    for sub, key, buckets in (
        ("tri", "tri_index", export_static.TRI_BUCKETS),
        ("bi", "bi_index", export_static.BI_BUCKETS),
    ):
        merged = {}
        for k in range(buckets):
            part = _load(os.path.join(idx_dir, sub, f"{k}.json"))
            for gram in part:
                assert export_static.gram_bucket(gram, buckets) == k, gram
                assert gram not in merged, f"{gram} が複数バケットに存在"
            merged.update(part)
        assert merged == idx[key], sub


def test_shard_members_cover_actual_members(built_db, monkeypatch):
    """manifest のシャード別メンバー一覧が実際の内容と一致（絞り込みの間引きに使う）。"""
    monkeypatch.setattr(export_static, "VIDEOS_PER_SHARD", 1)
    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    conn.close()

    man = idx["manifest"]
    values = [m["value"] for m in man["facets"]["members"]]
    for b, shard in idx["shards"].items():
        actual = {values.index(m["member"]) for m in shard["meta"] if m["member"] in values}
        assert set(man["shard_members"][b]) == actual, b


def test_gram_bucket_is_stable():
    """バケット規則は配信済み索引と互換であるべき（値を固定して検知する）。"""
    # web/api.js の gramBucket と同じ FNV-1a。値がずれると検索が壊れる。
    assert export_static.gram_bucket("おはよ", 512) == 429
    assert export_static.gram_bucket("hel", 512) == 46
    assert export_static.gram_bucket("です", 64) == 37

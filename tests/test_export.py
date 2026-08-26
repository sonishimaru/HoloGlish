"""静的サイト書き出し（動画単位の n-gram 索引）のテスト。"""

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

    assert os.path.isfile(os.path.join(out, "index.html"))
    for name in ("app.js", "api.js", "style.css", "config.js"):
        assert os.path.isfile(os.path.join(out, "static", name)), name
    idx = os.path.join(out, "static", "idx")
    assert os.path.isfile(os.path.join(idx, "manifest.json"))
    # n-gram 索引はバケット分割（起動時に全語彙を読まないため）
    for sub in ("uni", "bi", "tri"):
        assert os.path.isdir(os.path.join(idx, sub)), sub

    # index.html の /static/ 参照は相対パスへ書き換わっている
    html = open(os.path.join(out, "index.html"), encoding="utf-8").read()
    assert "/static/" not in html
    assert "static/app.js" in html

    cfg = open(os.path.join(idx, os.pardir, "config.js"), encoding="utf-8").read()
    assert "HOLOGLISH_INDEX_BASE" in cfg

    manifest = _load(os.path.join(idx, "manifest.json"))
    assert manifest["version"] == 5
    assert manifest["stats"]["videos"] == info["videos"]
    assert "members" in manifest["facets"]
    assert manifest["mask_group"] == export_static.MASK_GROUP
    assert manifest["tri_buckets"] == export_static.TRI_BUCKETS
    assert len(manifest["vids"]) == manifest["videos"]
    assert len(manifest["vmem"]) == manifest["videos"]
    assert len(manifest["vbr"]) == manifest["videos"]

    # 本文は動画単位（その語を含む動画だけ取得できるようにするため）
    for vid in manifest["vids"]:
        assert os.path.isfile(os.path.join(idx, "v", f"{vid}.json")), vid


def test_videos_are_date_ordered(built_db):
    """vids は投稿日の新しい順。新着順の検索が「先頭から見て打ち切れる」前提。"""
    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    conn.close()
    vids = idx["manifest"]["vids"]
    dates = [idx["videos"][v]["meta"]["published_at"] for v in vids]
    assert dates == sorted(dates, reverse=True), dates
    # フィクスチャは 0112 > 0111 > 0110
    assert vids[0] == "vid_jp002"
    assert vids[-1] == "vid_jp001"


def test_masks_point_at_videos_containing_the_gram(built_db):
    """索引のマスクが、その gram を実際に含む動画とちょうど一致する。"""
    from pipeline.normalize import normalize

    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    conn.close()
    man = idx["manifest"]
    vids, G = man["vids"], man["mask_group"]

    for gram in ("おはよ", "はよう", "ぺこ", "hel"):
        n = min(len(gram), 3)
        index = idx[{1: "uni_index", 2: "bi_index", 3: "tri_index"}[n]]
        flat = index.get(gram, [])
        got = set()
        for i in range(0, len(flat), 2):
            grp, mask = flat[i], flat[i + 1]
            for b in range(G):
                if mask >> b & 1:
                    got.add(vids[grp * G + b])
        want = {
            v for v in vids
            if any(gram in normalize(s[2]) for s in idx["videos"][v]["segs"])
        }
        assert got == want, gram


def test_gram_buckets_partition_whole_index(built_db, tmp_path):
    """全 gram がちょうど自分のバケットに1回だけ現れる（取りこぼし/重複なし）。"""
    out = str(tmp_path / "site")
    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    export_static.export_site(conn, out)
    conn.close()
    idx_dir = os.path.join(out, "static", "idx")

    for sub, key, buckets in (
        ("uni", "uni_index", export_static.UNI_BUCKETS),
        ("bi", "bi_index", export_static.BI_BUCKETS),
        ("tri", "tri_index", export_static.TRI_BUCKETS),
    ):
        merged = {}
        for k in range(buckets):
            part = _load(os.path.join(idx_dir, sub, f"{k}.json"))
            for gram in part:
                assert export_static.gram_bucket(gram, buckets) == k, gram
                assert gram not in merged, f"{gram} が複数バケットに存在"
            merged.update(part)
        assert merged == idx[key], sub


def test_gram_bucket_is_stable():
    """バケット規則は配信済み索引と互換であるべき（値を固定して検知する）。"""
    # web/api.js の gramBucket と同じ FNV-1a。値がずれると検索が壊れる。
    assert export_static.gram_bucket("おはよ", 512) == 429
    assert export_static.gram_bucket("hel", 512) == 46
    assert export_static.gram_bucket("です", 64) == 37


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


def _client_search(idx, query, member="", branch="", lang=""):
    """クライアント(api.js)と同じ手順の Python 版（テスト用の照合）。

    マスクで候補動画を確定 → その動画だけを走査、という流れまで写す。
    """
    from pipeline.normalize import normalize, terms as split_terms

    tl = split_terms(query)
    if not tl:
        return 0
    man = idx["manifest"]
    vids, G = man["vids"], man["mask_group"]

    def term_masks(t):
        n = min(len(t), 3)
        index = idx[{1: "uni_index", 2: "bi_index", 3: "tri_index"}[n]]
        grams = [t[i:i + 3] for i in range(len(t) - 2)] if n == 3 else [t]
        acc = None
        for g in set(grams):
            flat = index.get(g)
            if not flat:
                return {}
            m = {flat[i]: flat[i + 1] for i in range(0, len(flat), 2)}
            if acc is None:
                acc = m
            else:
                acc = {k: acc[k] & v for k, v in m.items() if k in acc and (acc[k] & v)}
            if not acc:
                return {}
        return acc or {}

    masks = None
    for t in tl:
        m = term_masks(t)
        masks = m if masks is None else {
            k: masks[k] & v for k, v in m.items() if k in masks and (masks[k] & v)
        }
        if not masks:
            break

    hits = 0
    for grp in sorted(masks or {}):
        mask = masks[grp]
        for b in range(G):
            if not (mask >> b & 1):
                continue
            vi = grp * G + b
            if vi >= len(vids):
                continue
            v = idx["videos"][vids[vi]]
            if member and v["meta"]["member"] != member:
                continue
            if branch and v["meta"]["branch"] != branch:
                continue
            for seg in v["segs"]:
                if lang and seg[3] != lang:
                    continue
                nt = normalize(seg[2])
                if all(t in nt for t in tl):
                    hits += 1
    return hits


def test_client_search_matches_server(built_db):
    """マスク索引での検索件数が、サーバ検索(FTS5)と一致する。"""
    from server import search

    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    for q in ["おはよう", "ありがとう", "歌", "ぺこ", "hello", "です",
              "おはよう 歌", "こんばんは", "ぺこら"]:
        server_total = search.search(conn, q, page_size=100)["total"]
        assert _client_search(idx, q) == server_total, f"{q}: {server_total}"
    conn.close()


def test_client_search_matches_server_with_filters(built_db):
    """メンバー/ブランチ/言語で絞り込んでもサーバ検索と一致する。"""
    from server import search

    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    cases = [
        ("おはよう", {"member": "Sakura Miko"}),
        ("おはよう", {"branch": "jp"}),
        ("hello", {"branch": "en"}),
        ("hello", {"branch": "jp"}),
        ("おはよう", {"lang": "ja"}),
    ]
    for q, f in cases:
        server_total = search.search(conn, q, page_size=100, **f)["total"]
        assert _client_search(idx, q, **f) == server_total, f"{q} {f}: {server_total}"
    conn.close()


def test_index_segment_count_matches(built_db):
    """全動画のセグメント数の合計が索引の総数と一致する。"""
    from server import search

    conn = db.connect(built_db)
    idx = export_static.build_index_files(conn)
    total = search.stats(conn)["segments"]
    conn.close()
    counted = sum(len(v["segs"]) for v in idx["videos"].values())
    assert counted == total == idx["manifest"]["segments"]

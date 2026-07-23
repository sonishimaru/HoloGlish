"""収集状況(coverage) 構築のテスト。"""

import json

from pipeline import coverage, db, build_index, run


def _seed(tmp_path):
    conn = db.connect(str(tmp_path / "cov.db"))
    db.init_db(conn)
    # 台帳: みこ3本 / ぺこら2本
    cat = [
        ("m1", "Sakura Miko", "さくらみこ", "jp", "miko done"),
        ("m2", "Sakura Miko", "さくらみこ", "jp", "miko pending"),
        ("m3", "Sakura Miko", "さくらみこ", "jp", "miko nosubs"),
        ("p1", "Usada Pekora", "兎田ぺこら", "jp", "peko done"),
        ("p2", "Usada Pekora", "兎田ぺこら", "jp", "peko error"),
    ]
    for vid, member, ja, branch, title in cat:
        db.upsert_catalog(conn, {
            "video_id": vid, "member": member, "member_ja": ja, "branch": branch,
            "title": title, "url": f"https://youtu.be/{vid}",
        }, "2026-01-01")
    # 取得結果
    db.mark_processed(conn, "m1", "done", "t")
    db.mark_processed(conn, "m3", "no_subs", "t")
    db.mark_processed(conn, "p2", "error", "t")
    # 取得済み動画（done は videos にも入る想定）
    build_index.upsert_video(conn, {
        "video_id": "m1", "member": "Sakura Miko", "member_ja": "さくらみこ",
        "branch": "jp", "lang": "ja", "title": "miko done", "published_at": "",
        "url": "u", "sub_kind": "auto",
    })
    build_index.replace_segments(conn, "m1", "ja", [{"start": 0, "dur": 1, "text": "おはよう"}])
    db.mark_processed(conn, "p1", "done", "t")
    build_index.upsert_video(conn, {
        "video_id": "p1", "member": "Usada Pekora", "member_ja": "兎田ぺこら",
        "branch": "jp", "lang": "ja", "title": "peko done", "published_at": "",
        "url": "u", "sub_kind": "auto",
    })
    conn.commit()
    return conn


def test_coverage_classifies_and_counts(tmp_path):
    conn = _seed(tmp_path)
    data = coverage.build_coverage(conn)
    conn.close()

    assert data["summary"] == {"total": 5, "done": 2, "no_subs": 1, "error": 1, "pending": 1}

    by_member = {m["member"]: m for m in data["members"]}
    miko = by_member["Sakura Miko"]
    assert miko["member_ja"] == "さくらみこ"
    assert miko["counts"] == {"total": 3, "done": 1, "no_subs": 1, "error": 0, "pending": 1}
    st = {v["video_id"]: v["status"] for v in miko["videos"]}
    assert st == {"m1": "done", "m2": "pending", "m3": "no_subs"}
    # 未収集(pending)は先頭に来る（着手すべきものを上に）
    assert miko["videos"][0]["status"] == "pending"

    peko = by_member["Usada Pekora"]
    assert peko["counts"] == {"total": 2, "done": 1, "no_subs": 0, "error": 1, "pending": 0}


def test_coverage_includes_collected_not_in_catalog(tmp_path):
    """台帳に無いが取得済みの動画も母集合に補完される。"""
    conn = db.connect(str(tmp_path / "c2.db"))
    db.init_db(conn)
    build_index.upsert_video(conn, {
        "video_id": "x1", "member": "Tokino Sora", "member_ja": "ときのそら",
        "branch": "jp", "lang": "ja", "title": "t", "published_at": "",
        "url": "u", "sub_kind": "auto",
    })
    db.mark_processed(conn, "x1", "done", "t")
    conn.commit()
    data = coverage.build_coverage(conn)
    conn.close()
    assert data["summary"]["total"] == 1
    assert data["members"][0]["member"] == "Tokino Sora"


def test_coverage_cli_writes_json(tmp_path):
    conn = _seed(tmp_path)
    conn.close()
    out = str(tmp_path / "coverage.json")
    rc = run.main(["--db", str(tmp_path / "cov.db"), "coverage", "--out", out])
    assert rc == 0
    data = json.load(open(out, encoding="utf-8"))
    assert data["summary"]["total"] == 5
    assert {m["member"] for m in data["members"]} == {"Sakura Miko", "Usada Pekora"}

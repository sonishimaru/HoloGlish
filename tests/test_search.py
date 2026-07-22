"""検索ロジックのテスト（FTS5 trigram + LIKE フォールバック + フィルタ）。"""

from server import search


def test_fts_match_japanese(conn):
    r = search.search(conn, "おはよう")
    assert r["total"] == 3
    members = {x["member"] for x in r["results"]}
    assert "Sakura Miko" in members
    assert "Usada Pekora" in members


def test_fts_match_english(conn):
    r = search.search(conn, "hello")
    assert r["total"] == 2
    assert all(x["lang"] == "en" for x in r["results"])


def test_like_fallback_single_char(conn):
    # 1文字は trigram では引けないので LIKE フォールバック
    r = search.search(conn, "歌")
    assert r["total"] == 1
    assert "歌枠" in r["results"][0]["text"]


def test_like_fallback_two_char(conn):
    r = search.search(conn, "ぺこ")
    assert r["total"] == 1
    assert r["results"][0]["member"] == "Usada Pekora"


def test_filter_by_branch(conn):
    assert search.search(conn, "おはよう", branch="jp")["total"] == 3
    assert search.search(conn, "おはよう", branch="en")["total"] == 0


def test_filter_by_member(conn):
    r = search.search(conn, "おはよう", member="Sakura Miko")
    assert r["total"] == 1
    assert r["results"][0]["member"] == "Sakura Miko"


def test_filter_by_lang(conn):
    assert search.search(conn, "hello", lang="en")["total"] == 2
    assert search.search(conn, "hello", lang="ja")["total"] == 0


def test_empty_query_returns_nothing(conn):
    assert search.search(conn, "")["total"] == 0


def test_snippet_present(conn):
    r = search.search(conn, "おはよう")
    assert all(x["snippet"] for x in r["results"])


def test_pagination(conn):
    p1 = search.search(conn, "おはよう", page=1, page_size=2)
    p2 = search.search(conn, "おはよう", page=2, page_size=2)
    assert len(p1["results"]) == 2
    assert len(p2["results"]) == 1
    ids = {(x["video_id"], x["start"]) for x in p1["results"]}
    ids |= {(x["video_id"], x["start"]) for x in p2["results"]}
    assert len(ids) == 3  # ページ間で重複なし


def test_facets(conn):
    f = search.facets(conn)
    assert set(f["branches"]) == {"jp", "en"}
    # メンバーは {value, label} の組で返る
    values = {m["value"] for m in f["members"]}
    assert "Sakura Miko" in values
    # name_ja 未設定なら label は英語表記にフォールバック
    miko = next(m for m in f["members"] if m["value"] == "Sakura Miko")
    assert miko["label"] == "Sakura Miko"
    assert set(f["langs"]) == {"ja", "en"}


def test_special_chars_do_not_crash(conn):
    # FTS の予約文字や LIKE のワイルドカードを含む語でも例外を出さない
    for q in ['"', "AND", "50%", "under_score", "a*b"]:
        search.search(conn, q)


def test_sort_relevance_returns_same_set(conn):
    # 並び順を変えても総件数と結果集合は一致する
    by_date = search.search(conn, "おはよう", sort="date")
    by_rel = search.search(conn, "おはよう", sort="relevance")
    assert by_rel["sort"] == "relevance"
    assert by_rel["total"] == by_date["total"] == 3
    key = lambda r: {(x["video_id"], x["start"]) for x in r["results"]}
    assert key(by_rel) == key(by_date)


def test_sort_invalid_falls_back_to_date(conn):
    r = search.search(conn, "おはよう", sort="bogus")
    assert r["sort"] == "date"


def test_sort_relevance_like_path(conn):
    # 2文字（LIKE 経路）でも relevance 指定が落ちない
    r = search.search(conn, "ぺこ", sort="relevance")
    assert r["sort"] == "relevance"
    assert r["total"] == 1


def test_context_window(conn):
    r = search.search(conn, "おはよう", member="Sakura Miko")
    hit = r["results"][0]
    ctx = search.context(conn, hit["video_id"], start=hit["start"], window=2)
    assert ctx["video"]["member"] == "Sakura Miko"
    assert ctx["segments"], "文脈が空でない"
    current = [s for s in ctx["segments"] if s["is_current"]]
    assert len(current) == 1
    assert abs(current[0]["start"] - hit["start"]) < 1e-6
    # window=2 なら最大 5 件（中心±2）
    assert len(ctx["segments"]) <= 5


def test_context_unknown_video(conn):
    ctx = search.context(conn, "does-not-exist", start=0.0)
    assert ctx["video"] is None
    assert ctx["segments"] == []


def test_member_ja_display_name(tmp_path):
    """member_ja があれば facets の label と検索結果に日本語表示名が載る。"""
    from pipeline import db, build_index

    dbp = str(tmp_path / "ja.db")
    conn = db.connect(dbp)
    db.init_db(conn)
    build_index.upsert_video(conn, {
        "video_id": "v1", "member": "Usada Pekora", "member_ja": "兎田ぺこら",
        "branch": "jp", "lang": "ja", "title": "t", "published_at": "20240101",
        "url": "u", "sub_kind": "auto",
    })
    build_index.replace_segments(conn, "v1", "ja", [{"start": 0.0, "dur": 1.0, "text": "おはようぺこ"}])
    conn.commit()

    r = search.search(conn, "おはよう")
    assert r["results"][0]["member"] == "Usada Pekora"
    assert r["results"][0]["member_ja"] == "兎田ぺこら"

    m = next(m for m in search.facets(conn)["members"] if m["value"] == "Usada Pekora")
    assert m["label"] == "兎田ぺこら"
    conn.close()


def test_migrate_adds_member_ja(tmp_path):
    """member_ja 列が無い旧DBでも init_db で冪等に追加される。"""
    import sqlite3
    from pipeline import db

    dbp = str(tmp_path / "old.db")
    raw = sqlite3.connect(dbp)
    raw.execute("CREATE TABLE videos (video_id TEXT PRIMARY KEY, member TEXT, branch TEXT, "
                "lang TEXT, title TEXT, published_at TEXT, url TEXT, sub_kind TEXT)")
    raw.execute("INSERT INTO videos(video_id, member) VALUES('x','Sakura Miko')")
    raw.commit(); raw.close()

    conn = db.connect(dbp)
    db.init_db(conn)  # 冪等マイグレーションで member_ja を追加
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    assert "member_ja" in cols
    conn.close()


def _mini_db(tmp_path, rows):
    """(member, text) の列から小さな検索用DBを作る。"""
    from pipeline import db, build_index

    dbp = str(tmp_path / "mini.db")
    conn = db.connect(dbp)
    db.init_db(conn)
    for i, (member, text) in enumerate(rows):
        vid = f"v{i}"
        build_index.upsert_video(conn, {
            "video_id": vid, "member": member, "member_ja": "", "branch": "jp",
            "lang": "ja", "title": "t", "published_at": "20240101", "url": "u", "sub_kind": "auto",
        })
        build_index.replace_segments(conn, vid, "ja", [{"start": 0.0, "dur": 1.0, "text": text}])
    conn.commit()
    return conn


def test_normalization_space_in_autocaption(tmp_path):
    """単語途中の空白（ASR由来）を吸収して一致する。"""
    conn = _mini_db(tmp_path, [("A", "本日はあり がとう ございます")])
    assert search.search(conn, "ありがとう")["total"] == 1
    conn.close()


def test_normalization_katakana_hiragana(tmp_path):
    """カタカナ/ひらがなの表記ゆれを吸収する。"""
    conn = _mini_db(tmp_path, [("A", "ペコラだいすき")])
    assert search.search(conn, "ぺこら")["total"] == 1   # クエリはひらがな
    assert search.search(conn, "ペコラ")["total"] == 1   # クエリはカタカナ
    conn.close()


def test_normalization_fullwidth(tmp_path):
    conn = _mini_db(tmp_path, [("A", "ＨＥＬＬＯ world")])
    assert search.search(conn, "hello")["total"] == 1
    conn.close()


def test_multi_term_and(tmp_path):
    """空白区切りは AND（すべての語を含むセグメントだけ）。"""
    conn = _mini_db(tmp_path, [
        ("A", "みこ が 歌う 配信"),
        ("B", "みこ の 雑談"),
        ("C", "ぺこら の 歌"),
    ])
    assert search.search(conn, "みこ 歌")["total"] == 1   # A のみ
    assert search.search(conn, "歌")["total"] == 2         # A, C
    assert search.search(conn, "みこ ぺこら")["total"] == 0  # 両方を含む発話は無い
    conn.close()


def test_stats(conn):
    s = search.stats(conn)
    assert s["videos"] >= 1
    assert s["segments"] >= 1
    assert s["members"] >= 1
    assert s["by_branch"]  # ブランチ別の内訳がある
    assert sum(s["by_branch"].values()) == s["videos"]

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
    assert "Sakura Miko" in f["members"]
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

"""web/api.js（静的モード）を実際に node で動かして検証する。

これまでクライアントの検索は Python に写した版（test_export._client_search）で
照合していたが、表記ゆれの展開はブラウザ側にしか無いので、実物の api.js を
書き出したサイトに対して動かして確かめる。node が無い環境ではスキップする。
"""

import functools
import gzip
import http.server
import json
import os
import shutil
import subprocess
import threading

import pytest

from pipeline import db, export_static

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node が無い")

API_JS = os.path.join(os.path.dirname(__file__), os.pardir, "web", "api.js")

# api.js をブラウザ相当の環境で読み込み、渡された呼び出しを順に実行して JSON で返す
# （node -e では process.argv にスクリプトのパスが入らないので、引数は [1] から）
RUNNER = r"""
globalThis.window = globalThis;
window.HOLOGLISH_INDEX_BASE = process.argv[1];
eval(require("fs").readFileSync(process.argv[2], "utf8"));
(async () => {
  const out = [];
  for (const [fn, arg] of JSON.parse(process.argv[3])) out.push(await window.Api[fn](...arg));
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""

# 表記ゆれ展開の検証用に、読み付きの語彙を差し替える（フィクスチャは小さく、
# 標本からの語彙抽出では足切りに掛かるため）。表記はフィクスチャ本文に実在する。
VOCAB = {
    "version": 3,
    "items": [
        {"t": "今日", "n": 9, "r": ["きょう"]},
        {"t": "配信", "n": 9, "r": ["はいしん"]},
        {"t": "元気", "n": 9, "r": ["げんき"]},
        # 英語の字幕に出てくる語（ローマ字の「もしかして」を出さない判定に使う）
        {"t": "hello", "n": 9},
        {"t": "made", "n": 9},
        {"t": "まで", "n": 9},
        # 助詞が連なっただけの断片（nine → にね）。頻度が高くても提案しない
        {"t": "にね", "n": 50, "f": 1},
        {"t": "おはよう", "n": 8},
        {"t": "ありがとう", "n": 7},
        # 読み付け器ごとに読みが割れる語（短い方は誤読）。補完には長い方を見せる
        {"t": "可愛い", "n": 7, "r": ["かわい", "かわいい"]},
        {"t": "かわいい", "n": 6},  # かな書きもよく使われる語（漢字の検索に足す対象）
        # 活用形と原形（本文: 今日も配信始めていきます / 最初の曲いきます）
        {"t": "始めて", "n": 6, "b": "始める"},
        {"t": "いきます", "n": 6, "b": "いく"},
        {"t": "こんにちは", "n": 5},
        {"t": "ペコラ", "n": 5},
    ],
}


@pytest.fixture
def site(built_db, tmp_path):
    out = str(tmp_path / "site")
    conn = db.connect(built_db)
    export_static.export_site(conn, out)
    conn.close()
    with gzip.open(os.path.join(out, "static", "idx", "suggest.json.gz"), "wt", encoding="utf-8") as f:
        json.dump(VOCAB, f, ensure_ascii=False)

    handler = functools.partial(
        type("Quiet", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None}),
        directory=out,
    )
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/static/idx"
    srv.shutdown()


@pytest.fixture
def site_many_buckets(built_db, tmp_path, monkeypatch):
    """索引を極端に細かく分けて書き出したサイト（バケット単位の取得を検証する）。"""
    monkeypatch.setattr(export_static, "BUCKET_TARGET_BYTES", 1)
    out = str(tmp_path / "site_many")
    conn = db.connect(built_db)
    export_static.export_site(conn, out)
    conn.close()
    handler = functools.partial(
        type("Quiet", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None}),
        directory=out,
    )
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/static/idx", out
    srv.shutdown()


def run(base, calls):
    proc = subprocess.run(
        [NODE, "-e", RUNNER, base, os.path.abspath(API_JS), json.dumps(calls, ensure_ascii=False)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def search(base, q, **kw):
    return run(base, [["search", [dict(q=q, page_size=100, **kw)]]])[0]


def test_matches_server_without_expansion(site, built_db):
    """展開しなければ、実物の api.js の件数がサーバ検索(FTS5)と一致する。"""
    from server import search as server

    conn = db.connect(built_db)
    for q in ["おはよう", "今日", "配信", "ぺこ", "hello", "おはよう 元気"]:
        want = server.search(conn, q, page_size=100)["total"]
        assert search(site, q)["total"] == want, q
    conn.close()


def test_kana_query_finds_kanji_spelling(site):
    """「げんき」で、字幕に「元気」と書かれた発話が見つかる（これが本題）。"""
    assert search(site, "げんき")["total"] == 0          # 展開しなければ取りこぼす
    r = search(site, "げんき", expand=True)
    assert r["total"] == 1
    assert r["variants"] == [["元気"]]                   # 何を含めたかを返す
    assert "元気" in r["results"][0]["snippet"]


def test_kanji_query_adds_kana_spelling_only_when_it_is_common(site):
    # 逆向き: 漢字で打つと、その読み（かな表記）も含める。ただし、かな表記が語彙に
    # 実在する（よく使われる）ときだけ。まれなかな書きのために索引を余計に取らない。
    assert search(site, "可愛い", expand=True)["variants"] == [["かわいい"]]  # 語彙に実在
    assert search(site, "元気", expand=True)["variants"] == [[]]               # げんき は語彙に無い


def test_expansion_matches_kanji_search_exactly(site):
    # 「はいしん」の展開結果は「配信」で検索したのと同じ発話の集合になる
    a = search(site, "はいしん", expand=True)
    b = search(site, "配信")
    key = lambda r: [(x["video_id"], x["start"]) for x in r["results"]]
    assert a["total"] == b["total"] > 0
    assert key(a) == key(b)


def test_each_term_expands_independently(site):
    # 複数語は語ごとに展開して AND（「きょう はいしん」=「今日」かつ「配信」）
    r = search(site, "きょう はいしん", expand=True)
    assert r["variants"] == [["今日"], ["配信"]]
    assert r["total"] == search(site, "今日 配信")["total"] == 1


def test_single_kana_is_not_expanded(site):
    # 1文字の読み（「き」→ 木・気 …）は同音が多すぎるので展開しない
    assert search(site, "き", expand=True)["variants"] == [[]]


def test_suggest_matches_reading_prefix(site):
    got = run(site, [["suggest", ["げん"]], ["suggest", ["元"]], ["suggest", ["おは"]], ["suggest", ["配信"]]])
    assert got[0] == [{"t": "元気", "reading": "げんき"}]   # 読みで当たった
    assert got[1] == [{"t": "元気", "reading": None}]       # 表記で当たった
    assert got[2] == [{"t": "おはよう", "reading": None}]
    assert got[3] == []                                      # 打った語そのものは出さない


def test_suggest_shows_the_longest_matching_reading(site):
    got = run(site, [["suggest", ["かわ"]]])[0]
    assert {"t": "可愛い", "reading": "かわいい"} in got



def internals(base, fn, args):
    """api.js の内部関数（純粋関数）を直接呼ぶ。"""
    js = r"""
globalThis.window = globalThis;
window.HOLOGLISH_INDEX_BASE = process.argv[1];
eval(require("fs").readFileSync(process.argv[2], "utf8"));
const [fn, args] = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify(args.map((a) => window.Api._internals[fn](a))));
"""
    proc = subprocess.run(
        [NODE, "-e", js, base, os.path.abspath(API_JS), json.dumps([fn, args], ensure_ascii=False)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ---------- のばし棒・小書き文字 ----------

def test_loose_form_drops_colloquial_lengthening(site):
    got = internals(site, "looseForm", ["おはよー", "すごーーい", "すごぉい", "やばっ", "かわいー〜", "おはよう"])
    assert got == ["おはよ", "すごい", "すごい", "やば", "かわい", "おはよう"]


def test_loose_form_keeps_katakana_long_vowel(site):
    # カタカナ語の「ー」は語の一部（外すと コーヒー → こひ で誤ヒットする）
    assert internals(site, "looseForm", ["コーヒー", "ラーメン"]) == ["こーひー", "らーめん"]


def test_long_vowel_query_reaches_standard_spelling(site):
    """「おはよー」で、字幕に「おはよう」と書かれた発話が見つかる。"""
    assert search(site, "おはよー")["total"] == 0
    r = search(site, "おはよー", expand=True)
    assert r["variants"] == [["おはよ"]]
    assert r["total"] == search(site, "おはよ")["total"] == 3


# ---------- 活用形 ----------

def test_inflection_is_opt_in(site):
    # 「始める」は本文に無い（「始めて」はある）。活用形を含めたときだけ届く
    assert search(site, "始める", expand=True)["total"] == 0
    r = search(site, "始める", expand=True, inflect=True)
    assert r["total"] == 1
    assert "始めて" in r["variants"][0]


def test_inflection_works_without_spelling_variants(site):
    # 表記ゆれを切っても活用形だけ広げられる（2つのチェックは独立）
    r = search(site, "いく", inflect=True)
    assert r["variants"] == [["いきます"]]
    assert r["total"] == 2  # 始めていきます / 最初の曲いきます


# ---------- ローマ字の「もしかして」 ----------

def test_romaji_to_kana(site):
    words = ["arigatou", "konnichiwa", "kitte", "matcha", "shinbun", "kon'ya", "onna",
             "pekora", "otsukare", "ra-men", "hello"]
    assert internals(site, "romajiToKana", words) == [
        "ありがとう", "こんにちわ", "きって", "まっちゃ", "しんぶん", "こんや", "おんな",
        "ぺこら", "おつかれ", "らーめん", None,
    ]


def test_romaji_query_gets_a_suggestion(site):
    got = {q: search(site, q)["suggestion"] for q in ["ohayou", "arigatou", "konnichiwa", "pekora"]}
    assert got == {
        "ohayou": "おはよう",
        "arigatou": "ありがとう",
        "konnichiwa": "こんにちは",  # 助詞の「は」を wa と打つ
        "pekora": "ペコラ",          # 語彙の表記で出す
    }


def test_english_words_get_no_suggestion(site):
    # made はかなに綺麗に変換でき（まで）語彙にも実在するが、英語として使われている
    # 語には、日本語側の頻度が十分に高くない限り出さない
    assert internals(site, "romajiToKana", ["made"]) == ["まで"]  # 判定まで到達する前提の確認
    assert search(site, "made")["suggestion"] is None


def test_fragments_get_no_suggestion(site):
    # にね は語彙に実在し頻度も高いが、助詞で始まる断片なので「もしかして」にしない
    assert internals(site, "romajiToKana", ["nine"]) == ["にね"]  # 判定まで到達する前提の確認
    assert search(site, "nine")["suggestion"] is None
    assert search(site, "hello")["suggestion"] is None  # そもそもかなにならない
    assert search(site, "おはよう")["suggestion"] is None



def test_minimal_cover(site):
    """照合では、ほかの表記を含む表記を外す（当たる発話は同じで、取得が減る）。"""
    got = internals(site, "minimalCover", [["可愛い", "かわいい", "かわい"], ["おはよー", "おはよ"], ["楽しい"]])
    assert got == [["可愛い", "かわい"], ["おはよ"], ["楽しい"]]


def test_minimal_cover_keeps_results_identical(site):
    # 「おはよー」は照合上 [おはよ] だけになるが、結果も表示も変わらない
    r = search(site, "おはよー", expand=True)
    assert r["variants"] == [["おはよ"]]
    assert r["total"] == search(site, "おはよ")["total"] == 3



def test_results_do_not_depend_on_bucket_count(site_many_buckets, built_db):
    """分割数をデータ量で変えても、実物の api.js の検索結果はサーバ検索と一致する。"""
    from server import search as server

    base, out = site_many_buckets
    man = json.load(open(os.path.join(out, "static", "idx", "manifest.json"), encoding="utf-8"))
    assert man["tri_buckets"] > export_static.MIN_BUCKETS  # 本当に細かく分かれている前提の確認
    conn = db.connect(built_db)
    for q in ["おはよう", "今日", "配信", "ぺこ", "hello", "おはよう 元気"]:
        assert search(base, q)["total"] == server.search(conn, q, page_size=100)["total"], q
    conn.close()

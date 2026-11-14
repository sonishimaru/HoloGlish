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
    "version": 2,
    "items": [
        {"t": "今日", "n": 9, "r": ["きょう"]},
        {"t": "配信", "n": 9, "r": ["はいしん"]},
        {"t": "元気", "n": 9, "r": ["げんき"]},
        {"t": "おはよう", "n": 8},
        # 読み付け器ごとに読みが割れる語（短い方は誤読）。補完には長い方を見せる
        {"t": "可愛い", "n": 7, "r": ["かわい", "かわいい"]},
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


def test_kanji_query_also_finds_kana_spelling(site):
    # 逆向き: 漢字で打つと、その読み（かな表記）も含める
    r = search(site, "元気", expand=True)
    assert r["variants"] == [["げんき"]]
    assert r["total"] == search(site, "元気")["total"]   # フィクスチャに かな表記は無い


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

"""収集済み索引(SQLite)を静的サイトへ書き出す。

サーバ無しで、ブラウザ内(クライアントサイド)で検索できる静的サイトを生成する。
GitHub Pages 等にそのまま公開でき、収集した索引を「キャッシュ」として持ち歩ける。

設計（索引 version 5）— 「必要な発話だけを取りに行く」:
  - **n-gram 索引が「どの動画に在るか」まで持つ**。動画を投稿日の新しい順に
    並べ、MASK_GROUP 本ずつの「グループ」に分ける。索引は
    gram → [group, mask, group, mask, ...] の平坦配列で、mask はそのグループ内の
    どの動画にその gram が在るかを示すビット列。
    動画IDを直接並べるより桁違いに小さく、かつ動画単位まで絞り込める。
  - **本文は動画単位のファイル**（v/<video_id>.json）。クライアントは索引で
    絞った候補動画だけを、新しい順に、必要件数が埋まるまで取りに行く。
    その語を含まない動画はダウンロードしない。
  - 1文字クエリ用に uni（1-gram）索引も持つため、1文字でも全走査しない。
  - メンバー/ブランチは manifest の動画別配列で判定するので、絞り込み検索でも
    候補を先に間引ける。
  - 照合は**正規化テキスト**（normalize.py: NFKC・小文字化・空白除去・カナ→かな）。

出力構成（out_dir 直下）:
  index.html
  static/{app.js, api.js, style.css, config.js}
  static/idx/manifest.json      版・動画一覧(新しい順)・動画別メンバー/ブランチ・facets・stats
  static/idx/uni/<k>.json       1-gram → [group, mask, ...]
  static/idx/bi/<k>.json        2-gram → [group, mask, ...]
  static/idx/tri/<k>.json       3-gram → [group, mask, ...]
  static/idx/v/<video_id>.json  {meta, segs}
  static/idx/suggest.json       入力補完の候補語彙（実際に話されている言い回し）
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from array import array
from typing import Any, Callable, Dict, List, Optional, Set

from .normalize import normalize
from .suggest import SAMPLE_MAX, build_suggestions
from server import search as _search

WEB_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "web")

_ASSETS = ["app.js", "api.js", "style.css"]

# マスク1つが受け持つ動画数。JS のビット演算は 32bit 符号付きなので 30 未満に保つ。
MASK_GROUP = 24

# n-gram 索引の分割数。1バケット = 全語彙 / この数。
# 検索1回で必ず読むので、実データ(777万発話・異なりトリグラム298万)で
# 1バケットが 30KB台(gzip) に収まる値にしてある。分割を増やしてもファイル数が
# 増えるだけで総量は変わらない（取得するのはクエリに出てくる gram のバケットだけ）。
UNI_BUCKETS = 64
BI_BUCKETS = 512
TRI_BUCKETS = 2048

INDEX_VERSION = 5


def _ngrams(text: str, n: int) -> Set[str]:
    return {text[i : i + n] for i in range(len(text) - n + 1)} if len(text) >= n else set()


def gram_bucket(gram: str, buckets: int) -> int:
    """gram を安定ハッシュでバケットへ割り当てる。

    web/api.js の gramBucket と同じ計算（FNV-1a 32bit / UTF-8 バイト列）。
    ブラウザ側に依存を持ち込まずに同じ分配を再現できるものを選んでいる。
    """
    h = 2166136261
    for byte in gram.encode("utf-8"):
        h ^= byte
        h = (h * 16777619) & 0xFFFFFFFF
    return h % buckets


def _ordered_vids(videos: Dict[str, Dict[str, Any]]) -> List[str]:
    """投稿日の新しい順に並べる（日付なしは末尾）。同日は video_id で安定化。"""
    return sorted(
        videos,
        key=lambda v: (videos[v].get("published_at") or "", v),
        reverse=True,
    )


def _video_meta(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    videos: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT video_id, member, member_ja, branch, lang, title, url, published_at, sub_kind FROM videos"
    ):
        videos[r["video_id"]] = {
            "member": r["member"] or "",
            "member_ja": r["member_ja"] or "",
            "branch": r["branch"] or "",
            "lang": r["lang"] or "",
            "title": r["title"] or "",
            "url": r["url"] or f"https://www.youtube.com/watch?v={r['video_id']}",
            "published_at": r["published_at"] or "",
            "sub_kind": r["sub_kind"] or "",
        }
    return videos


def _segs_for(conn: sqlite3.Connection, video_id: str) -> List[List[Any]]:
    return [
        [r["start"], r["dur"], r["text"], r["lang"] or ""]
        for r in conn.execute(
            "SELECT start, dur, text, lang FROM segments WHERE video_id = ? ORDER BY start",
            (video_id,),
        )
    ]


def _build_index(
    conn: sqlite3.Connection,
    on_video: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """manifest と n-gram 索引を、動画1本ずつのストリーミングで構築する。

    全発話を一括でメモリへ載せる作りだと、数千万発話の実データで Pages ビルドの
    ランナーが物理メモリを使い切って落ちる（2026-09-25 に OOM で2連続失敗）。
    動画単位に読み → n-gram を索引へ反映 → on_video へ渡して手放す、を繰り返す。
    postings は [group, mask, ...] の平坦な C 配列（array）で持ち、Python
    オブジェクトのオーバーヘッドを避ける。動画は新しい順に処理するため
    group は単調増加で、配列は常に整列済み。
    """
    videos = _video_meta(conn)
    vids = _ordered_vids(videos)

    facets = _search.facets(conn)
    member_ix = {m["value"]: i for i, m in enumerate(facets.get("members", []))}
    branch_ix = {b: i for i, b in enumerate(facets.get("branches", []))}

    # gram → array([group, mask, group, mask, ...])
    uni: Dict[str, array] = {}
    bi: Dict[str, array] = {}
    tri: Dict[str, array] = {}

    seg_total = 0
    for i, vid in enumerate(vids):
        group, bit = i // MASK_GROUP, 1 << (i % MASK_GROUP)
        segs = _segs_for(conn, vid)
        seg_total += len(segs)
        g1: Set[str] = set()
        g2: Set[str] = set()
        g3: Set[str] = set()
        for seg in segs:
            norm = normalize(seg[2])
            g1 |= set(norm)
            g2 |= _ngrams(norm, 2)
            g3 |= _ngrams(norm, 3)
        for index, grams in ((uni, g1), (bi, g2), (tri, g3)):
            for g in grams:
                a = index.get(g)
                if a is None:
                    index[g] = array("i", (group, bit))
                elif a[-2] == group:
                    a[-1] |= bit
                else:
                    a.append(group)
                    a.append(bit)
        if on_video is not None:
            on_video(vid, {"meta": videos[vid], "segs": segs})

    manifest = {
        "version": INDEX_VERSION,
        "videos": len(vids),
        "segments": seg_total,
        "mask_group": MASK_GROUP,
        "uni_buckets": UNI_BUCKETS,
        "bi_buckets": BI_BUCKETS,
        "tri_buckets": TRI_BUCKETS,
        # 新しい順の動画ID。索引の group/mask はこの並びの位置を指す。
        "vids": vids,
        # 絞り込みを索引段階で効かせるための動画別メンバー/ブランチ（facets 内の位置）
        "vmem": [member_ix.get(videos[v]["member"], -1) for v in vids],
        "vbr": [branch_ix.get(videos[v]["branch"], -1) for v in vids],
        "facets": facets,
        "stats": _search.stats(conn),
    }
    return {
        "manifest": manifest,
        "uni_index": uni,
        "bi_index": bi,
        "tri_index": tri,
    }


def build_index_files(conn: sqlite3.Connection) -> Dict[str, Any]:
    """索引一式（manifest / uni・bi・tri 索引 / 動画別データ）を返す。

    全動画の本文をメモリに持つため、テスト・小規模データ用。
    実データの書き出しは :func:`export_site`（ストリーミング）を使う。
    """
    payloads: Dict[str, Dict[str, Any]] = {}
    idx = _build_index(conn, lambda vid, p: payloads.__setitem__(vid, p))
    for key in ("uni_index", "bi_index", "tri_index"):
        idx[key] = {g: list(a) for g, a in idx[key].items()}
    idx["videos"] = payloads
    return idx


def _split_grams(index: Dict[str, Any], buckets: int) -> Dict[int, Dict[str, Any]]:
    """gram 索引をハッシュでバケットへ分割する。postings は list / array どちらでも。"""
    out: Dict[int, Dict[str, Any]] = {}
    for gram, postings in index.items():
        out.setdefault(gram_bucket(gram, buckets), {})[gram] = postings
    return out


def export_site(conn: sqlite3.Connection, out_dir: str) -> Dict[str, Any]:
    """静的サイトを out_dir へ書き出し、簡単な統計を返す。"""
    static_dir = os.path.join(out_dir, "static")
    idx_dir = os.path.join(static_dir, "idx")
    os.makedirs(idx_dir, exist_ok=True)
    # 前回の索引を消す（構成が変わっても古いファイルが残らないように）
    for f in os.listdir(idx_dir):
        p = os.path.join(idx_dir, f)
        if f.endswith(".json"):
            os.remove(p)
        elif os.path.isdir(p) and f in ("uni", "bi", "tri", "v"):
            shutil.rmtree(p)

    for name in _ASSETS:
        shutil.copyfile(os.path.join(WEB_DIR, name), os.path.join(static_dir, name))

    with open(os.path.join(static_dir, "config.js"), "w", encoding="utf-8") as f:
        f.write(
            "// 自動生成: 静的サイト用の設定（このファイルがあると api.js は静的モードで動く）\n"
            "window.HOLOGLISH_INDEX_BASE = 'static/idx';\n"
        )

    def _dump(path: str, obj: Any) -> None:
        with open(path, "w", encoding="utf-8") as fp:
            # default=list: postings の array を書き出し時にだけ list 化する
            json.dump(obj, fp, ensure_ascii=False, separators=(",", ":"), default=list)

    # 本文は動画単位。候補になった動画だけを取得できるようにする。
    # 索引構築のストリーミング中にその場で書き出し、メモリへ溜め込まない。
    v_dir = os.path.join(idx_dir, "v")
    os.makedirs(v_dir, exist_ok=True)

    # 入力補完のサンプリング（全文リストを作らず、書き出しついでに間引いて拾う）
    total = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    stride = max(1, total // SAMPLE_MAX)
    sample_texts: List[str] = []
    seen = 0

    def _write_video(vid: str, payload: Dict[str, Any]) -> None:
        nonlocal seen
        _dump(os.path.join(v_dir, f"{vid}.json"), payload)
        for seg in payload["segs"]:
            if seen % stride == 0:
                sample_texts.append(seg[2])
            seen += 1

    idx = _build_index(conn, _write_video)

    _dump(os.path.join(idx_dir, "manifest.json"), idx["manifest"])

    # n-gram 索引はバケット分割して書き出す（検索時に必要なバケットだけ取得する）
    for sub, index, buckets in (
        ("uni", idx["uni_index"], UNI_BUCKETS),
        ("bi", idx["bi_index"], BI_BUCKETS),
        ("tri", idx["tri_index"], TRI_BUCKETS),
    ):
        sub_dir = os.path.join(idx_dir, sub)
        os.makedirs(sub_dir, exist_ok=True)
        parts = _split_grams(index, buckets)
        for k in range(buckets):
            _dump(os.path.join(sub_dir, f"{k}.json"), parts.get(k, {}))

    # 入力補完の候補語彙。「何を検索できるか」が分からない状態を避けるため、
    # 実際に話されている言い回しだけを候補にする（選べば必ずヒットする）。
    _dump(os.path.join(idx_dir, "suggest.json"), build_suggestions(sample_texts))

    # 収集状況（ライバー別の完了/未収集）を Pages にも同梱し、安定URLで配信する。
    # （Google スプレッドシートの Apps Script はこの JSON を取得して自動更新する）
    from .coverage import build_coverage
    _dump(os.path.join(out_dir, "coverage.json"), build_coverage(conn))

    with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/static/", "static/")
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    stats = idx["manifest"]["stats"]
    return {
        "out_dir": out_dir,
        "videos": stats["videos"],
        "segments": stats["segments"],
        "members": stats["members"],
        "shards": idx["manifest"]["videos"],  # 互換: 出力単位の数（動画数）
    }

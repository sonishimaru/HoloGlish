"""収集済み索引(SQLite)を静的サイトへ書き出す。

サーバ無しで、ブラウザ内(クライアントサイド)で検索できる静的サイトを生成する。
GitHub Pages 等にそのまま公開でき、収集した索引を「キャッシュ」として持ち歩ける。

スケーリング & 検索品質:
  - 全セグメントを1 JSON に載せると全アーカイブで数十MBになるため、動画を
    N シャードに分割し、各シャードに「メタ・セグメント」を格納する。
  - **シャードは投稿日の新しい順に詰める**（shard 0 が最新）。既定の並び順
    （新着順）ではクライアントが shard 0 から順に見るだけでよく、必要件数が
    埋まった時点で打ち切れる（＝よくある語でも全シャードを取りに行かない）。
  - 照合は**正規化テキスト**（normalize.py: NFKC・小文字化・空白除去・カナ→かな）で
    行い、n-gram → 該当シャードのグローバル索引で絞る。
  - **n-gram 索引はバケット分割して配信する**。全語彙をまとめた 1 ファイルは
    数十MBに達し、検索前に必ず読む起動コストになるため、gram のハッシュで
    分割し「クエリに出てくる gram のバケットだけ」を取得する。
  - シャードごとのメンバー/ブランチ一覧を manifest に持ち、絞り込み検索では
    そのメンバーを含むシャードだけを取得する。

出力構成（out_dir 直下）:
  index.html
  static/{app.js, api.js, style.css, config.js}
  static/idx/manifest.json      版・シャード数・facets・stats・件数・シャード別メンバー
  static/idx/tri/<k>.json       3-gram → シャード番号配列（gram ハッシュで分割）
  static/idx/bi/<k>.json        2-gram → シャード番号配列（同上）
  static/idx/shard-<b>.json     {vids, meta, segs}
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from typing import Any, Dict, List, Set

from .normalize import normalize
from server import search as _search

WEB_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "web")

_ASSETS = ["app.js", "api.js", "style.css"]

VIDEOS_PER_SHARD = 25
# シャード数の上限。1シャードが肥大化すると1回の取得が重くなるため、
# データが増えてもシャードは小さいまま数を増やす（取得は必要な分だけ）。
MAX_SHARDS = 1024

# n-gram 索引の分割数。1バケット = 全語彙 / この数。
TRI_BUCKETS = 512
BI_BUCKETS = 64

INDEX_VERSION = 4


def _ngrams(text: str, n: int) -> Set[str]:
    return {text[i : i + n] for i in range(len(text) - n + 1)} if len(text) >= n else set()


def _shard_count(num_videos: int) -> int:
    if num_videos <= 0:
        return 1
    n = (num_videos + VIDEOS_PER_SHARD - 1) // VIDEOS_PER_SHARD
    return max(1, min(MAX_SHARDS, n))


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


def build_index_files(conn: sqlite3.Connection) -> Dict[str, Any]:
    """シャード索引一式（manifest / tri_index / bi_index / shards）を返す。"""
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

    segs_by_video: Dict[str, List[List[Any]]] = {vid: [] for vid in videos}
    seg_total = 0
    for r in conn.execute(
        "SELECT video_id, lang, start, dur, text FROM segments ORDER BY video_id, start"
    ):
        if r["video_id"] not in segs_by_video:
            continue
        segs_by_video[r["video_id"]].append([r["start"], r["dur"], r["text"], r["lang"] or ""])
        seg_total += 1

    vids = _ordered_vids(videos)
    n = _shard_count(len(vids))
    shards: Dict[int, Dict[str, Any]] = {b: {"vids": [], "meta": [], "segs": []} for b in range(n)}
    tri_index: Dict[str, Set[int]] = {}
    bi_index: Dict[str, Set[int]] = {}

    facets = _search.facets(conn)
    member_ix = {m["value"]: i for i, m in enumerate(facets.get("members", []))}
    branch_ix = {b: i for i, b in enumerate(facets.get("branches", []))}
    shard_members: List[Set[int]] = [set() for _ in range(n)]
    shard_branches: List[Set[int]] = [set() for _ in range(n)]

    # 新しい順に VIDEOS_PER_SHARD 本ずつ詰める（shard 0 が最新）。
    # シャード数が上限に達した場合は残りを最後のシャードへ寄せる。
    for i, vid in enumerate(vids):
        b = min(i // VIDEOS_PER_SHARD, n - 1)
        sh = shards[b]
        meta = videos[vid]
        sh["vids"].append(vid)
        sh["meta"].append(meta)
        seglist = segs_by_video.get(vid, [])
        sh["segs"].append(seglist)
        if meta["member"] in member_ix:
            shard_members[b].add(member_ix[meta["member"]])
        if meta["branch"] in branch_ix:
            shard_branches[b].add(branch_ix[meta["branch"]])
        for seg in seglist:
            norm = normalize(seg[2])
            for g in _ngrams(norm, 3):
                tri_index.setdefault(g, set()).add(b)
            for g in _ngrams(norm, 2):
                bi_index.setdefault(g, set()).add(b)

    manifest = {
        "version": INDEX_VERSION,
        "shards": n,
        "videos": len(vids),
        "segments": seg_total,
        # shard 0 が最新。クライアントはこの順に見て、必要件数が揃えば打ち切れる。
        "order": "date_desc",
        "tri_buckets": TRI_BUCKETS,
        "bi_buckets": BI_BUCKETS,
        # 絞り込み検索でシャードを事前に間引くための索引（facets 内の位置）
        "shard_members": [sorted(s) for s in shard_members],
        "shard_branches": [sorted(s) for s in shard_branches],
        "facets": facets,
        "stats": _search.stats(conn),
    }
    return {
        "manifest": manifest,
        "tri_index": {g: sorted(bs) for g, bs in tri_index.items()},
        "bi_index": {g: sorted(bs) for g, bs in bi_index.items()},
        "shards": shards,
    }


def _split_grams(index: Dict[str, List[int]], buckets: int) -> Dict[int, Dict[str, List[int]]]:
    """gram 索引をハッシュでバケットへ分割する。"""
    out: Dict[int, Dict[str, List[int]]] = {}
    for gram, shard_ids in index.items():
        out.setdefault(gram_bucket(gram, buckets), {})[gram] = shard_ids
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
        elif os.path.isdir(p) and f in ("tri", "bi"):
            shutil.rmtree(p)

    for name in _ASSETS:
        shutil.copyfile(os.path.join(WEB_DIR, name), os.path.join(static_dir, name))

    with open(os.path.join(static_dir, "config.js"), "w", encoding="utf-8") as f:
        f.write(
            "// 自動生成: 静的サイト用の設定（このファイルがあると api.js は静的モードで動く）\n"
            "window.HOLOGLISH_INDEX_BASE = 'static/idx';\n"
        )

    idx = build_index_files(conn)

    def _dump(path: str, obj: Any) -> None:
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(obj, fp, ensure_ascii=False, separators=(",", ":"))

    _dump(os.path.join(idx_dir, "manifest.json"), idx["manifest"])

    # n-gram 索引はバケット分割して書き出す（検索時に必要なバケットだけ取得する）
    for sub, index, buckets in (
        ("tri", idx["tri_index"], TRI_BUCKETS),
        ("bi", idx["bi_index"], BI_BUCKETS),
    ):
        sub_dir = os.path.join(idx_dir, sub)
        os.makedirs(sub_dir, exist_ok=True)
        parts = _split_grams(index, buckets)
        for k in range(buckets):
            _dump(os.path.join(sub_dir, f"{k}.json"), parts.get(k, {}))

    for b, shard in idx["shards"].items():
        _dump(os.path.join(idx_dir, f"shard-{b}.json"), shard)

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
        "shards": idx["manifest"]["shards"],
    }

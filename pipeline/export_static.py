"""収集済み索引(SQLite)を静的サイトへ書き出す。

サーバ無しで、ブラウザ内(クライアントサイド)で検索できる静的サイトを生成する。
GitHub Pages 等にそのまま公開でき、収集した索引を「キャッシュ」として持ち歩ける。

スケーリング & 検索品質:
  - 全セグメントを1 JSON に載せると全アーカイブで数十MBになるため、動画を
    ハッシュで N シャードに分割し、各シャードに「メタ・セグメント」を格納。
  - 照合は**正規化テキスト**（normalize.py: NFKC・小文字化・空白除去・カナ→かな）で
    行い、n-gram → 該当シャードのグローバル索引で絞る。
      * tri-index.json … 3-gram → 該当シャード（3文字以上のクエリ用）
      * bi-index.json  … 2-gram → 該当シャード（2文字クエリの高速化）
  - クライアントは「クエリの n-gram を含むシャードだけ」を取得し、シャード内で
    正規化テキストに対する複数語 AND を検証する（サーバと同じ結果）。

出力構成（out_dir 直下）:
  index.html
  static/{app.js, api.js, style.css, config.js}
  static/idx/manifest.json     版・シャード数・facets・stats・件数
  static/idx/tri-index.json    3-gram → シャード番号配列
  static/idx/bi-index.json     2-gram → シャード番号配列
  static/idx/shard-<b>.json    {vids, meta, segs}
"""

from __future__ import annotations

import hashlib
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
MAX_SHARDS = 256


def _ngrams(text: str, n: int) -> Set[str]:
    return {text[i : i + n] for i in range(len(text) - n + 1)} if len(text) >= n else set()


def _shard_count(num_videos: int) -> int:
    if num_videos <= 0:
        return 1
    n = (num_videos + VIDEOS_PER_SHARD - 1) // VIDEOS_PER_SHARD
    return max(1, min(MAX_SHARDS, n))


def _bucket(video_id: str, shards: int) -> int:
    h = hashlib.md5(video_id.encode("utf-8")).hexdigest()[:8]
    return int(h, 16) % shards


def build_index_files(conn: sqlite3.Connection) -> Dict[str, Any]:
    """シャード索引一式（manifest / tri-index / bi-index / shard-*）を返す。"""
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

    vids = sorted(videos)
    n = _shard_count(len(vids))
    shards: Dict[int, Dict[str, Any]] = {b: {"vids": [], "meta": [], "segs": []} for b in range(n)}
    tri_index: Dict[str, Set[int]] = {}
    bi_index: Dict[str, Set[int]] = {}

    for vid in vids:
        b = _bucket(vid, n)
        sh = shards[b]
        sh["vids"].append(vid)
        sh["meta"].append(videos[vid])
        seglist = segs_by_video.get(vid, [])
        sh["segs"].append(seglist)
        for seg in seglist:
            norm = normalize(seg[2])
            for g in _ngrams(norm, 3):
                tri_index.setdefault(g, set()).add(b)
            for g in _ngrams(norm, 2):
                bi_index.setdefault(g, set()).add(b)

    manifest = {
        "version": 3,
        "shards": n,
        "videos": len(vids),
        "segments": seg_total,
        "facets": _search.facets(conn),
        "stats": _search.stats(conn),
    }
    return {
        "manifest": manifest,
        "tri_index": {g: sorted(bs) for g, bs in tri_index.items()},
        "bi_index": {g: sorted(bs) for g, bs in bi_index.items()},
        "shards": shards,
    }


def export_site(conn: sqlite3.Connection, out_dir: str) -> Dict[str, Any]:
    """静的サイトを out_dir へ書き出し、簡単な統計を返す。"""
    static_dir = os.path.join(out_dir, "static")
    idx_dir = os.path.join(static_dir, "idx")
    os.makedirs(idx_dir, exist_ok=True)
    for f in os.listdir(idx_dir):
        if f.endswith(".json"):
            os.remove(os.path.join(idx_dir, f))

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
    _dump(os.path.join(idx_dir, "tri-index.json"), idx["tri_index"])
    _dump(os.path.join(idx_dir, "bi-index.json"), idx["bi_index"])
    for b, shard in idx["shards"].items():
        _dump(os.path.join(idx_dir, f"shard-{b}.json"), shard)

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

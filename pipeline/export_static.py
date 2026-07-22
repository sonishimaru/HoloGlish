"""収集済み索引(SQLite)を静的サイトへ書き出す。

サーバ無しで、ブラウザ内(クライアントサイド)で検索できる静的サイトを生成する。
GitHub Pages 等にそのまま公開でき、収集した索引を「キャッシュ」として持ち歩ける。

スケーリング対応:
  全セグメントを1つの JSON に載せると全アーカイブ（数十万発話）で数十MBになり
  ブラウザが重く/落ちるため、**トリグラム転置インデックスをシャーディング**して書き出す。
  - 動画をハッシュで N シャードに分割し、各シャードに「その動画群の
    メタ・セグメント・トリグラム転置表」を格納（1動画のセグメントは同一シャード）。
  - グローバルな tri-index.json（トリグラム→該当シャード番号）を1つ持つ。
  - クライアントは「クエリのトリグラムを含むシャードだけ」を取得して検索するため、
    全体をDLしない（サーバの FTS5 trigram と同じ部分一致セマンティクス）。

出力構成（out_dir 直下）:
  index.html            web/index.html の /static/ 参照を相対パスへ書き換えたもの
  static/app.js         フロント（サーバ版と共通）
  static/api.js         検索バックエンド抽象（静的モードでシャード索引を検索）
  static/style.css
  static/config.js      静的モード切替（HOLOGLISH_INDEX_BASE を設定）
  static/idx/manifest.json     版・シャード数・facets・stats・件数
  static/idx/tri-index.json    トリグラム → 該当シャード番号の配列
  static/idx/shard-<b>.json    各シャード（vids/meta/segs/tri）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from typing import Any, Dict, List, Set

from server import search as _search

WEB_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "web")

# サーバ版フロントからコピーする静的アセット
_ASSETS = ["app.js", "api.js", "style.css"]

# 1シャードあたりの目安動画数（多いほどシャードは大きく数は少ない）
VIDEOS_PER_SHARD = 25
MAX_SHARDS = 256


def _trigrams(text: str) -> Set[str]:
    """テキストの相異なる3-gram（小文字化）。3文字未満は空集合。"""
    t = text.lower()
    return {t[i : i + 3] for i in range(len(t) - 2)}


def _shard_count(num_videos: int) -> int:
    if num_videos <= 0:
        return 1
    n = (num_videos + VIDEOS_PER_SHARD - 1) // VIDEOS_PER_SHARD
    return max(1, min(MAX_SHARDS, n))


def _bucket(video_id: str, shards: int) -> int:
    """動画IDから決定的にシャード番号を割り当てる（言語非依存の安定ハッシュ）。

    クライアントはこの計算を必要としない（検索は tri-index 経由、文脈は取得済み
    シャードから引く）ため、ここだけで完結してよい。
    """
    h = hashlib.md5(video_id.encode("utf-8")).hexdigest()[:8]
    return int(h, 16) % shards


def build_index_files(conn: sqlite3.Connection) -> Dict[str, Any]:
    """シャード索引一式（manifest / tri-index / shard-*）を組み立てて返す。

    戻り値: {"manifest": {...}, "tri_index": {...}, "shards": {b: {...}}}
    """
    # 動画メタ
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

    # 動画ごとのセグメント（時刻順）
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

    # 各シャードを構築（vids / meta / segs は並行配列、tri はシャード内転置表）
    shards: Dict[int, Dict[str, Any]] = {
        b: {"vids": [], "meta": [], "segs": [], "tri": {}} for b in range(n)
    }
    tri_index: Dict[str, Set[int]] = {}

    for vid in vids:
        b = _bucket(vid, n)
        sh = shards[b]
        vi = len(sh["vids"])  # このシャード内での動画ローカル番号
        sh["vids"].append(vid)
        sh["meta"].append(videos[vid])
        seglist = segs_by_video.get(vid, [])
        sh["segs"].append(seglist)
        for si, seg in enumerate(seglist):
            for tri in _trigrams(seg[2]):
                sh["tri"].setdefault(tri, []).append([vi, si])
                tri_index.setdefault(tri, set()).add(b)

    manifest = {
        "version": 2,
        "shards": n,
        "videos": len(vids),
        "segments": seg_total,
        "facets": _search.facets(conn),
        "stats": _search.stats(conn),
    }
    tri_index_out = {tri: sorted(bs) for tri, bs in tri_index.items()}
    return {"manifest": manifest, "tri_index": tri_index_out, "shards": shards}


def export_site(conn: sqlite3.Connection, out_dir: str) -> Dict[str, Any]:
    """静的サイトを out_dir へ書き出し、簡単な統計を返す。"""
    static_dir = os.path.join(out_dir, "static")
    idx_dir = os.path.join(static_dir, "idx")
    os.makedirs(idx_dir, exist_ok=True)
    # 既存シャードが残ると古いデータが混ざるため一旦掃除
    for f in os.listdir(idx_dir):
        if f.endswith(".json"):
            os.remove(os.path.join(idx_dir, f))

    # アセットをコピー
    for name in _ASSETS:
        shutil.copyfile(os.path.join(WEB_DIR, name), os.path.join(static_dir, name))

    # 静的モード設定（シャード索引のベースパス）
    with open(os.path.join(static_dir, "config.js"), "w", encoding="utf-8") as f:
        f.write(
            "// 自動生成: 静的サイト用の設定（このファイルがあると api.js は静的モードで動く）\n"
            "window.HOLOGLISH_INDEX_BASE = 'static/idx';\n"
        )

    # 索引の書き出し
    idx = build_index_files(conn)

    def _dump(path: str, obj: Any) -> None:
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(obj, fp, ensure_ascii=False, separators=(",", ":"))

    _dump(os.path.join(idx_dir, "manifest.json"), idx["manifest"])
    _dump(os.path.join(idx_dir, "tri-index.json"), idx["tri_index"])
    for b, shard in idx["shards"].items():
        _dump(os.path.join(idx_dir, f"shard-{b}.json"), shard)

    # index.html は /static/ 参照を相対パスへ書き換える
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

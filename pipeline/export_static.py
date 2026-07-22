"""収集済み索引(SQLite)を静的サイトへ書き出す。

サーバ無しで、ブラウザ内(クライアントサイド)で検索できる静的サイトを生成する。
GitHub Pages 等にそのまま公開でき、収集した索引を「キャッシュ」として持ち歩ける。

出力構成（out_dir 直下）:
  index.html            web/index.html の /static/ 参照を相対パスへ書き換えたもの
  static/app.js         フロント（サーバ版と共通）
  static/api.js         検索バックエンド抽象（静的モードで data.json を検索）
  static/style.css
  static/config.js      静的モード切替（HOLOGLISH_DATA_URL を設定）
  static/data.json      索引データ（videos / segments / facets / stats）

data.json はサーバの /api/search・/api/context・/api/facets・/api/stats と
同じ結果を JS 側で再現できるだけの情報を持つ。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from typing import Any, Dict

from . import db
from server import search as _search

WEB_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "web")

# サーバ版フロントからコピーする静的アセット
_ASSETS = ["app.js", "api.js", "style.css"]


def build_data(conn: sqlite3.Connection) -> Dict[str, Any]:
    """data.json に載せる索引データを組み立てる。"""
    videos: Dict[str, Any] = {}
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

    # segments はコンパクトなキーで（v=video_id, l=lang, s=start, d=dur, t=text）
    segments = [
        {"v": r["video_id"], "l": r["lang"] or "", "s": r["start"], "d": r["dur"], "t": r["text"]}
        for r in conn.execute(
            "SELECT video_id, lang, start, dur, text FROM segments ORDER BY video_id, start"
        )
    ]

    return {
        "videos": videos,
        "segments": segments,
        "facets": _search.facets(conn),
        "stats": _search.stats(conn),
    }


def export_site(conn: sqlite3.Connection, out_dir: str) -> Dict[str, Any]:
    """静的サイトを out_dir へ書き出し、簡単な統計を返す。"""
    static_dir = os.path.join(out_dir, "static")
    os.makedirs(static_dir, exist_ok=True)

    # アセットをコピー
    for name in _ASSETS:
        shutil.copyfile(os.path.join(WEB_DIR, name), os.path.join(static_dir, name))

    # 静的モード設定
    with open(os.path.join(static_dir, "config.js"), "w", encoding="utf-8") as f:
        f.write(
            "// 自動生成: 静的サイト用の設定（このファイルがあると api.js は静的モードで動く）\n"
            "window.HOLOGLISH_DATA_URL = 'static/data.json';\n"
        )

    # 索引データ
    data = build_data(conn)
    with open(os.path.join(static_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    # index.html は /static/ 参照を相対パスへ書き換える
    with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/static/", "static/")
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    return {
        "out_dir": out_dir,
        "videos": data["stats"]["videos"],
        "segments": data["stats"]["segments"],
        "members": data["stats"]["members"],
    }

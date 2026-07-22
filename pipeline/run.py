"""HoloGlish 収集オーケストレーション（CLI）。

使い方:
  # ライブ収集（YouTube アクセスが必要）
  python -m pipeline.run collect --branch jp --limit 5
  python -m pipeline.run collect --members "Usada Pekora,Sakura Miko" --limit 20

  # ローカル字幕ファイルの取り込み（オフライン / テスト用）
  python -m pipeline.run ingest --manifest data/fixtures/manifest.json

収集は再開可能: 一度 done になった video_id はスキップする（--force で再取得）。
全ブランチ対応の設定を持ちつつ、--branch / --members / --limit で範囲を絞って段階実行できる。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
from typing import List, Dict, Any, Optional

import yaml

from . import build_index, db
from .parse_subs import parse_subtitle_file

CONFIG_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "config", "channels.yaml")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def load_channels(path: str = CONFIG_PATH) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("channels", []) if data else []


def _filter_channels(
    channels: List[Dict[str, Any]], branch: Optional[str], members: Optional[List[str]]
) -> List[Dict[str, Any]]:
    out = channels
    if branch:
        out = [c for c in out if c.get("branch") == branch]
    if members:
        wanted = {m.strip().lower() for m in members}
        out = [c for c in out if c.get("member", "").lower() in wanted]
    return out


def cmd_collect(args: argparse.Namespace) -> int:
    # ライブ収集時のみ yt-dlp を import（オフライン ingest では不要）
    from .fetch_videos import list_channel_videos
    from .fetch_subtitles import fetch_subtitle, fetch_video_meta

    channels = _filter_channels(load_channels(), args.branch, _split(args.members))
    if not channels:
        print("対象チャンネルがありません（--branch / --members を確認）", file=sys.stderr)
        return 1

    conn = db.connect(args.db)
    db.init_db(conn)
    total_segments = 0

    for ch in channels:
        cid, member, branch = ch["channel_id"], ch["member"], ch["branch"]
        lang_order = _lang_order(ch.get("lang"))
        print(f"[channel] {member} ({branch}) {cid}")
        try:
            videos = list_channel_videos(
                cid, limit=args.limit, date_after=args.date_after,
                retries=args.retries, retry_base=args.retry_base,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  ! 一覧取得失敗: {e}", file=sys.stderr)
            continue

        for v in videos:
            vid = v["video_id"]
            if not args.force and db.is_processed(conn, vid):
                continue
            try:
                got = fetch_subtitle(
                    vid, args.raw_dir, lang_order=lang_order,
                    retries=args.retries, retry_base=args.retry_base,
                )
                if not got:
                    db.mark_processed(conn, vid, "no_subs", _now())
                    conn.commit()
                    continue
                sub_path, lang, sub_kind = got
                segments = parse_subtitle_file(sub_path)
                meta = fetch_video_meta(vid, retries=args.retries, retry_base=args.retry_base)
                build_index.upsert_video(
                    conn,
                    {
                        "video_id": vid,
                        "member": member,
                        "branch": branch,
                        "lang": lang,
                        "title": meta.get("title") or v.get("title", ""),
                        "published_at": meta.get("published_at", ""),
                        "url": v.get("url", f"https://www.youtube.com/watch?v={vid}"),
                        "sub_kind": sub_kind,
                    },
                )
                n = build_index.replace_segments(conn, vid, lang, segments)
                db.mark_processed(conn, vid, "done", _now())
                conn.commit()
                total_segments += n
                print(f"  + {vid} [{lang}/{sub_kind}] {n} segments")
            except Exception as e:  # noqa: BLE001
                db.mark_processed(conn, vid, "error", _now())
                conn.commit()
                print(f"  ! {vid} 失敗: {e}", file=sys.stderr)
            time.sleep(args.sleep)  # レート制限（YouTube への配慮）

    print(f"完了: 合計 {total_segments} セグメントを追加/更新")
    conn.close()
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """ローカルの字幕ファイルを取り込む（オフライン / テスト用）。

    manifest.json 形式:
      [
        {"video_id": "...", "member": "...", "branch": "jp", "lang": "ja",
         "title": "...", "published_at": "YYYYMMDD", "sub_kind": "manual",
         "subtitle_file": "相対パス.json3"}
      ]
    """
    with open(args.manifest, "r", encoding="utf-8") as f:
        entries = json.load(f)
    base = os.path.dirname(os.path.abspath(args.manifest))

    conn = db.connect(args.db)
    db.init_db(conn)
    total = 0
    for e in entries:
        vid = e["video_id"]
        sub_path = e["subtitle_file"]
        if not os.path.isabs(sub_path):
            sub_path = os.path.join(base, sub_path)
        segments = parse_subtitle_file(sub_path)
        build_index.upsert_video(
            conn,
            {
                "video_id": vid,
                "member": e.get("member", ""),
                "branch": e.get("branch", ""),
                "lang": e.get("lang", "ja"),
                "title": e.get("title", ""),
                "published_at": e.get("published_at", ""),
                "url": e.get("url", f"https://www.youtube.com/watch?v={vid}"),
                "sub_kind": e.get("sub_kind", "manual"),
            },
        )
        n = build_index.replace_segments(conn, vid, e.get("lang", "ja"), segments)
        db.mark_processed(conn, vid, "done", _now())
        total += n
        print(f"  + {vid} {n} segments")
    conn.commit()
    conn.close()
    print(f"完了: 合計 {total} セグメントを取り込み")
    return 0


def _split(csv: Optional[str]) -> Optional[List[str]]:
    if not csv:
        return None
    return [x for x in (s.strip() for s in csv.split(",")) if x]


def _lang_order(primary: Optional[str]) -> List[str]:
    order = ["ja", "en", "id"]
    if primary and primary in order:
        order.remove(primary)
        order.insert(0, primary)
    return order


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="HoloGlish 収集パイプライン")
    p.add_argument("--db", default=db.DEFAULT_DB, help="SQLite DB パス")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="yt-dlp でライブ収集")
    c.add_argument("--branch", choices=["jp", "en", "id"], help="対象ブランチ")
    c.add_argument("--members", help="カンマ区切りのメンバー名で絞り込み")
    c.add_argument("--limit", type=int, default=10, help="チャンネルあたりの動画数上限")
    c.add_argument("--date-after", help="YYYYMMDD 以降のみ")
    c.add_argument("--raw-dir", default=os.path.join("data", "raw"), help="字幕保存先")
    c.add_argument("--sleep", type=float, default=1.0, help="動画間の待機秒（レート制限）")
    c.add_argument("--retries", type=int, default=3, help="一過性エラー(429等)のリトライ回数")
    c.add_argument("--retry-base", type=float, default=2.0, help="リトライの基本待機秒（指数バックオフ）")
    c.add_argument("--force", action="store_true", help="処理済みも再取得")
    c.set_defaults(func=cmd_collect)

    g = sub.add_parser("ingest", help="ローカル字幕ファイルを取り込み（オフライン）")
    g.add_argument("--manifest", required=True, help="manifest.json パス")
    g.set_defaults(func=cmd_ingest)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

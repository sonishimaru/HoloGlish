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
import random
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
    from .fetch_subtitles import fetch_subtitle

    channels = _filter_channels(load_channels(), args.branch, _split(args.members))
    if not channels:
        print("対象チャンネルがありません（--branch / --members を確認）", file=sys.stderr)
        return 1

    # 1回の実行で全チャンネルを最新側から均等に前進させるため、順序をシャッフルする。
    # （時間予算で途中打ち切りになっても、実行ごとに違う順序で回れば特定チャンネルに
    #   偏らず、繰り返し実行で全チャンネルが均等に奥へ進む。）
    random.shuffle(channels)

    # 列挙の深さ（--list-depth）。0/未指定なら全件を列挙して過去アーカイブへ到達する。
    # 「1回に処理する新規本数」は --limit で別に制御する（列挙深さ != 処理量）。
    list_depth = args.list_depth if args.list_depth and args.list_depth > 0 else None
    per_channel_cap = args.limit if args.limit and args.limit > 0 else None

    conn = db.connect(args.db)
    db.init_db(conn)
    total_segments = 0
    new_total = 0

    # 時間予算（秒）。ジョブのハードタイムアウトで強制中断され公開が中途半端に
    # なるのを避けるため、予算に達したら区切りよく収集を打ち切る（再開可能）。
    budget = args.time_budget if args.time_budget and args.time_budget > 0 else None
    deadline = (time.monotonic() + budget) if budget else None

    def _over_budget() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    stopped = False
    for ch in channels:
        if _over_budget():
            stopped = True
            break
        cid, member, branch = ch["channel_id"], ch["member"], ch["branch"]
        member_ja = ch.get("name_ja") or ""
        lang_order = _lang_order(ch.get("lang"))
        print(f"[channel] {member} ({branch}) {cid}")
        try:
            videos = list_channel_videos(
                cid, limit=list_depth, date_after=args.date_after,
                retries=args.retries, retry_base=args.retry_base,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  ! 一覧取得失敗: {e}", file=sys.stderr)
            continue

        new_count = 0  # このチャンネルで今回処理した新規本数
        for v in videos:
            if _over_budget():
                stopped = True
                break
            vid = v["video_id"]
            if not args.force and db.should_skip(conn, vid):
                continue  # 取得済み/字幕なし確定はスキップし、さらに古い方へ進む
            if per_channel_cap is not None and new_count >= per_channel_cap:
                break  # このチャンネルの1回分の上限に達した（次回さらに奥へ続行）
            new_count += 1
            new_total += 1
            try:
                got = fetch_subtitle(
                    vid, args.raw_dir, lang_order=lang_order,
                    retries=args.retries, retry_base=args.retry_base,
                )
                if not got:
                    db.mark_processed(conn, vid, "no_subs", _now())
                    conn.commit()
                    continue
                # メタはプローブ結果から取得済み（追加の抽出をしない）
                sub_path, lang, sub_kind, meta = got
                segments = parse_subtitle_file(sub_path)
                build_index.upsert_video(
                    conn,
                    {
                        "video_id": vid,
                        "member": member,
                        "member_ja": member_ja,
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
        if stopped:
            break

    if stopped:
        print(f"時間予算({int(budget)}秒)に達したため区切りました（次回続行）: "
              f"新規 {new_total} 本 / 合計 {total_segments} セグメントを追加/更新")
    else:
        print(f"完了: 新規 {new_total} 本 / 合計 {total_segments} セグメントを追加/更新")
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
                "member_ja": e.get("member_ja", ""),
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


def cmd_backfill_names(args: argparse.Namespace) -> int:
    """既存 DB の member_ja を channels.yaml の name_ja で埋める（再収集不要）。"""
    mapping = {c["member"]: (c.get("name_ja") or "") for c in load_channels()}
    conn = db.connect(args.db)
    db.init_db(conn)
    updated = 0
    for member, name_ja in mapping.items():
        if not name_ja:
            continue
        cur = conn.execute(
            "UPDATE videos SET member_ja = ? WHERE member = ? AND (member_ja IS NULL OR member_ja = '')",
            (name_ja, member),
        )
        updated += cur.rowcount
    conn.commit()
    conn.close()
    print(f"完了: {updated} 本の日本語表示名を補完")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """収集済み索引を静的サイトへ書き出す（サーバ不要のブラウザ検索）。"""
    from .export_static import export_site

    conn = db.connect(args.db)
    db.init_db(conn)
    info = export_site(conn, args.out)
    conn.close()
    print(
        f"完了: {info['out_dir']} に静的サイトを書き出し"
        f"（{info['videos']} 本 / {info['segments']} 発話 / {info['members']} メンバー）"
    )
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
    c.add_argument("--limit", type=int, default=10,
                   help="1実行でチャンネルあたり新規に処理する本数の上限（0で無制限＝時間予算まで）")
    c.add_argument("--list-depth", type=int, default=0,
                   help="列挙する本数（新しい順）。0で全件＝過去アーカイブへ到達。処理量は --limit で制御")
    c.add_argument("--date-after", help="YYYYMMDD 以降のみ")
    c.add_argument("--raw-dir", default=os.path.join("data", "raw"), help="字幕保存先")
    c.add_argument("--sleep", type=float, default=1.0, help="動画間の待機秒（レート制限）")
    c.add_argument("--retries", type=int, default=3, help="一過性エラー(429等)のリトライ回数")
    c.add_argument("--retry-base", type=float, default=2.0, help="リトライの基本待機秒（指数バックオフ）")
    c.add_argument("--time-budget", type=float, default=0.0,
                   help="収集の時間予算（秒）。0で無制限。超過時は区切りよく打ち切る（再開可能）")
    c.add_argument("--force", action="store_true", help="処理済みも再取得")
    c.set_defaults(func=cmd_collect)

    g = sub.add_parser("ingest", help="ローカル字幕ファイルを取り込み（オフライン）")
    g.add_argument("--manifest", required=True, help="manifest.json パス")
    g.set_defaults(func=cmd_ingest)

    e = sub.add_parser("export", help="静的サイト（クライアント検索）を書き出す")
    e.add_argument("--out", default="site", help="出力ディレクトリ")
    e.set_defaults(func=cmd_export)

    b = sub.add_parser("backfill-names", help="既存DBの日本語表示名を channels.yaml から補完")
    b.set_defaults(func=cmd_backfill_names)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

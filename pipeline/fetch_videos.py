"""yt-dlp でチャンネルの動画一覧を列挙する。

YouTube Data API のクォータ/キーを使わずに済むよう yt-dlp を利用する。
`extract_flat` で軽量に video_id とタイトルだけを取得する。

チャンネルは複数の「タブ」を持つ:
  - `videos`  … 「動画」タブ（通常のアップロード動画）
  - `streams` … 「ライブ」タブ（配信アーカイブ。歌枠・ゲーム・雑談など）
既定では両方を列挙してマージするため、ライブアーカイブも収集対象になる。
"""

from __future__ import annotations

from itertools import zip_longest
from typing import List, Dict, Any, Optional, Sequence

from yt_dlp import YoutubeDL

from ._net import common_ydl_opts, with_retries

# 既定で参照するチャンネルタブ（「動画」＋「ライブ」）
DEFAULT_TABS: tuple = ("videos", "streams")


def list_channel_videos(
    channel_id: str,
    limit: Optional[int] = None,
    date_after: Optional[str] = None,
    retries: int = 3,
    retry_base: float = 2.0,
    tabs: Sequence[str] = DEFAULT_TABS,
) -> List[Dict[str, Any]]:
    """チャンネルの動画一覧（新しい順）を返す。

    limit: 取得件数上限（None で全件）。マージ後の総件数に対して適用する。
    date_after: 'YYYYMMDD' 以降のみ（yt-dlp の daterange）
    tabs: 参照するタブ（既定は「動画」＋「ライブ」）。
    レート制限は指数バックオフでリトライする。

    複数タブの結果は video_id で重複排除し、各タブの新しい順を保ったまま
    ラウンドロビンでインターリーブする（どのタブも新しい側から均等に前進させるため）。
    """
    lists: List[List[Dict[str, Any]]] = []
    for tab in tabs:
        lists.append(
            _list_channel_tab(
                channel_id, tab, limit=limit, date_after=date_after,
                retries=retries, retry_base=retry_base,
            )
        )
    return _merge_tab_videos(lists, limit=limit)


def _list_channel_tab(
    channel_id: str,
    tab: str,
    limit: Optional[int] = None,
    date_after: Optional[str] = None,
    retries: int = 3,
    retry_base: float = 2.0,
) -> List[Dict[str, Any]]:
    """単一タブ（videos / streams など）の動画一覧を列挙する。

    タブが存在しない・空のチャンネルでも例外を投げず空リストを返す。
    """
    url = f"https://www.youtube.com/channel/{channel_id}/{tab}"
    opts: Dict[str, Any] = {
        "quiet": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
        **common_ydl_opts(),
    }
    if limit:
        opts["playlistend"] = limit
    if date_after:
        opts["daterange"] = _DateRange(date_after)

    def _list():
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = with_retries(_list, retries=retries, base_delay=retry_base)
    videos: List[Dict[str, Any]] = []
    for entry in (info or {}).get("entries", []) or []:
        if not entry or not entry.get("id"):
            continue
        videos.append(
            {
                "video_id": entry["id"],
                "title": entry.get("title") or "",
                "url": entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}",
            }
        )
    return videos


def _merge_tab_videos(
    lists: Sequence[List[Dict[str, Any]]], limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """複数タブの動画リストを、重複排除しつつラウンドロビンで統合する。

    各リストは新しい順。ラウンドロビンにより、どのタブも新しい側から等しく
    先頭付近に現れるため、1実行あたりの処理上限(--limit)が両タブへ行き渡る。
    """
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    for group in zip_longest(*lists):
        for v in group:
            if v is None:
                continue
            vid = v["video_id"]
            if vid in seen:
                continue
            seen.add(vid)
            merged.append(v)
            if limit and len(merged) >= limit:
                return merged
    return merged


class _DateRange:
    """yt-dlp の daterange 互換の簡易実装（after 以降を許可）。"""

    def __init__(self, after: str):
        self.after = after

    def __contains__(self, date: str) -> bool:  # date: 'YYYYMMDD'
        return bool(date) and date >= self.after

"""yt-dlp でチャンネルの動画一覧を列挙する。

YouTube Data API のクォータ/キーを使わずに済むよう yt-dlp を利用する。
`extract_flat` で軽量に video_id とタイトルだけを取得する。
"""

from __future__ import annotations

from typing import List, Dict, Any, Optional

from yt_dlp import YoutubeDL

from ._net import with_retries


def list_channel_videos(
    channel_id: str,
    limit: Optional[int] = None,
    date_after: Optional[str] = None,
    retries: int = 3,
    retry_base: float = 2.0,
) -> List[Dict[str, Any]]:
    """チャンネルの動画一覧（新しい順）を返す。

    limit: 取得件数上限（None で全件）
    date_after: 'YYYYMMDD' 以降のみ（yt-dlp の daterange）
    レート制限は指数バックオフでリトライする。
    """
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    opts: Dict[str, Any] = {
        "quiet": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    if limit:
        opts["playlistend"] = limit
    if date_after:
        opts["daterange"] = _DateRange(date_after)

    videos: List[Dict[str, Any]] = []

    def _list():
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = with_retries(_list, retries=retries, base_delay=retry_base)
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


class _DateRange:
    """yt-dlp の daterange 互換の簡易実装（after 以降を許可）。"""

    def __init__(self, after: str):
        self.after = after

    def __contains__(self, date: str) -> bool:  # date: 'YYYYMMDD'
        return bool(date) and date >= self.after

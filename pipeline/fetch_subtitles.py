"""yt-dlp で 1 動画の字幕を取得する。

方針（ユーザー要件）: 手動字幕を優先し、無ければ自動生成字幕にフォールバック。
言語は ja / en / id を横断で探し、最初に見つかった言語を採用する。
json3 形式（タイムスタンプ付き）で保存する。
"""

from __future__ import annotations

import glob
import os
from typing import Dict, Any, Optional, List, Tuple

from yt_dlp import YoutubeDL

from ._net import common_ydl_opts, with_retries

# 探索する言語の優先順（member の主言語を先頭に差し替えて使う）
DEFAULT_LANG_ORDER = ["ja", "en", "id"]


def _pick_lang(available: Dict[str, Any], order: List[str]) -> Optional[str]:
    for lang in order:
        if lang in available:
            return lang
        # 'en-US' のような地域付きも許容
        for key in available:
            if key.split("-")[0] == lang:
                return key
    return None


def fetch_subtitle(
    video_id: str,
    out_dir: str,
    lang_order: Optional[List[str]] = None,
    retries: int = 3,
    retry_base: float = 2.0,
) -> Optional[Tuple[str, str, str]]:
    """字幕を取得して保存する。

    戻り値: (保存ファイルパス, lang, sub_kind) / 取得できなければ None
    sub_kind は 'manual' または 'auto'。

    レート制限（429 等）は一過性エラーとして指数バックオフでリトライする。
    リトライしても回復しない場合は例外を送出し、呼び出し側で 'error'
    として記録する（次回実行で再取得される）。字幕が存在しない動画は
    None を返し 'no_subs' として記録する（この2つを混同しない）。
    """
    lang_order = lang_order or DEFAULT_LANG_ORDER
    os.makedirs(out_dir, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"

    # まず利用可能な字幕を調べる（download=False）。
    # ignoreerrors=False にして一過性エラーを検知・リトライできるようにする。
    probe_opts = {"quiet": True, "skip_download": True, "ignoreerrors": False, **common_ydl_opts()}

    def _probe():
        with YoutubeDL(probe_opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = with_retries(_probe, retries=retries, base_delay=retry_base)
    if not info:
        return None

    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    lang = _pick_lang(manual, lang_order)
    sub_kind = "manual"
    if not lang:
        lang = _pick_lang(auto, lang_order)
        sub_kind = "auto"
    if not lang:
        return None

    # 実際に該当言語の字幕だけ json3 でダウンロード
    dl_opts = {
        "quiet": True,
        "skip_download": True,
        "ignoreerrors": True,
        "writesubtitles": sub_kind == "manual",
        "writeautomaticsub": sub_kind == "auto",
        "subtitleslangs": [lang],
        "subtitlesformat": "json3",
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        **common_ydl_opts(),
    }

    def _download():
        with YoutubeDL(dl_opts) as ydl:
            return ydl.download([url])

    with_retries(_download, retries=retries, base_delay=retry_base)

    # 保存された字幕ファイルを探す（例: <id>.ja.json3）
    candidates = sorted(glob.glob(os.path.join(out_dir, f"{video_id}.*json3")))
    if not candidates:
        # 一部の言語は vtt でしか出ないことがあるためフォールバック検索
        candidates = sorted(glob.glob(os.path.join(out_dir, f"{video_id}.*vtt")))
    if not candidates:
        return None
    return candidates[0], lang.split("-")[0], sub_kind


def fetch_video_meta(video_id: str, retries: int = 3, retry_base: float = 2.0) -> Dict[str, Any]:
    """タイトル・投稿日など軽量メタを取得。"""
    url = f"https://www.youtube.com/watch?v={video_id}"

    def _meta():
        opts = {"quiet": True, "skip_download": True, "ignoreerrors": True, **common_ydl_opts()}
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}

    info = with_retries(_meta, retries=retries, base_delay=retry_base)
    return {
        "title": info.get("title") or "",
        "published_at": info.get("upload_date") or "",  # YYYYMMDD
        "url": url,
    }

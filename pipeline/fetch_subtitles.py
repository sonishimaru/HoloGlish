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


def _meta_from_info(info: Optional[Dict[str, Any]], video_id: str) -> Dict[str, Any]:
    """extract_info の結果から軽量メタ（title / 投稿日 / url）を組み立てる。

    純粋関数。プローブで得た info を使い回すことで、メタ取得のための
    追加の extract_info を省く（収集の高速化）。
    """
    info = info or {}
    return {
        "title": info.get("title") or "",
        "published_at": info.get("upload_date") or "",  # YYYYMMDD
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def fetch_subtitle(
    video_id: str,
    out_dir: str,
    lang_order: Optional[List[str]] = None,
    retries: int = 3,
    retry_base: float = 2.0,
) -> Optional[Tuple[str, str, str, Dict[str, Any]]]:
    """字幕を取得して保存する。

    戻り値: (保存ファイルパス, lang, sub_kind, meta) / 取得できなければ None
    sub_kind は 'manual' または 'auto'。meta はプローブ結果から得た
    title / published_at / url（追加の抽出をしないための使い回し）。

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

    # プローブの info からメタを確定（別途 fetch_video_meta を呼ばない）
    meta = _meta_from_info(info, video_id)

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
    return candidates[0], lang.split("-")[0], sub_kind, meta


# youtube-transcript-api で「字幕が無い/取得対象外」とみなす例外クラス名。
# これらは None（no_subs 相当）で返す。RequestBlocked / IpBlocked / HTTPError 等の
# 一過性ブロックは含めず、例外を送出して呼び出し側で 'error'（次回再取得）にする。
_NO_TRANSCRIPT = {
    "TranscriptsDisabled", "NoTranscriptFound", "NoTranscriptAvailable",
    "VideoUnavailable", "VideoUnplayable", "InvalidVideoId",
    "TranslationLanguageNotAvailable", "NotTranslatable", "AgeRestricted",
}


def _transcript_lister():
    """youtube-transcript-api の list 呼び出しを返す（1.x=インスタンス / 0.6=クラス）。"""
    from youtube_transcript_api import YouTubeTranscriptApi
    try:
        inst = YouTubeTranscriptApi()          # 1.x はインスタンス API
        if hasattr(inst, "list"):
            return inst.list
    except Exception:  # noqa: BLE001
        pass
    return YouTubeTranscriptApi.list_transcripts  # 0.6 系


def fetch_transcript_api(
    video_id: str, lang_order: Optional[List[str]] = None,
) -> Optional[Tuple[List[Dict[str, Any]], str, str]]:
    """youtube-transcript-api でトランスクリプトを取得する（yt-dlp とは別経路）。

    yt-dlp のプレイヤー取得が bot 判定で弾かれる状況でも、timedtext ベースの
    この経路なら通ることがある（データセンターIP対策の一手）。

    戻り値: (segments, lang, sub_kind) / 字幕が無ければ None。
      segments は [{"start","dur","text"}]。sub_kind は 'manual'/'auto'。
    ブロック等の一過性エラーは例外を送出し、呼び出し側で 'error' として記録する。
    """
    lang_order = lang_order or DEFAULT_LANG_ORDER
    try:
        lister = _transcript_lister()
    except ImportError:
        return None

    try:
        tlist = lister(video_id)
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ in _NO_TRANSCRIPT:
            return None
        raise  # ブロック等 → 呼び出し側で error（次回再取得）

    def _pick(generated: bool):
        for want in lang_order:
            for t in tlist:
                code = getattr(t, "language_code", "")
                if bool(getattr(t, "is_generated", False)) == generated and (
                    code == want or code.split("-")[0] == want
                ):
                    return t
        return None

    tr = _pick(False) or _pick(True)  # 手動字幕を優先、無ければ自動生成
    if tr is None:
        return None

    try:
        rows = tr.fetch()
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ in _NO_TRANSCRIPT:
            return None
        raise

    segments: List[Dict[str, Any]] = []
    for r in rows:
        # 0.6系は dict、1.x系はオブジェクト属性の両対応
        text = r.get("text") if isinstance(r, dict) else getattr(r, "text", "")
        start = r.get("start") if isinstance(r, dict) else getattr(r, "start", 0.0)
        dur = r.get("duration") if isinstance(r, dict) else getattr(r, "duration", 0.0)
        if text:
            segments.append({"start": float(start or 0.0), "dur": float(dur or 0.0), "text": text})
    if not segments:
        return None
    kind = "auto" if getattr(tr, "is_generated", False) else "manual"
    return segments, getattr(tr, "language_code", lang_order[0]).split("-")[0], kind


def fetch_video_meta(video_id: str, retries: int = 3, retry_base: float = 2.0) -> Dict[str, Any]:
    """タイトル・投稿日など軽量メタを取得（スタンドアロン用）。

    通常の収集では fetch_subtitle がプローブ結果からメタを返すため呼ばれない。
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    def _meta():
        opts = {"quiet": True, "skip_download": True, "ignoreerrors": True, **common_ydl_opts()}
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}

    info = with_retries(_meta, retries=retries, base_delay=retry_base)
    return _meta_from_info(info, video_id)

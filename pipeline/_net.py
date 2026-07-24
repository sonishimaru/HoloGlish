"""ネットワーク処理の共通ヘルパ（一過性エラーのリトライ）。

YouTube 収集ではレート制限（HTTP 429 / "Too Many Requests" / "Sign in to
confirm you're not a bot"）が散発する。これらは時間を置けば回復する一過性
エラーなので、指数バックオフで数回リトライする。恒久的な失敗（動画が非公開・
削除済みなど）はリトライせずそのまま送出する。

``sleep`` を差し替え可能にしているのは、テストで実待機せず検証するため。
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, TypeVar

T = TypeVar("T")

# 自動収集（CI/クラウド）では YouTube の bot 判定が出やすい。
# ブラウザから書き出した cookies ファイルのパスを環境変数で渡せると緩和できる。
COOKIES_ENV = "HOLOGLISH_COOKIES"

# yt-dlp の innertube クライアント。既定の web クライアントはデータセンターIPで
# 「Sign in to confirm you're not a bot」を出しやすい。別クライアントに切り替えると
# 回避できる場合があるため、環境変数で調整可能にする（カンマ区切り）。
# 空文字/"default" を渡すと yt-dlp 既定に任せる。
PLAYER_CLIENTS_ENV = "HOLOGLISH_PLAYER_CLIENTS"
_DEFAULT_PLAYER_CLIENTS = ["tv", "mweb", "web_safari"]


def _player_clients() -> list | None:
    raw = os.environ.get(PLAYER_CLIENTS_ENV)
    if raw is None:
        return list(_DEFAULT_PLAYER_CLIENTS)
    raw = raw.strip()
    if not raw or raw.lower() == "default":
        return None  # yt-dlp 既定のクライアント選択に任せる
    return [c.strip() for c in raw.split(",") if c.strip()]


def common_ydl_opts() -> Dict[str, Any]:
    """全 yt-dlp 呼び出しに共通で足すオプション。

    - ``HOLOGLISH_COOKIES`` にファイルパスが設定され、かつ実在すれば
      ``cookiefile`` として渡す（bot 判定・年齢制限の緩和）。
    - ``HOLOGLISH_PLAYER_CLIENTS`` で innertube クライアントを切替（bot 判定回避）。
    - 字幕しか使わないので、動画フォーマット(DASH/HLS)の manifest 取得と
      翻訳字幕の列挙をスキップし、抽出(extract_info)を大幅に軽量化する。
      （原語の手動/自動字幕は影響を受けない。収集を速くする狙い。）
    """
    youtube_args: Dict[str, Any] = {"skip": ["hls", "dash", "translated_subs"]}
    clients = _player_clients()
    if clients:
        youtube_args["player_client"] = clients
    opts: Dict[str, Any] = {
        "extractor_args": {"youtube": youtube_args},
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
    }
    cookies = os.environ.get(COOKIES_ENV)
    if cookies and os.path.isfile(cookies):
        opts["cookiefile"] = cookies
    return opts

# メッセージに含まれていれば一過性（＝リトライ価値あり）と判断するマーカー
_TRANSIENT_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "rate-limit",
    "sign in to confirm",
    "temporarily unavailable",
    "timed out",
    "connection reset",
)


def is_transient(exc: BaseException) -> bool:
    """例外メッセージからレート制限等の一過性エラーかを判定する。"""
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def with_retries(
    fn: Callable[[], T],
    *,
    retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    transient: Callable[[BaseException], bool] = is_transient,
) -> T:
    """``fn`` を実行し、一過性エラーなら指数バックオフでリトライする。

    - リトライ間隔: base_delay * 2**attempt（max_delay で頭打ち）。
    - 一過性でない例外、または retries を使い切った場合は最後の例外を送出。
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if attempt >= retries or not transient(exc):
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            sleep(delay)
            attempt += 1

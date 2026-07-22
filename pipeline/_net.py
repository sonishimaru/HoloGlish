"""ネットワーク処理の共通ヘルパ（一過性エラーのリトライ）。

YouTube 収集ではレート制限（HTTP 429 / "Too Many Requests" / "Sign in to
confirm you're not a bot"）が散発する。これらは時間を置けば回復する一過性
エラーなので、指数バックオフで数回リトライする。恒久的な失敗（動画が非公開・
削除済みなど）はリトライせずそのまま送出する。

``sleep`` を差し替え可能にしているのは、テストで実待機せず検証するため。
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")

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

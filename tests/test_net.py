"""一過性エラーのリトライヘルパのテスト（ネットワーク不要・実待機なし）。"""

import pytest

from pipeline import _net


def test_common_ydl_opts_empty_without_env(monkeypatch):
    monkeypatch.delenv(_net.COOKIES_ENV, raising=False)
    assert _net.common_ydl_opts() == {}


def test_common_ydl_opts_ignores_missing_file(monkeypatch):
    monkeypatch.setenv(_net.COOKIES_ENV, "/no/such/cookies.txt")
    assert "cookiefile" not in _net.common_ydl_opts()


def test_common_ydl_opts_uses_existing_file(monkeypatch, tmp_path):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv(_net.COOKIES_ENV, str(cookie))
    assert _net.common_ydl_opts()["cookiefile"] == str(cookie)


def test_is_transient_detects_rate_limit():
    assert _net.is_transient(Exception("HTTP Error 429: Too Many Requests"))
    assert _net.is_transient(Exception("Sign in to confirm you're not a bot"))
    assert _net.is_transient(Exception("The read operation timed out"))


def test_is_transient_ignores_permanent():
    assert not _net.is_transient(Exception("Video unavailable"))
    assert not _net.is_transient(Exception("Private video"))


def test_with_retries_succeeds_first_try():
    calls = []
    out = _net.with_retries(lambda: calls.append(1) or "ok", sleep=lambda _: None)
    assert out == "ok"
    assert len(calls) == 1


def test_with_retries_recovers_after_transient():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Exception("HTTP Error 429: Too Many Requests")
        return "recovered"

    slept = []
    out = _net.with_retries(flaky, retries=3, base_delay=2.0, sleep=slept.append)
    assert out == "recovered"
    assert attempts["n"] == 3
    # 指数バックオフ: 2, 4 秒
    assert slept == [2.0, 4.0]


def test_with_retries_gives_up_after_exhausting():
    def always_429():
        raise Exception("429 Too Many Requests")

    with pytest.raises(Exception, match="429"):
        _net.with_retries(always_429, retries=2, sleep=lambda _: None)


def test_with_retries_does_not_retry_permanent():
    attempts = {"n": 0}

    def permanent():
        attempts["n"] += 1
        raise Exception("Video unavailable")

    with pytest.raises(Exception, match="unavailable"):
        _net.with_retries(permanent, retries=5, sleep=lambda _: None)
    assert attempts["n"] == 1  # リトライしていない


def test_with_retries_caps_delay():
    def always_429():
        raise Exception("429")

    slept = []
    with pytest.raises(Exception):
        _net.with_retries(always_429, retries=5, base_delay=10.0, max_delay=15.0, sleep=slept.append)
    # 10, 15(cap), 15, 15, 15
    assert slept == [10.0, 15.0, 15.0, 15.0, 15.0]

"""有償の字幕取得サービス経路（Supadata）のテスト（オフライン・HTTPはモック）。"""

import argparse
import urllib.error

import pytest

from pipeline import fetch_subtitles as fs
from pipeline import run


def _ok_body():
    return {
        "content": [
            {"text": "おはよう", "offset": 1500, "duration": 2000, "lang": "ja"},
            {"text": "こんにちは", "offset": 4000, "duration": 1000, "lang": "ja"},
            {"text": "  ", "offset": 6000, "duration": 500, "lang": "ja"},  # 空白は捨てる
        ],
        "lang": "ja",
        "availableLangs": ["ja", "en"],
    }


def test_service_parses_ms_to_seconds(monkeypatch):
    monkeypatch.setenv(fs.SERVICE_KEY_ENV, "k")
    monkeypatch.setattr(fs, "_service_get", lambda url, key, timeout=60.0: (200, _ok_body()))
    segs, lang, kind = fs.fetch_transcript_service("vid", ["ja", "en", "id"])
    assert lang == "ja" and kind == "auto"
    assert segs == [
        {"start": 1.5, "dur": 2.0, "text": "おはよう"},
        {"start": 4.0, "dur": 1.0, "text": "こんにちは"},
    ]


def test_service_requests_first_lang(monkeypatch):
    seen = {}
    def _get(url, key, timeout=60.0):
        seen["url"] = url; seen["key"] = key
        return 200, _ok_body()
    monkeypatch.setenv(fs.SERVICE_KEY_ENV, "sekret")
    monkeypatch.setattr(fs, "_service_get", _get)
    fs.fetch_transcript_service("abc123", ["en", "ja"])
    assert "videoId=abc123" in seen["url"]
    assert "lang=en" in seen["url"]
    assert "mode=native" in seen["url"]   # AI生成(高額)は使わない
    assert seen["key"] == "sekret"


def test_service_206_means_no_subs(monkeypatch):
    monkeypatch.setenv(fs.SERVICE_KEY_ENV, "k")
    monkeypatch.setattr(fs, "_service_get", lambda *a, **kw: (206, {}))
    assert fs.fetch_transcript_service("vid") is None


def test_service_http_error_raises(monkeypatch):
    monkeypatch.setenv(fs.SERVICE_KEY_ENV, "k")
    def _get(url, key, timeout=60.0):
        raise urllib.error.HTTPError(url, 429, "limit-exceeded", None, None)
    monkeypatch.setattr(fs, "_service_get", _get)
    with pytest.raises(RuntimeError, match="429"):
        fs.fetch_transcript_service("vid")


def test_service_403_forbidden_means_no_subs(monkeypatch):
    # メンバー限定動画の 403 は恒久的に取得不能 → no_subs（確定スキップ）。
    # error にするとメン限が連続するチャンネルで連続失敗ブレーカーが誤作動する。
    import io
    monkeypatch.setenv(fs.SERVICE_KEY_ENV, "k")
    body = (b'{"error":"forbidden","message":"Forbidden",'
            b'"details":"This video requires channel membership to access."}')
    def _get(url, key, timeout=60.0):
        raise urllib.error.HTTPError(url, 403, "forbidden", None, io.BytesIO(body))
    monkeypatch.setattr(fs, "_service_get", _get)
    assert fs.fetch_transcript_service("vid") is None


def test_service_403_without_forbidden_body_raises(monkeypatch):
    # 403 でも本文が読めない/想定外なら従来どおり error（安全側）。
    monkeypatch.setenv(fs.SERVICE_KEY_ENV, "k")
    def _get(url, key, timeout=60.0):
        raise urllib.error.HTTPError(url, 403, "forbidden", None, None)
    monkeypatch.setattr(fs, "_service_get", _get)
    with pytest.raises(RuntimeError, match="403"):
        fs.fetch_transcript_service("vid")


def test_service_missing_key_raises(monkeypatch):
    monkeypatch.delenv(fs.SERVICE_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError, match="SUPADATA_API_KEY"):
        fs.fetch_transcript_service("vid")


def _args(db_path, **over):
    base = dict(
        db=db_path, branch=None, members="Sakura Miko", limit=5, list_depth=0,
        date_after=None, raw_dir="data/raw", sleep=0.0, retries=1, retry_base=0.0,
        time_budget=0.0, force=False, tabs="videos", subs_source="service",
        error_streak=30,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_collect_service_only(tmp_path, monkeypatch):
    """subs_source=service は yt-dlp / transcript-api を一切呼ばない。"""
    import pipeline.fetch_videos as fv
    from pipeline import db

    monkeypatch.setenv(fs.SERVICE_KEY_ENV, "k")
    monkeypatch.setattr(fv, "list_channel_videos",
                        lambda *a, **k: [{"video_id": "s1", "title": "t", "url": "u"}])

    def _forbidden(*a, **k):
        raise AssertionError("service モードで他経路が呼ばれた")
    monkeypatch.setattr(fs, "fetch_subtitle", _forbidden)
    monkeypatch.setattr(fs, "fetch_transcript_api", _forbidden)
    monkeypatch.setattr(fs, "fetch_transcript_service",
                        lambda *a, **k: ([{"start": 0.0, "dur": 1.0, "text": "hi"}], "ja", "auto"))

    dbp = str(tmp_path / "svc.db")
    rc = run.cmd_collect(_args(dbp))
    assert rc == 0
    conn = db.connect(dbp)
    assert conn.execute("SELECT status FROM processed WHERE video_id='s1'").fetchone()["status"] == "done"
    seg = conn.execute("SELECT text FROM segments WHERE video_id='s1'").fetchone()
    conn.close()
    assert seg["text"] == "hi"


def test_collect_service_requires_key(tmp_path, monkeypatch):
    monkeypatch.delenv(fs.SERVICE_KEY_ENV, raising=False)
    rc = run.cmd_collect(_args(str(tmp_path / "nokey.db")))
    assert rc == 1  # キー未設定はエラー終了（no_subs 誤記録を防ぐ）

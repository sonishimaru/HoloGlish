"""player_client 切替 と youtube-transcript-api フォールバックのテスト（オフライン）。"""

import argparse

from pipeline import _net, fetch_subtitles, run


# ---- ① player_client（_net） ----

def test_player_clients_default(monkeypatch):
    monkeypatch.delenv(_net.PLAYER_CLIENTS_ENV, raising=False)
    yt = _net.common_ydl_opts()["extractor_args"]["youtube"]
    assert yt["player_client"] == ["tv", "mweb", "web_safari"]


def test_player_clients_disabled(monkeypatch):
    monkeypatch.setenv(_net.PLAYER_CLIENTS_ENV, "default")
    yt = _net.common_ydl_opts()["extractor_args"]["youtube"]
    assert "player_client" not in yt


def test_player_clients_custom(monkeypatch):
    monkeypatch.setenv(_net.PLAYER_CLIENTS_ENV, "android, web ")
    yt = _net.common_ydl_opts()["extractor_args"]["youtube"]
    assert yt["player_client"] == ["android", "web"]


# ---- ② youtube-transcript-api フォールバック ----

class _FakeTranscript:
    def __init__(self, code, generated, rows):
        self.language_code = code
        self.is_generated = generated
        self._rows = rows

    def fetch(self):
        return self._rows


def _fake_lister(transcripts):
    def _list(video_id):
        return list(transcripts)
    return _list


def test_fetch_transcript_prefers_manual(monkeypatch):
    ts = [
        _FakeTranscript("ja", True, [{"text": "auto", "start": 0.0, "duration": 1.0}]),
        _FakeTranscript("ja", False, [{"text": "おはよう", "start": 1.5, "duration": 2.0}]),
    ]
    monkeypatch.setattr(fetch_subtitles, "_transcript_lister", lambda: _fake_lister(ts))
    segs, lang, kind = fetch_subtitles.fetch_transcript_api("v", ["ja", "en", "id"])
    assert kind == "manual" and lang == "ja"
    assert segs == [{"start": 1.5, "dur": 2.0, "text": "おはよう"}]


def test_fetch_transcript_lang_order_and_objects(monkeypatch):
    class Row:  # 1.x はオブジェクト属性
        def __init__(s, t, st, d): s.text, s.start, s.duration = t, st, d
    ts = [
        _FakeTranscript("en", True, [Row("hello", 0.0, 1.0)]),
        _FakeTranscript("id", True, [Row("halo", 0.0, 1.0)]),
    ]
    monkeypatch.setattr(fetch_subtitles, "_transcript_lister", lambda: _fake_lister(ts))
    segs, lang, kind = fetch_subtitles.fetch_transcript_api("v", ["ja", "en", "id"])
    assert lang == "en" and kind == "auto"
    assert segs == [{"start": 0.0, "dur": 1.0, "text": "hello"}]


def test_fetch_transcript_none_when_absent(monkeypatch):
    monkeypatch.setattr(fetch_subtitles, "_transcript_lister", lambda: _fake_lister([]))
    assert fetch_subtitles.fetch_transcript_api("v", ["ja"]) is None


def test_fetch_transcript_block_raises(monkeypatch):
    class RequestBlocked(Exception):
        pass

    def _boom(vid):
        raise RequestBlocked("The request was blocked")

    monkeypatch.setattr(fetch_subtitles, "_transcript_lister", lambda: _boom)
    try:
        fetch_subtitles.fetch_transcript_api("v", ["ja"])
        assert False, "should raise on block"
    except RequestBlocked:
        pass


# ---- cmd_collect のフォールバック統合 ----

def _args(db_path, **over):
    base = dict(
        db=db_path, branch=None, members="Sakura Miko", limit=5, list_depth=0,
        date_after=None, raw_dir="data/raw", sleep=0.0, retries=1, retry_base=0.0,
        time_budget=0.0, force=False, tabs="videos", subs_source="both",
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_collect_falls_back_to_api_when_ytdlp_blocked(tmp_path, monkeypatch):
    """yt-dlp が bot 判定で失敗しても、api 経路が通れば done になる。"""
    import pipeline.fetch_videos as fv
    import pipeline.fetch_subtitles as fs
    from pipeline import db

    monkeypatch.setattr(fv, "list_channel_videos",
                        lambda *a, **k: [{"video_id": "v1", "title": "t", "url": "u"}])

    def _blocked(*a, **k):
        raise Exception("Sign in to confirm you're not a bot")
    monkeypatch.setattr(fs, "fetch_subtitle", _blocked)
    monkeypatch.setattr(fs, "fetch_transcript_api",
                        lambda *a, **k: ([{"start": 0.0, "dur": 1.0, "text": "おはよう"}], "ja", "auto"))

    dbp = str(tmp_path / "fb.db")
    rc = run.cmd_collect(_args(dbp))
    assert rc == 0
    conn = db.connect(dbp)
    row = conn.execute("SELECT status FROM processed WHERE video_id='v1'").fetchone()
    seg = conn.execute("SELECT text FROM segments WHERE video_id='v1'").fetchone()
    conn.close()
    assert row["status"] == "done"
    assert seg["text"] == "おはよう"


def test_collect_api_only_skips_ytdlp(tmp_path, monkeypatch):
    import pipeline.fetch_videos as fv
    import pipeline.fetch_subtitles as fs
    from pipeline import db

    monkeypatch.setattr(fv, "list_channel_videos",
                        lambda *a, **k: [{"video_id": "v2", "title": "t", "url": "u"}])

    def _should_not_call(*a, **k):
        raise AssertionError("ytdlp must not be called when subs_source=api")
    monkeypatch.setattr(fs, "fetch_subtitle", _should_not_call)
    monkeypatch.setattr(fs, "fetch_transcript_api",
                        lambda *a, **k: ([{"start": 0.0, "dur": 1.0, "text": "hi"}], "en", "auto"))

    dbp = str(tmp_path / "api.db")
    rc = run.cmd_collect(_args(dbp, subs_source="api"))
    assert rc == 0
    conn = db.connect(dbp)
    assert conn.execute("SELECT status FROM processed WHERE video_id='v2'").fetchone()["status"] == "done"
    conn.close()

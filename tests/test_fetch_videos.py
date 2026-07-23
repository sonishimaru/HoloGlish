"""チャンネルタブ列挙とマージのテスト（ネットワーク不要）。

「動画」タブに加え「ライブ」タブ（配信アーカイブ）も列挙対象にする挙動を検証する。
"""

from pipeline import fetch_videos as fv


def _v(vid, title=""):
    return {"video_id": vid, "title": title, "url": f"https://youtu.be/{vid}"}


def test_default_tabs_include_streams():
    assert "videos" in fv.DEFAULT_TABS
    assert "streams" in fv.DEFAULT_TABS


def test_merge_interleaves_round_robin():
    videos = [_v("a1"), _v("a2"), _v("a3")]
    streams = [_v("b1"), _v("b2")]
    merged = fv._merge_tab_videos([videos, streams])
    # 各タブの新しい順を保ちつつラウンドロビン: a1, b1, a2, b2, a3
    assert [m["video_id"] for m in merged] == ["a1", "b1", "a2", "b2", "a3"]


def test_merge_dedupes_by_video_id():
    videos = [_v("x"), _v("y")]
    streams = [_v("x"), _v("z")]  # x は両タブに存在
    merged = fv._merge_tab_videos([videos, streams])
    ids = [m["video_id"] for m in merged]
    assert ids.count("x") == 1
    assert set(ids) == {"x", "y", "z"}


def test_merge_applies_limit_to_total():
    videos = [_v(f"a{i}") for i in range(10)]
    streams = [_v(f"b{i}") for i in range(10)]
    merged = fv._merge_tab_videos([videos, streams], limit=5)
    assert len(merged) == 5
    # 先頭は両タブが交互に現れる
    assert merged[0]["video_id"] == "a0"
    assert merged[1]["video_id"] == "b0"


def test_merge_handles_empty_tab():
    videos = [_v("a1"), _v("a2")]
    merged = fv._merge_tab_videos([videos, []])
    assert [m["video_id"] for m in merged] == ["a1", "a2"]


def test_list_channel_videos_queries_both_tabs(monkeypatch):
    calls = []

    def fake_tab(channel_id, tab, limit=None, date_after=None, retries=3, retry_base=2.0):
        calls.append(tab)
        return {"videos": [_v("vid1"), _v("vid2")],
                "streams": [_v("live1")]}[tab]

    monkeypatch.setattr(fv, "_list_channel_tab", fake_tab)
    out = fv.list_channel_videos("UCxxxx")
    assert calls == ["videos", "streams"]  # 両タブを列挙
    ids = {m["video_id"] for m in out}
    assert ids == {"vid1", "vid2", "live1"}


def test_list_channel_videos_custom_tabs(monkeypatch):
    monkeypatch.setattr(
        fv, "_list_channel_tab",
        lambda channel_id, tab, **kw: [_v(f"{tab}-1")],
    )
    out = fv.list_channel_videos("UCxxxx", tabs=("streams",))
    assert [m["video_id"] for m in out] == ["streams-1"]

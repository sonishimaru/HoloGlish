"""字幕パーサ（json3 / vtt）のテスト。"""

from pipeline.parse_subs import parse_json3, parse_vtt


def test_parse_json3_basic():
    raw = (
        '{"events":['
        '{"tStartMs":1500,"dDurationMs":2500,"segs":[{"utf8":"おはよう"},{"utf8":"ございます"}]}'
        "]}"
    )
    segs = parse_json3(raw)
    assert len(segs) == 1
    assert segs[0]["text"] == "おはようございます"
    assert segs[0]["start"] == 1.5
    assert segs[0]["dur"] == 2.5


def test_parse_json3_empty_events():
    assert parse_json3('{"events":[]}') == []


def test_parse_json3_skips_blank_segments():
    raw = '{"events":[{"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"\\n"}]}]}'
    assert parse_json3(raw) == []


def test_parse_vtt_strips_tags_and_noise():
    vtt = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:03.000\n"
        "おはよう<00:00:02.000><c> みんな</c>\n\n"
        "00:00:04.500 --> 00:00:06.000\n"
        "今日も[音楽]よろしく\n"
    )
    segs = parse_vtt(vtt)
    assert segs[0]["text"] == "おはよう みんな"
    assert segs[0]["start"] == 1.0
    assert segs[0]["dur"] == 2.0
    # [音楽] のような効果音注釈は除去される
    assert segs[1]["text"] == "今日もよろしく"


def test_merge_dedups_progressive_captions():
    # 自動字幕にありがちな「前行を含む次行」を圧縮する
    raw = (
        '{"events":['
        '{"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"hello"}]},'
        '{"tStartMs":1000,"dDurationMs":1000,"segs":[{"utf8":"hello world"}]}'
        "]}"
    )
    segs = parse_json3(raw)
    assert len(segs) == 1
    assert segs[0]["text"] == "hello world"

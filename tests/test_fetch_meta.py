"""メタ組み立て（プローブ結果の使い回し）のテスト。ネットワーク不要。"""

from pipeline import fetch_subtitles as fs


def test_meta_from_info_extracts_fields():
    info = {"title": "テスト配信", "upload_date": "20240115", "extra": "ignored"}
    meta = fs._meta_from_info(info, "abc123")
    assert meta["title"] == "テスト配信"
    assert meta["published_at"] == "20240115"
    assert meta["url"] == "https://www.youtube.com/watch?v=abc123"


def test_meta_from_info_handles_missing():
    meta = fs._meta_from_info(None, "xyz")
    assert meta["title"] == ""
    assert meta["published_at"] == ""
    assert meta["url"].endswith("v=xyz")


def test_meta_from_info_missing_keys():
    meta = fs._meta_from_info({"title": "のみ"}, "v1")
    assert meta["title"] == "のみ"
    assert meta["published_at"] == ""

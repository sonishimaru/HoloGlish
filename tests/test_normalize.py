"""検索用正規化のテスト。"""

from pipeline.normalize import normalize, terms


def test_removes_whitespace():
    assert normalize("あり がとう") == "ありがとう"
    assert normalize("  hello   world ") == "helloworld"


def test_nfkc_width():
    assert normalize("ＨＥＬＬＯ") == "hello"
    assert normalize("１２３") == "123"


def test_katakana_to_hiragana():
    assert normalize("ペコラ") == "ぺこら"
    assert normalize("ミコ") == "みこ"
    # 半角カナも NFKC で全角化 → ひらがな化
    assert normalize("ﾐｺ") == "みこ"


def test_lowercase():
    assert normalize("Hello") == "hello"


def test_terms_splits_then_normalizes():
    assert terms("みこ 歌") == ["みこ", "歌"]
    assert terms("  ペコラ  Hello ") == ["ぺこら", "hello"]
    assert terms("") == []
    assert terms("   ") == []

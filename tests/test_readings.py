"""読み付け（表記ゆれの束ね）のテスト。"""

import pytest

from pipeline import readings

pytestmark = pytest.mark.skipif(not readings.available(), reason="読み付けの依存が無い")


@pytest.mark.parametrize("word, reading", [
    ("楽しい", "たのしい"),
    ("元気", "げんき"),
    ("配信", "はいしん"),
    ("気持ちいい", "きもちいい"),
    ("お疲れ", "おつかれ"),
    ("大丈夫", "だいじょうぶ"),
    ("ペコラ先輩", "ぺこらせんぱい"),
])
def test_reads_common_words(word, reading):
    assert reading in readings.readings(word)


def test_uses_both_readers_so_either_reading_reaches():
    # MeCab+UniDic は送り仮名に強い（pykakasi は「かわい」と読み違える）
    assert "かわいい" in readings.readings("可愛い")
    # pykakasi は代表的な読みに強い（UniDic は「あす」に決め打ちする）
    assert "あした" in readings.readings("明日")


@pytest.mark.parametrize("word", ["おはよう", "コラボ", "hello", "ぺこ", ""])
def test_kana_and_latin_need_no_reading(word):
    # 仮名だけの語は正規化（カナ→かな）で既に束ねられているので読みは不要
    assert readings.readings(word) == set()


def test_unreadable_leftovers_are_dropped():
    # 読み付けで数字が残るもの等、ひらがなだけにならない読みは採らない
    assert all(set(r) <= set("ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとどなにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわゐゑをんゔゕゖー")
               for r in readings.readings("2024年の配信"))


def test_reading_list_is_sorted_or_none():
    assert readings.reading_list("明日") == sorted(readings.readings("明日"))
    assert readings.reading_list("おはよう") is None


@pytest.mark.parametrize("word, base", [
    ("楽しかった", "楽しい"),
    ("頑張って", "頑張る"),
    ("食べたい", "食べる"),
    ("面白くない", "面白い"),
    ("楽しく", "楽しい"),
    ("かわいかった", "かわいい"),  # 書かれた文字種のまま（lemma の「可愛い」ではない）
])
def test_base_form_of_inflected_words(word, base):
    assert readings.base_form(word) == base


@pytest.mark.parametrize("word", [
    "楽しいね",   # 原形を含む → 部分一致で既に当たるので束ねる必要が無い
    "楽しみ",     # 名詞（別の語）
    "楽しそう",   # 様態の「そう」が続く → 別の言い回し
    "楽しい曲",   # 名詞が続く
    # 文の途中で切れた断片（束ねると「食べ」が「食べ物」まで当てる語幹検索になる）
    "楽し", "食べ", "行っ", "行ってみ", "食べられ",
    "おはよう",
    "",
])
def test_base_form_is_none_for_non_inflections(word):
    assert readings.base_form(word) is None

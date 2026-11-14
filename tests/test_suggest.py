"""入力補完の候補語彙づくりのテスト。"""

from pipeline import suggest


def _words(items):
    return [it["t"] for it in items]


CORPUS = [
    "みなさんおはようございます、今日も配信始めていきます",
    "おはようございます！ 今日はゲームやるぺこ",
    "おはようございます。よろしくお願いします",
    "ありがとうございます、ありがとうございます",
    "ありがとうございます！",
    "hello everyone, thank you so much",
    "hello hello thank you",
    "hello guys thank you very much",
    "コラボ配信ありがとうございます",
    "今日はコラボ配信です、コラボ楽しみ",
]


def test_chunks_split_on_punctuation_and_keep_letters():
    assert suggest.chunks("みなさん、おはよう！ hello") == ["みなさん", "おはよう", "hello"]


def test_chunks_split_between_latin_and_japanese():
    # 英数字と日本語をまたいだ取り違え（"thanキ"）を候補にしないための分割
    assert suggest.chunks("thanキ 2024年") == ["than", "キ", "2024", "年"]


def test_surface_normalizes_width_and_case_only():
    # NFKC + 小文字化はする。カナ畳み込み・空白除去はしない（そのまま画面に出すため）
    assert suggest.surface("ＨＥＬＬＯ　コラボ") == "hello コラボ"


def test_mines_whole_phrases_not_fragments():
    words = _words(suggest.mine(CORPUS, min_count=3))
    assert "おはようございます" in words
    assert "ありがとうございます" in words
    # 語の途中から切り出しただけの断片は落ちる
    assert "はようございま" not in words
    assert "りがとうございま" not in words


def test_latin_words_are_kept_whole():
    words = _words(suggest.mine(CORPUS, min_count=3))
    assert "hello" in words
    assert "thank" in words
    # 英単語の部分文字列は候補にしない
    assert "hell" not in words
    assert "hankyo" not in words


def test_ngrams_never_cross_a_delimiter():
    # 「ぺこ」と「今日」は句読点をまたぐので繋がった候補は出ない
    texts = ["ぺこ、今日"] * 5
    words = _words(suggest.mine(texts, min_count=3))
    assert all("、" not in w for w in words)
    assert "ぺこ今日" not in words


def test_min_count_filters_rare_phrases():
    texts = ["おはようございます"] * 5 + ["めったに言わない台詞"]
    words = _words(suggest.mine(texts, min_count=3))
    assert "おはようございます" in words
    assert all("めったに" not in w for w in words)


def test_useless_candidates_are_dropped():
    texts = ["2024 2024 2024", "ーーーー", "ああああ"] * 5
    words = _words(suggest.mine(texts, min_count=3))
    assert all(not w.isdigit() for w in words), words
    assert all(len(set(w)) > 1 for w in words), words


def test_ranked_by_frequency_descending():
    items = suggest.mine(CORPUS, min_count=3)
    counts = [it["n"] for it in items]
    assert counts == sorted(counts, reverse=True)


def test_limit_caps_the_vocabulary():
    assert len(suggest.mine(CORPUS, min_count=3, limit=3)) == 3


def test_build_suggestions_samples_large_corpora():
    texts = CORPUS * 200  # 2000 発話
    built = suggest.build_suggestions(texts, sample_max=100)
    assert built["version"] == 3  # 2: 読み r、3: 活用形の原形 b
    assert built["sampled"] <= 100
    assert built["items"], "標本からでも候補は取れる"


def test_build_suggestions_keeps_small_corpora_whole():
    built = suggest.build_suggestions(CORPUS)
    assert built["sampled"] == len(CORPUS)
    assert built["min_count"] == 3  # 小さなコーパスでも候補が出る足切り
    assert "おはようございます" in _words(built["items"])


def test_kanji_items_carry_readings():
    """漢字を含む候補には読みが付き、仮名だけの候補には付かない（展開の土台）。"""
    built = suggest.build_suggestions(CORPUS)
    by_t = {it["t"]: it for it in built["items"]}
    assert by_t["配信"]["r"] == ["はいしん"]
    assert by_t["今日"]["r"]  # きょう（と辞書によっては こんにち）
    assert "r" not in by_t["おはようございます"]
    assert "r" not in by_t["コラボ"]  # カタカナは正規化でひらがなに畳まれるので不要


def test_inflected_items_carry_their_base_form():
    """活用形には原形が付く（「活用形も探す」の土台）。原形を含む形には付けない。"""
    import pytest

    from pipeline import readings

    if not readings.available():
        pytest.skip("読み付けの依存が無い")
    items = [{"t": "楽しかった", "n": 5}, {"t": "頑張って", "n": 5},
             {"t": "楽しいね", "n": 5}, {"t": "楽しみ", "n": 5}]
    counts = suggest.attach_annotations(items)
    by_t = {it["t"]: it for it in items}
    assert by_t["楽しかった"]["b"] == "楽しい"
    assert by_t["頑張って"]["b"] == "頑張る"
    assert "b" not in by_t["楽しいね"]   # 「楽しい」を含むので部分一致で既に当たる
    assert "b" not in by_t["楽しみ"]     # 名詞（別の語）
    assert counts["b"] == 2

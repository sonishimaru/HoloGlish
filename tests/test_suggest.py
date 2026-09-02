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
    assert built["version"] == 1
    assert built["sampled"] <= 100
    assert built["items"], "標本からでも候補は取れる"


def test_build_suggestions_keeps_small_corpora_whole():
    built = suggest.build_suggestions(CORPUS)
    assert built["sampled"] == len(CORPUS)
    assert built["min_count"] == 3  # 小さなコーパスでも候補が出る足切り
    assert "おはようございます" in _words(built["items"])

"""語の「読み」（ひらがな）を求める。表記ゆれ（漢字⇔かな）を束ねるために使う。

YouTube の日本語自動字幕は「可愛い」「楽しい」「お疲れ」のように漢字で書くことが
多く、ひらがなで打つと大半を取りこぼす（実データ: たのしい 72本 / 楽しい 26,570本）。
そこで補完語彙の各語に読みを振り、検索時に「たのしい」→「たのしい OR 楽しい」と
展開できるようにする。

読み付けは2つの方式を併用し、どちらかの読みを持てば届くようにする:

- **MeCab + UniDic**（fugashi / unidic-lite）: 形態素解析ベースで送り仮名に強い
  （「可愛い」→ かわいい。pykakasi は「かわい」と読み違える）。
- **pykakasi**: 辞書ベース。単語単体の代表的な読みに強い
  （「明日」→ あした。UniDic は「あす」に決め打ちする）。

どちらも妥当な読みなので、和集合を取っても誤展開はほぼ増えない。
同音異義（「いって」→ 言って・行って）は束ねられるが、このツールは
「その音が話されている場面」を探すものなので、むしろ望ましい。

依存（pykakasi / fugashi / unidic-lite）は静的サイトの書き出しにだけ必要で、
収集には不要。入っていない環境では空集合を返し、書き出しは読み無しで続行する。

あわせて、活用した語（楽しかった・頑張って）の原形（楽しい・頑張る）も求める
（:func:`base_form`）。「楽しい」で検索したときに「楽しかった」も探せるようにするため。
"""

from __future__ import annotations

import functools
import logging
from typing import Callable, List, Optional, Set

from .normalize import normalize

log = logging.getLogger(__name__)


def _hiragana_only(s: str) -> bool:
    """正規化済み文字列が、ひらがな（と長音）だけでできているか。"""
    return bool(s) and all("ぁ" <= ch <= "ゖ" or ch == "ー" for ch in s)


@functools.lru_cache(maxsize=1)
def _tagger():
    """MeCab + UniDic の解析器（無ければ None）。"""
    try:
        import fugashi

        return fugashi.Tagger()
    except (ImportError, RuntimeError):
        # RuntimeError: fugashi はあるが辞書(unidic-lite)が無い
        return None


@functools.lru_cache(maxsize=1)
def _readers() -> List[Callable[[str], str]]:
    """利用できる読み付け器の一覧。依存が無いものは黙って外す。"""
    readers: List[Callable[[str], str]] = []
    try:
        import pykakasi

        kks = pykakasi.kakasi()
        readers.append(lambda s: "".join(p["hira"] for p in kks.convert(s)))
    except ImportError:
        log.warning("pykakasi が無いため読み付けの一部を省きます")
    tagger = _tagger()
    if tagger is not None:

        def _mecab(s: str) -> str:
            out = []
            for tok in tagger(s):
                kana = getattr(tok.feature, "kana", None)
                # 未知語（kana が無い・"*"）は表層をそのまま使う
                out.append(kana if kana and kana != "*" else tok.surface)
            return "".join(out)

        readers.append(_mecab)
    else:
        log.warning("fugashi/unidic-lite が無いため読み付けの一部を省きます")
    return readers


def available() -> bool:
    """読み付け器が1つでも使えるか。"""
    return bool(_readers())


def readings(text: str) -> Set[str]:
    """``text`` の読み（正規化済みひらがな）の集合。

    表記そのものと同じ読み（もともと仮名の語）や、読み付けに失敗して漢字が
    残ったものは返さない。返り値が空なら「展開する必要が無い/できない語」。
    """
    surface = normalize(text)
    if not surface or _hiragana_only(surface):
        return set()
    out: Set[str] = set()
    for read in _readers():
        try:
            r = normalize(read(text))
        except Exception:  # noqa: BLE001  読み付け器の内部エラーで書き出しを止めない
            continue
        if r != surface and _hiragana_only(r):
            out.add(r)
    return out


def reading_list(text: str) -> Optional[List[str]]:
    """JSON に載せる形（並びを固定したリスト。読みが無ければ None）。"""
    r = readings(text)
    return sorted(r) if r else None


# 活用の「語尾」として認める品詞。これ以外が続くもの（楽しい曲・楽しそう）は
# 別の言い回しとみなし、活用形として束ねない。
def _inflection_tail(feature) -> bool:
    p1, p2 = feature.pos1, feature.pos2
    return (
        p1 == "助動詞"                                   # 楽しかっ|た・食べ|たい・見|てる
        or (p1 == "助詞" and p2 == "接続助詞")             # 頑張っ|て・行け|ば
        or (p1 in ("動詞", "形容詞") and p2 == "非自立可能")  # 面白く|ない・食べ|て|いる
    )


# 発話の終わりになれる活用形。補完語彙には文の途中で切れた断片（楽し・食べ・行っ）も
# 混じるが、それを活用形として束ねると「食べ」が「食べ物」まで当ててしまう（語幹検索に
# なる）ので、末尾がこれらの形のものだけを束ねる。
_FINAL_FORMS = ("終止形", "連体形", "命令形", "意志推量形", "仮定形")


def _ends_cleanly(feature) -> bool:
    form = feature.cForm or "*"
    if form == "*":                                   # 活用しない語（頑張っ|て の「て」）
        return True
    if form.startswith(_FINAL_FORMS):                 # 楽しかっ|た・頑張れ・頑張ろう
        return True
    return feature.pos1 == "形容詞" and form.startswith("連用形")  # 楽しく


def base_form(text: str) -> Optional[str]:
    """活用した語の原形（書かれた文字種のまま）。活用形でなければ None。

    例: 楽しかった → 楽しい、頑張って → 頑張る、かわいかった → かわいい。
    UniDic の orthBase を使う（lemma は「可愛い」のように漢字へ寄せてしまい、
    かなで書かれた語の原形として使えない）。

    原形をそのまま含む形（楽しいね）は部分一致で既に当たるので None を返す。
    束ねる意味があるのは「楽しかった」のように原形を含まない形だけ。
    """
    tagger = _tagger()
    if tagger is None or not text:
        return None
    try:
        toks = list(tagger(text))
    except Exception:  # noqa: BLE001
        return None
    if not toks or toks[0].feature.pos1 not in ("動詞", "形容詞"):
        return None
    base = toks[0].feature.orthBase
    if not base or base == "*":
        return None
    if not all(_inflection_tail(t.feature) for t in toks[1:]):
        return None
    if not _ends_cleanly(toks[-1].feature):
        return None
    nb, nt = normalize(base), normalize(text)
    if len(nb) < 2 or nb in nt:
        return None
    return base


# 語の頭に来ない品詞。補完語彙はコーパスの文字 n-gram から作るので、語の途中や
# 助詞の連なり（って・ちゃ・にね）から始まる断片も混じる。
_FUNCTION_HEADS = ("助詞", "助動詞", "記号", "補助記号", "接尾辞")


def is_fragment(text: str) -> bool:
    """助詞・助動詞などで始まる、語として独り立ちしない断片か。

    ローマ字の「もしかして」に断片を出さないために使う（nine → にね を防ぐ）。
    解析器が無ければ判定できないので False（断片扱いしない）。
    """
    tagger = _tagger()
    if tagger is None or not text or text.isascii():
        return False
    try:
        toks = list(tagger(text))
    except Exception:  # noqa: BLE001
        return False
    return bool(toks) and toks[0].feature.pos1 in _FUNCTION_HEADS

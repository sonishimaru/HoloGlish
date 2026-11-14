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
def _readers() -> List[Callable[[str], str]]:
    """利用できる読み付け器の一覧。依存が無いものは黙って外す。"""
    readers: List[Callable[[str], str]] = []
    try:
        import pykakasi

        kks = pykakasi.kakasi()
        readers.append(lambda s: "".join(p["hira"] for p in kks.convert(s)))
    except ImportError:
        log.warning("pykakasi が無いため読み付けの一部を省きます")
    try:
        import fugashi

        tagger = fugashi.Tagger()

        def _mecab(s: str) -> str:
            out = []
            for tok in tagger(s):
                kana = getattr(tok.feature, "kana", None)
                # 未知語（kana が無い・"*"）は表層をそのまま使う
                out.append(kana if kana and kana != "*" else tok.surface)
            return "".join(out)

        readers.append(_mecab)
    except (ImportError, RuntimeError):
        # RuntimeError: fugashi はあるが辞書(unidic-lite)が無い
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

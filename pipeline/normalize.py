"""検索用テキスト正規化（索引側・クエリ側で共通に使う）。

自動字幕は表記ゆれ（全角/半角、カタカナ/ひらがな）や単語途中の空白
（ASR 由来: 「あり がとう」）が多く、素の部分一致では取りこぼす。
索引もクエリも同じ正規化をかけることで一致率を上げる。

正規化:
  1. NFKC（全角/半角などの互換文字を統一）
  2. 小文字化
  3. 空白（全種）を除去 … 単語途中の空白を吸収して照合
  4. カタカナ→ひらがな畳み込み … 「ペコラ」と「ぺこら」を同一視

この関数は JS 側（web/api.js の normalizeText）と**同一の結果**になるよう
実装している（静的サイトのクライアント検索と一致させるため）。
"""

from __future__ import annotations

import re
import unicodedata
from typing import List

_WS_RE = re.compile(r"\s+")

# カタカナ (U+30A1..U+30F6) → ひらがな (U+3041..U+3096) は -0x60
_KATA_LO, _KATA_HI, _OFFSET = 0x30A1, 0x30F6, 0x60


def _kata_to_hira(s: str) -> str:
    out = []
    for ch in s:
        o = ord(ch)
        if _KATA_LO <= o <= _KATA_HI:
            out.append(chr(o - _OFFSET))
        else:
            out.append(ch)
    return "".join(out)


def normalize(s: str) -> str:
    """検索照合用の正規化テキストを返す。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = _WS_RE.sub("", s)
    s = _kata_to_hira(s)
    return s


def terms(query: str) -> List[str]:
    """クエリを空白で分割し、各語を正規化して返す（複数語 AND 検索用）。

    空白で区切ってから正規化するのがポイント（normalize は空白を消すため、
    先に分割しないと複数語にならない）。
    """
    if not query:
        return []
    raw = _WS_RE.split(query.strip())
    out = []
    for t in raw:
        n = normalize(t)
        if n:
            out.append(n)
    return out

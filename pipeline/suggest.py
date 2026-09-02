"""検索サジェスト（入力補完）用の語彙を字幕本文から作る。

「何を検索できるのか分からない」を解消するのが目的。実際に配信で話されて
いる言い回しだけを候補にするので、選べば必ずヒットする。

作り方（形態素解析器を持ち込まずに「語らしい単位」を取り出す）:

1. 本文を NFKC + 小文字化した上で、文字・数字以外（句読点・記号・空白）で
   区切る。区切りをまたぐ n-gram は作らない。
2. 日本語の塊からは長さ 2..MAX_LEN の文字 n-gram を数える。ただし全長を
   数えるとメモリが持たないので、Apriori と同じ要領で「1文字短い n-gram が
   足切りを超えていたものだけ」を次の長さの候補にする。
3. 残った n-gram のうち、**前後の伸び方から「語として完結している」もの**だけを
   採る（詳細は :func:`_maximal`）。左へ1文字伸ばしたものがほぼ同頻度なら語の
   途中から切り出しただけ、右に続く文字の種類が少なく終端になることも少なければ
   言い回しの途中。これで「おはようございます」は残り、「はようございま」や
   「ありがとうございま」のような梯子状の断片は消える。
4. 英数字の塊は部分文字列を作らず、単語をそのまま候補にする。

正規化は :mod:`pipeline.normalize` より弱い（カナ畳み込み・空白除去をしない）。
候補をそのまま画面に出すためで、ユーザーが選んだ文字列は検索側で改めて
正規化されるため一致は保たれる。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Sequence

MIN_LEN = 2          # 日本語 n-gram の最短
MAX_LEN = 12         # 日本語 n-gram の最長（言い回しが丸ごと収まる長さが要る）
LATIN_MIN_LEN = 3    # 英単語の最短
LATIN_MAX_LEN = 20
LEFT_RATIO = 0.9     # 左へ1文字伸ばしたものがこの比率以上なら「語の途中」とみなす
MIN_BRANCH = 4       # 右に続く文字の種類がこれ以上あれば「ここで語が切れている」
FINAL_RATIO = 0.25   # 句読点等で終わる割合がこれ以上なら、それ自体で完結した言い回し
DEFAULT_LIMIT = 30000  # 実データで gzip 約 150KB。入力があって初めて取りに行く
SAMPLE_MAX = 200_000  # 語彙抽出に使う発話数の上限（全件でなくても頻出語は取れる）

# 文字・数字以外はすべて区切り（ひらがな・カタカナ・長音・漢字・英数字を残す）
_SPLIT = re.compile(
    r"[^0-9A-Za-zぁ-ヿ々㐀-䶿一-鿿豈-﫿]+"
)
_LATIN = re.compile(r"^[0-9a-z]+$")
_DIGITS = re.compile(r"^[0-9]+$")
# 英数字と日本語の境目でも切る（"thanキ" のような取り違えを候補にしない）
_SCRIPT = re.compile(r"[0-9a-z]+|[^0-9a-z]+")


def surface(text: str) -> str:
    """候補の表記を揃える軽い正規化（NFKC + 小文字化のみ）。"""
    return unicodedata.normalize("NFKC", text or "").lower()


def chunks(text: str) -> List[str]:
    """本文を「n-gram を作ってよい塊」へ分割する。

    記号・空白で切ったうえで、英数字と日本語の境目でも切る。
    """
    out: List[str] = []
    for part in _SPLIT.split(surface(text)):
        if part:
            out.extend(_SCRIPT.findall(part))
    return out


def _useless(gram: str) -> bool:
    """候補として意味のない文字列（数字だけ・同じ文字の繰り返し）。"""
    return bool(_DIGITS.match(gram)) or len(set(gram)) == 1


def _count_latin(chunk_lists: Sequence[Sequence[str]], min_count: int) -> Dict[str, int]:
    """英数字の塊は単語単位でそのまま数える。"""
    counts: Dict[str, int] = {}
    for chs in chunk_lists:
        for ch in chs:
            if LATIN_MIN_LEN <= len(ch) <= LATIN_MAX_LEN and _LATIN.match(ch):
                counts[ch] = counts.get(ch, 0) + 1
    return {g: c for g, c in counts.items() if c >= min_count and not _useless(g)}


def _count_ngrams(
    chunk_lists: Sequence[Sequence[str]], min_count: int, max_len: int
) -> Dict[int, Dict[str, int]]:
    """長さ別の n-gram 頻度。1つ短い n-gram が生き残ったものだけを数える。"""
    tables: Dict[int, Dict[str, int]] = {}
    prev: Dict[str, int] | None = None
    for n in range(MIN_LEN, max_len + 1):
        counts: Dict[str, int] = {}
        for chs in chunk_lists:
            for ch in chs:
                if _LATIN.match(ch):
                    continue  # 英数字は単語単位で別に数える
                for i in range(len(ch) - n + 1):
                    gram = ch[i : i + n]
                    if prev is not None and gram[:-1] not in prev:
                        continue  # 短い側が足切り済みなら、長い側も超えられない
                    counts[gram] = counts.get(gram, 0) + 1
        kept = {g: c for g, c in counts.items() if c >= min_count}
        if not kept:
            break
        tables[n] = kept
        prev = kept
    return tables


def _maximal(tables: Dict[int, Dict[str, int]]) -> Dict[str, int]:
    """「言い回しの途中」でしかない n-gram を落とし、語として完結したものを残す。

    素朴に「頻度が高い n-gram」を採ると、「ありがとうござい」「ありがとうございま」
    のような梯子状の断片が候補を埋め尽くす。そこで前後の伸び方を見て判定する。

    - **左**: 左へ1文字伸ばしたものがほぼ同じ頻度なら、その1文字から始まる語を
      途中で切っただけ（「はようございます」←「お」）。落とす。
    - **右**: 語の切れ目では次に来る文字が散らばる（「配信」の次は は/を/で/し …）。
      逆に「ありがとうございま」は「す」「し」しか続かない。続く文字の種類が
      MIN_BRANCH 未満なら語の途中とみなす。
      ただし「ありがとうございます」のように句読点で終わることが多い語は
      そもそも続きが少ないので、終端になる割合が高ければ残す。
    """
    ext_sum: Dict[str, int] = {}    # 右に1文字伸びた出現数の合計
    ext_kinds: Dict[str, int] = {}  # 右に続く文字の種類数
    best_left: Dict[str, int] = {}  # 左に1文字伸ばしたものの最大頻度
    for table in tables.values():
        for gram, count in table.items():
            head, tail = gram[:-1], gram[1:]
            ext_sum[head] = ext_sum.get(head, 0) + count
            ext_kinds[head] = ext_kinds.get(head, 0) + 1
            if count > best_left.get(tail, 0):
                best_left[tail] = count

    out: Dict[str, int] = {}
    for table in tables.values():
        for gram, count in table.items():
            if _useless(gram):
                continue
            if best_left.get(gram, 0) >= LEFT_RATIO * count:
                continue
            ends_here = count - ext_sum.get(gram, 0)
            if ext_kinds.get(gram, 0) < MIN_BRANCH and ends_here < FINAL_RATIO * count:
                continue
            out[gram] = count
    return out


def mine(
    texts: Sequence[str],
    min_count: int = 3,
    limit: int = DEFAULT_LIMIT,
    max_len: int = MAX_LEN,
) -> List[Dict[str, Any]]:
    """本文の並びから候補語彙を作る。頻度の高い順に最大 ``limit`` 件返す。

    戻り値: ``[{"t": 候補文字列, "n": 出現数}, ...]``
    """
    chunk_lists = [chunks(t) for t in texts]
    vocab = _maximal(_count_ngrams(chunk_lists, min_count, max_len))
    vocab.update(_count_latin(chunk_lists, min_count))
    ranked = sorted(vocab.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
    return [{"t": g, "n": c} for g, c in ranked[:limit]]


def build_suggestions(
    texts: Sequence[str], limit: int = DEFAULT_LIMIT, sample_max: int = SAMPLE_MAX
) -> Dict[str, Any]:
    """発話本文から静的サイト用のサジェスト辞書を作る。

    件数が多いときは間引いて数える（頻出語の順位は標本でも安定する）。
    足切りは標本サイズに合わせて決め、小さなコーパスでも候補が出るようにする。
    """
    stride = max(1, len(texts) // sample_max) if sample_max else 1
    sample = texts[::stride]
    min_count = max(3, len(sample) // 20000)
    return {
        "version": 1,
        "sampled": len(sample),
        "min_count": min_count,
        "items": mine(sample, min_count=min_count, limit=limit),
    }

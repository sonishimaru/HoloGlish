"""YouTube 字幕（json3 / vtt）をセグメント列にパースする。

セグメント = {start: 秒(float), dur: 秒(float), text: str}

yt-dlp の `--sub-format json3` で得られる json3 は、各イベントが tStartMs / dDurationMs
とセグメント配列 segs を持つ。自動生成字幕は 1 単語ずつ細切れなイベントに分かれることが
多いため、隣接イベントを 1 行の発話にまとめて可読性の高いスニペットにする。
"""

from __future__ import annotations

import json
import re
from typing import List, Dict, Any

# 「[音楽]」等の効果音注釈や制御文字を除去
_NOISE_RE = re.compile(r"\[[^\]]*\]")
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    text = _NOISE_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def parse_json3(raw: str) -> List[Dict[str, Any]]:
    """json3 文字列 → セグメント列。"""
    data = json.loads(raw)
    segments: List[Dict[str, Any]] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = _clean("".join(s.get("utf8", "") for s in segs))
        if not text:
            continue
        start = event.get("tStartMs", 0) / 1000.0
        dur = event.get("dDurationMs", 0) / 1000.0
        segments.append({"start": round(start, 3), "dur": round(dur, 3), "text": text})
    return _merge_adjacent(segments)


# --- VTT フォールバック（--sub-format json3 が使えない動画向け） ---

_VTT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


def _vtt_ts_to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(raw: str) -> List[Dict[str, Any]]:
    """WebVTT 文字列 → セグメント列。"""
    segments: List[Dict[str, Any]] = []
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        m = _VTT_TIME_RE.search(lines[i])
        if not m:
            i += 1
            continue
        start = _vtt_ts_to_sec(*m.group(1, 2, 3, 4))
        end = _vtt_ts_to_sec(*m.group(5, 6, 7, 8))
        i += 1
        buf: List[str] = []
        while i < len(lines) and lines[i].strip() and not _VTT_TIME_RE.search(lines[i]):
            buf.append(_TAG_RE.sub("", lines[i]))
            i += 1
        text = _clean(" ".join(buf))
        if text:
            segments.append(
                {"start": round(start, 3), "dur": round(max(end - start, 0), 3), "text": text}
            )
    return _merge_adjacent(segments)


def _merge_adjacent(
    segments: List[Dict[str, Any]], max_gap: float = 0.6, max_len: int = 120
) -> List[Dict[str, Any]]:
    """細切れの自動字幕を、句点や長さを目安に読みやすい単位へ結合する。

    重複行（自動字幕は前行を繰り返すことがある）も除去する。
    """
    merged: List[Dict[str, Any]] = []
    for seg in segments:
        if not merged:
            merged.append(dict(seg))
            continue
        prev = merged[-1]
        # 自動字幕にありがちな「前の行を含んだ次の行」を圧縮
        if seg["text"].startswith(prev["text"]):
            prev["text"] = seg["text"]
            prev["dur"] = round(seg["start"] + seg["dur"] - prev["start"], 3)
            continue
        gap = seg["start"] - (prev["start"] + prev["dur"])
        joined = f"{prev['text']} {seg['text']}"
        if gap <= max_gap and len(joined) <= max_len and not prev["text"].endswith(("。", "！", "？", ".", "!", "?")):
            prev["text"] = joined
            prev["dur"] = round(seg["start"] + seg["dur"] - prev["start"], 3)
        else:
            merged.append(dict(seg))
    return merged


def parse_subtitle_file(path: str) -> List[Dict[str, Any]]:
    """拡張子から json3 / vtt を判別してパースする。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if path.endswith(".json3") or path.endswith(".json"):
        return parse_json3(raw)
    if path.endswith(".vtt"):
        return parse_vtt(raw)
    # 中身で判定（json3 は先頭が JSON）
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        return parse_json3(raw)
    return parse_vtt(raw)

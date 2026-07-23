"""collect コマンドの時間予算（グレースフルな打ち切り）のテスト。

ネットワークには一切アクセスしない: 予算が尽きたら列挙・字幕取得の関数へ
到達しないことを、それらを「呼ばれたら失敗」に差し替えて検証する。
"""

import argparse

import pytest

from pipeline import run


def _args(db_path, **over):
    base = dict(
        db=db_path, branch=None, members=None, limit=5, date_after=None,
        raw_dir="data/raw", sleep=0.0, retries=1, retry_base=0.0,
        time_budget=0.0, force=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_time_budget_stops_before_network(tmp_path, monkeypatch, capsys):
    """予算超過時は最初のチャンネル列挙にすら到達せず区切る。"""
    import pipeline.fetch_videos as fv

    def _boom(*a, **k):  # 呼ばれてはいけない
        raise AssertionError("予算超過後にネットワークへ到達した")

    monkeypatch.setattr(fv, "list_channel_videos", _boom)

    # 疑似時計: deadline 計算で 1000、以降は 9999（=常に超過）
    clock = iter([1000.0] + [9999.0] * 50)
    monkeypatch.setattr(run.time, "monotonic", lambda: next(clock))

    rc = run.cmd_collect(_args(str(tmp_path / "c.db"), time_budget=5.0))
    assert rc == 0
    out = capsys.readouterr().out
    assert "時間予算" in out  # 区切りメッセージ


def test_no_budget_runs_normally(tmp_path, monkeypatch, capsys):
    """time_budget=0 なら無制限（列挙は呼ばれる。動画0本で正常終了）。"""
    import pipeline.fetch_videos as fv

    calls = {"n": 0}

    def _empty(*a, **k):
        calls["n"] += 1
        return []

    monkeypatch.setattr(fv, "list_channel_videos", _empty)
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)

    rc = run.cmd_collect(_args(str(tmp_path / "c.db"), time_budget=0.0))
    assert rc == 0
    assert calls["n"] >= 1  # 各チャンネルの列挙に到達している
    assert "完了" in capsys.readouterr().out

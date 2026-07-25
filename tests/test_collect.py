"""collect コマンドの時間予算（グレースフルな打ち切り）のテスト。

ネットワークには一切アクセスしない: 予算が尽きたら列挙・字幕取得の関数へ
到達しないことを、それらを「呼ばれたら失敗」に差し替えて検証する。
"""

import argparse

import pytest

from pipeline import run


def _args(db_path, **over):
    base = dict(
        db=db_path, branch=None, members=None, limit=5, list_depth=0,
        date_after=None, raw_dir="data/raw", sleep=0.0, retries=1,
        retry_base=0.0, time_budget=0.0, force=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_error_streak_breaker_stops_early(tmp_path, monkeypatch, capsys):
    """連続失敗がしきい値に達したら打ち切る（IPブロック中の空振り連打を防ぐ）。"""
    import pipeline.fetch_videos as fv
    import pipeline.fetch_subtitles as fs
    from pipeline import db

    vids = [{"video_id": f"v{i}", "title": f"t{i}", "url": f"u{i}"} for i in range(50)]
    monkeypatch.setattr(fv, "list_channel_videos", lambda *a, **k: list(vids))

    calls = {"n": 0}

    def _blocked(*a, **k):
        calls["n"] += 1
        raise Exception("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(fs, "fetch_subtitle", _blocked)
    monkeypatch.setattr(fs, "fetch_transcript_api", _blocked)
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)

    dbp = str(tmp_path / "breaker.db")
    rc = run.cmd_collect(_args(dbp, members="Sakura Miko", limit=0, error_streak=5))
    assert rc == 0
    # しきい値5で止まる（50本全部は試みない）
    assert calls["n"] == 10  # ytdlp+api の2経路 × 5本
    out = capsys.readouterr().out
    assert "連続 5 本の失敗を検知" in out
    conn = db.connect(dbp)
    n_err = conn.execute("SELECT count(*) FROM processed WHERE status='error'").fetchone()[0]
    conn.close()
    assert n_err == 5


def test_error_streak_resets_on_success(tmp_path, monkeypatch, capsys):
    """成功を挟むと連続カウントはリセットされ、打ち切られない。"""
    import pipeline.fetch_videos as fv
    import pipeline.fetch_subtitles as fs

    vids = [{"video_id": f"v{i}", "title": f"t{i}", "url": f"u{i}"} for i in range(6)]
    monkeypatch.setattr(fv, "list_channel_videos", lambda *a, **k: list(vids))

    state = {"i": 0}

    def _alternating(*a, **k):
        state["i"] += 1
        if state["i"] % 2 == 0:
            return [{"start": 0.0, "dur": 1.0, "text": "おはよう"}], "ja", "auto"
        raise Exception("429")

    monkeypatch.setattr(fs, "fetch_subtitle", lambda *a, **k: (_ for _ in ()).throw(Exception("429")))
    monkeypatch.setattr(fs, "fetch_transcript_api", _alternating)
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)

    dbp = str(tmp_path / "reset.db")
    rc = run.cmd_collect(_args(dbp, members="Sakura Miko", limit=0, error_streak=2))
    assert rc == 0
    out = capsys.readouterr().out
    # 交互に成功するため連続2失敗に達せず、完了メッセージで終わる
    assert "完了" in out


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


def test_deep_reach_advances_over_runs(tmp_path, monkeypatch):
    """列挙は全件・処理は --limit 本ずつ。実行を繰り返すと古い方へ前進する。"""
    import pipeline.fetch_videos as fv
    import pipeline.fetch_subtitles as fs
    from pipeline import db

    # 1チャンネルに 5 本。新しい順 v0..v4 を毎回全件返す（list-depth=全件を模す）。
    vids = [{"video_id": f"v{i}", "title": f"t{i}", "url": f"u{i}"} for i in range(5)]
    monkeypatch.setattr(fv, "list_channel_videos", lambda *a, **k: list(vids))
    # 字幕は無し(None)で確定させる（parse 不要。no_subs として処理済みになる）。
    # フォールバック経路(fetch_transcript_api)も差し替えないと実ネットワークへ出てしまう。
    monkeypatch.setattr(fs, "fetch_subtitle", lambda *a, **k: None)
    monkeypatch.setattr(fs, "fetch_transcript_api", lambda *a, **k: None)

    dbp = str(tmp_path / "reach.db")

    def processed_ids():
        conn = db.connect(dbp)
        ids = {r[0] for r in conn.execute("SELECT video_id FROM processed")}
        conn.close()
        return ids

    # 1回目: 最新 2 本 (v0,v1)
    run.cmd_collect(_args(dbp, members="Sakura Miko", limit=2))
    assert processed_ids() == {"v0", "v1"}
    # 2回目: 次の 2 本 (v2,v3) へ前進
    run.cmd_collect(_args(dbp, members="Sakura Miko", limit=2))
    assert processed_ids() == {"v0", "v1", "v2", "v3"}
    # 3回目: 残り (v4)。全件処理済みでこれ以上増えない
    run.cmd_collect(_args(dbp, members="Sakura Miko", limit=2))
    assert processed_ids() == {"v0", "v1", "v2", "v3", "v4"}
    run.cmd_collect(_args(dbp, members="Sakura Miko", limit=2))
    assert processed_ids() == {"v0", "v1", "v2", "v3", "v4"}

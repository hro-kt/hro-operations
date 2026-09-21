"""agent の kind→コマンド写像(flow 戦略まわり)のテスト。DB/JV-Link は使わない。"""

from __future__ import annotations

import pytest

from hro_operations import agent


def test_flow_day_paper_defaults(monkeypatch):
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    cmd, cwd, env = agent._b_flow_day({"date": "20260919"})
    assert cmd == ["bash", "scripts/flow_day.sh"] and cwd.endswith("hro-operations")
    assert env["MODE"] == "paper" and env["FLOW_THRESHOLD"] == "0.2802"
    # 既定は「締切30秒前に投票開始」= 発走-90s。その時点で存在する最新スナップは 発走-120s。
    assert env["FLOW_SOURCE"] == "sokuho" and env["LEAD_SECONDS"] == "90"
    assert env["FLOW_LEAD"] == "120"


def test_flow_day_live_requires_confirm_and_limits(monkeypatch):
    """live は UI からでも多重ゲート: confirm_live と 1件/1日上限が無ければ組み立てない。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    with pytest.raises(ValueError):
        agent._b_flow_day({"mode": "live"})
    with pytest.raises(ValueError):
        agent._b_flow_day({"mode": "live", "confirm_live": True, "max_amount_per_order": 1000})
    # 時刻が成立する組でのみ通る(決定 T-180s > 実行 T-90s > 締切 T-60s)。
    _, _, env = agent._b_flow_day({"mode": "live", "confirm_live": True,
                                   "max_amount_per_order": 1000, "max_amount_per_day": 20000,
                                   "lead_seconds": 180, "act_lead_seconds": 90})
    assert env["MODE"] == "live" and env["CONFIRM_LIVE"] == "1"
    assert env["MAX_PER_ORDER"] == "1000" and env["MAX_PER_DAY"] == "20000"


_LIVE = {"mode": "live", "confirm_live": True,
         "max_amount_per_order": 1000, "max_amount_per_day": 20000}


def test_flow_day_live_rejects_order_time_after_deadline(monkeypatch):
    """★実行時刻が締切より後だと live を組み立てない。

    この設定は「1日走りきって0件」という最悪の壊れ方をする(run-day は T-act まで待って
    発注するが、ガードは T-60s を過ぎた発注を捨てる)。開催日は取り返しがつかない。
    """
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    with pytest.raises(ValueError, match="締切"):
        agent._b_flow_day({**_LIVE, "act_lead_seconds": 30, "lead_seconds": 60})


def test_flow_day_live_rejects_snapshot_not_yet_available(monkeypatch):
    """決定に使うスナップショットが実行時刻にまだ無い組み合わせも弾く。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    with pytest.raises(ValueError, match="まだ存在しません"):
        agent._b_flow_day({**_LIVE, "act_lead_seconds": 90, "lead_seconds": 60})


def test_flow_day_paper_keeps_running_but_reports_timing_problem(monkeypatch):
    """paper は計測走行なので、時刻が破綻していても止めず警告だけ返す。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    _, _, env = agent._b_flow_day({"date": "20260926", "act_lead_seconds": 30})
    assert env["MODE"] == "paper" and env["LEAD_SECONDS"] == "30"
    p = agent._flow_day_params({"date": "20260926", "act_lead_seconds": 30})
    assert "締切" in p["timing_problem"]


def test_act_time_is_relative_to_deadline(monkeypatch):
    """運用の基準は「締切の何秒前に投票を開始するか」。締切=発走-60s から逆算する。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    p15 = agent._flow_day_params({"date": "20260926", "act_before_deadline_seconds": 15})
    p30 = agent._flow_day_params({"date": "20260926", "act_before_deadline_seconds": 30})
    assert p15["act_lead"] == 75 and p30["act_lead"] == 90     # 発走基準へ変換
    assert p15["timing_problem"] is None and p30["timing_problem"] is None


def test_threshold_not_transferable_across_source_or_lead(monkeypatch):
    """0.2802 は (ts / 発走-60s) の絶対値。設定が違えば取り直しを促す。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    p = agent._flow_day_params({"date": "20260926"})          # 既定 sokuho/120
    assert "flow-threshold" in p["threshold_note"]
    same = agent._flow_day_params({"date": "20260926", "source": "ts", "lead_seconds": 60})
    assert "threshold_note" not in same                        # 検証と同条件なら黙る
    other = agent._flow_day_params({"date": "20260926", "threshold": 0.31})
    assert "threshold_note" not in other                       # 取り直し済みの値には言わない


def test_flow_day_windows_builds_run_day_without_bash(monkeypatch):
    """Windows 経路は bash に依存せず run-day を直接起動する。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    cmd, cwd, _ = agent._b_flow_day_windows({**_LIVE, "date": "20260926",
                                             "lead_seconds": 180, "act_lead_seconds": 90})
    assert cmd[:4] == ["poetry", "run", "hro-ops", "run-day"] and "bash" not in cmd
    assert "--confirm-live" in cmd and "--ipat-recipe" in cmd
    assert cmd[cmd.index("--lead-seconds") + 1] == "90"
    assert cmd[cmd.index("--deadline-lead-seconds") + 1] == "60"
    assert cwd.endswith("hro-operations")


def test_fetch_ts_odds_resident_limits_scope():
    cmd, cwd, _ = agent._b_fetch_ts_odds({"date": "20260919", "repeat_seconds": 20})
    assert "--repeat-seconds" in cmd and "--within-minutes" in cmd and cwd.endswith("hro-synchronizer")
    cmd1, _, _ = agent._b_fetch_ts_odds({"date": "20260919"})
    assert "--repeat-seconds" not in cmd1


def test_new_kinds_are_registered():
    assert {"flow_day", "flow_check", "import_results", "jrdb_load"} <= set(agent._COMMANDS["vm"])
    assert {"fetch_ts_odds", "env_check"} <= set(agent._COMMANDS["windows"])


def test_run_odds_limits_scope_so_each_race_is_polled_within_a_minute():
    """★絞らないと当日全レースを毎周なめる。2026-09-21 の実測で24レース/1周82秒。
    締切30秒前に 発走-120秒 のスナップを使うには直前にそのレースを取れている必要がある。"""
    cmd, cwd, env = agent._b_run_odds({"date": "20260921"})
    assert cmd[cmd.index("--within-minutes") + 1] == "20"
    assert cmd[cmd.index("--past-minutes") + 1] == "5"
    assert env["ODDS_SPEC"] == "0B30"
    assert cwd.endswith("hro-synchronizer")
    # UI から広げられること
    wide, _, _ = agent._b_run_odds({"date": "20260921", "within_minutes": 45})
    assert wide[wide.index("--within-minutes") + 1] == "45"


def test_flow_day_passes_per_lead_thresholds(monkeypatch):
    """「T-120s が間に合ったレースだけ買う」= 120 だけ渡す。他のリードは run-day が見送る。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    _, _, env = agent._b_flow_day({"date": "20260926", "thresholds": {"120": 0.1631}})
    assert env["FLOW_THRESHOLDS"] == '{"120": 0.1631}'
    cmd, _, _ = agent._b_flow_day_windows({"date": "20260926", "thresholds": {"120": 0.1631}})
    assert cmd[cmd.index("--flow-thresholds") + 1] == '{"120": 0.1631}'


def test_flow_day_rejects_leads_off_the_announcement_grid(monkeypatch):
    """発表時刻は分格子なので、60の倍数以外のリードは実測され得ない=設定ミス。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    with pytest.raises(ValueError, match="60秒の倍数"):
        agent._b_flow_day({"date": "20260926", "thresholds": {"117": 0.1}})
    with pytest.raises(ValueError, match="JSON"):
        agent._b_flow_day({"date": "20260926", "thresholds": "not json"})


def test_run_odds_poll_interval_is_tunable():
    """★到着遅れの一部は自分のサンプリング待ち(平均 周期/2)。対象を絞れば1周1秒未満
    なので、周期を詰めれば無料で数秒縮まる。締切直前の価格を掴めるかに直結する。"""
    _, _, env = agent._b_run_odds({"date": "20260922"})
    assert env["ODDS_POLL_INTERVAL_SEC"] == "3.0"
    _, _, env2 = agent._b_run_odds({"date": "20260922", "poll_interval_sec": 2})
    assert env2["ODDS_POLL_INTERVAL_SEC"] == "2.0"

"""agent の kind→コマンド写像(flow 戦略まわり)のテスト。DB/JV-Link は使わない。"""

from __future__ import annotations

import pytest

from hro_operations import agent


def test_flow_day_paper_defaults(monkeypatch):
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    cmd, cwd, env = agent._b_flow_day({"date": "20260919"})
    assert cmd == ["bash", "scripts/flow_day.sh"] and cwd.endswith("hro-operations")
    assert env["MODE"] == "paper" and env["FLOW_THRESHOLD"] == "0.2802"
    assert env["FLOW_SOURCE"] == "ts" and env["LEAD_SECONDS"] == "30"


def test_flow_day_live_requires_confirm_and_limits(monkeypatch):
    """live は UI からでも多重ゲート: confirm_live と 1件/1日上限が無ければ組み立てない。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    with pytest.raises(ValueError):
        agent._b_flow_day({"mode": "live"})
    with pytest.raises(ValueError):
        agent._b_flow_day({"mode": "live", "confirm_live": True, "max_amount_per_order": 1000})
    _, _, env = agent._b_flow_day({"mode": "live", "confirm_live": True,
                                   "max_amount_per_order": 1000, "max_amount_per_day": 20000})
    assert env["MODE"] == "live" and env["CONFIRM_LIVE"] == "1"
    assert env["MAX_PER_ORDER"] == "1000" and env["MAX_PER_DAY"] == "20000"


def test_fetch_ts_odds_resident_limits_scope():
    cmd, cwd, _ = agent._b_fetch_ts_odds({"date": "20260919", "repeat_seconds": 20})
    assert "--repeat-seconds" in cmd and "--within-minutes" in cmd and cwd.endswith("hro-synchronizer")
    cmd1, _, _ = agent._b_fetch_ts_odds({"date": "20260919"})
    assert "--repeat-seconds" not in cmd1


def test_new_kinds_are_registered():
    assert {"flow_day", "flow_check", "import_results", "jrdb_load"} <= set(agent._COMMANDS["vm"])
    assert {"fetch_ts_odds", "env_check"} <= set(agent._COMMANDS["windows"])

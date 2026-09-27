

def test_flow_day_params_keep_netkeiba_source():
    """★以前は `"ts" if src=="ts" else "sokuho"` と書いており、netkeiba を指定しても
    黙って sokuho で走った。別ソースの結果を netkeiba の成績として記録してしまう。"""
    from hro_operations.agent import _flow_day_params

    assert _flow_day_params({"source": "netkeiba"})["source"] == "netkeiba"
    assert _flow_day_params({"source": "ts"})["source"] == "ts"
    assert _flow_day_params({"source": "sokuho"})["source"] == "sokuho"
    assert _flow_day_params({"source": "でたらめ"})["source"] == "sokuho"
    assert _flow_day_params({})["source"] == "sokuho"


def test_netkeiba_odds_job_is_vm_only_and_guards_the_window():
    """★netkeiba は JV-Link を使わないので VM で動かし、Windows の1プロセス制約とは
    競合しない。★--within-minutes を 8 より下げると起点(発走6分前)が取れなくなり、
    そのレースが丸ごと見送りになるので下限で守る。"""
    from hro_operations.agent import _COMMANDS, _b_netkeiba_odds

    assert "netkeiba_odds" in _COMMANDS["vm"]
    assert "netkeiba_odds" not in _COMMANDS["windows"]

    cmd, cwd, env = _b_netkeiba_odds({"date": "20260927", "within_minutes": 3})
    assert "netkeiba-odds" in cmd
    assert cmd[cmd.index("--within-minutes") + 1] == "8"      # 下限で守る
    assert cmd[cmd.index("--date") + 1] == "20260927"
    assert cwd.endswith("hro-synchronizer")

    _c, _w, env2 = _b_netkeiba_odds({"state": "/home/azureuser/.netkeiba_state.json"})
    assert env2["NETKEIBA_STATE"].endswith(".netkeiba_state.json")


def test_child_env_does_not_leak_the_agents_virtualenv(monkeypatch):
    """★agent は hro-operations の venv で動く。os.environ をそのまま子へ渡すと
    VIRTUAL_ENV が残り、`poetry run` が「既に仮想環境が有効」と判断して
    **別パッケージを hro-operations の venv で実行**する。
    2026-09-27 に netkeiba_odds(hro-synchronizer)で ModuleNotFoundError: yaml として露見。"""
    import os

    from hro_operations.agent import _child_env

    monkeypatch.setenv("VIRTUAL_ENV", "/home/u/.venvs/ops")
    monkeypatch.setenv("POETRY_ACTIVE", "1")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setenv("PATH", os.pathsep.join(
        ["/home/u/.venvs/ops/bin", "/usr/local/bin", "/usr/bin"]))

    env = _child_env({"ODDS_SPEC": "0B30"})
    assert "VIRTUAL_ENV" not in env and "POETRY_ACTIVE" not in env
    assert "PYTHONPATH" not in env
    assert "/home/u/.venvs/ops/bin" not in env["PATH"].split(os.pathsep)
    assert "/usr/bin" in env["PATH"].split(os.pathsep)   # 他は残す
    assert env["ODDS_SPEC"] == "0B30"                    # ジョブ固有の env は通す


def test_threshold_grid_check_is_source_aware():
    """★60秒格子は JV-Link(発表時刻が分刻み)の制約。netkeiba は実時刻なので 75 が正しい。
    信号源を見ずに弾いていたため、正しい netkeiba 設定でランナーが起動できなかった
    (2026-09-27 に実害。フロントだけ直して agent を直し忘れた)。"""
    import pytest

    from hro_operations.agent import _flow_day_params, _thresholds

    assert _thresholds({"75": 0.1533}, "netkeiba") == {75: 0.1533}
    assert _thresholds({"120": 0.1583}, "sokuho") == {120: 0.1583}
    with pytest.raises(ValueError, match="60秒の倍数"):
        _thresholds({"75": 0.1533}, "sokuho")

    p = _flow_day_params({"source": "netkeiba", "thresholds": '{"75": 0.1533}',
                          "lead_seconds": 75})
    assert p["source"] == "netkeiba" and p["thresholds"] == {75: 0.1533}
    assert p["flow_lead"] == 75

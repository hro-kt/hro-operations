

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

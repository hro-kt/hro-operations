

def test_flow_day_params_keep_netkeiba_source():
    """★以前は `"ts" if src=="ts" else "sokuho"` と書いており、netkeiba を指定しても
    黙って sokuho で走った。別ソースの結果を netkeiba の成績として記録してしまう。"""
    from hro_operations.agent import _flow_day_params

    assert _flow_day_params({"source": "netkeiba"})["source"] == "netkeiba"
    assert _flow_day_params({"source": "ts"})["source"] == "ts"
    assert _flow_day_params({"source": "sokuho"})["source"] == "sokuho"
    assert _flow_day_params({"source": "でたらめ"})["source"] == "sokuho"
    assert _flow_day_params({})["source"] == "sokuho"

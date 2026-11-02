"""決定時点の単勝オッズ帯(flow_min_tan_odds)。

★2026-11-02 の2窓OOS(単勝 / 人気7+ / ts@T-60s / 上位5%):
    全帯      窓1 1.4583(774本 P=0.019) / 窓2 1.5772(690本 P=0.009)
    20倍以上  窓1 1.4980(710本 P=0.025) / 窓2 **1.9069**(641本 P=0.000)
  外れる 7-20倍 だけを取り出すと 窓1 1.31 / 窓2 0.90。
  «負ける帯» ではなく **«当てにならない帯»** で、3期間の過去記録
  (4.0-20倍「前後で入れ替わり一貫性なし」)と5窓ぶん一致した。

★設定の受け渡しは**鎖**になっていて、1箇所落とすと黙って既定に戻る。
  同じ落とし方で 2026-10-24 に preselect が別の式別を選び続けていた。
  鎖の全部をここで縛る。
"""

from __future__ import annotations

from hro_operations.agent import _b_flow_day, _b_flow_day_windows, _b_race_day
from hro_operations.race_day import DayConfig, flow_config


def _cfg(**kw) -> DayConfig:
    return DayConfig(date="20261102", win_model="m", place_model="m",
                     results_path="r.jsonl", strategy="flow", **kw)


def test_the_band_reaches_the_signal():
    """★DayConfig → FlowConfig。ここが唯一の組み立て場所。"""
    fc = flow_config(_cfg(flow_min_tan_odds=20.0, flow_max_tan_odds=40.0))
    assert fc.min_tan_odds == 20.0
    assert fc.max_tan_odds == 40.0


def test_the_default_is_no_band():
    fc = flow_config(_cfg())
    assert fc.min_tan_odds == 0.0 and fc.max_tan_odds == 0.0


def test_the_band_survives_the_job_arguments():
    """★admin/orchestrator から来る args が子プロセスの引数まで届くこと。"""
    cmd, _cwd, env = _b_flow_day({"date": "20261102", "min_tan_odds": 20})
    assert env["MIN_TAN_ODDS"] == "20.0"

    cmd, _cwd, _env = _b_flow_day_windows({"date": "20261102", "min_tan_odds": 20})
    i = cmd.index("--flow-min-tan-odds")
    assert cmd[i + 1] == "20.0"


def test_race_day_forwards_the_band_to_its_children():
    """★race_day は子に生の args を渡す。鍵の表に入っていないと落ちる。"""
    import json

    cmd, _cwd, _env = _b_race_day({"date": "20261102", "min_tan_odds": 20,
                                   "max_tan_odds": 40})
    args = json.loads(cmd[cmd.index("--flow-args") + 1])
    assert args["min_tan_odds"] == 20
    assert args["max_tan_odds"] == 40


def test_the_band_is_part_of_the_strategy_version():
    """★戦略の版は flow_config の結果から作る。ここに入っていない設定は
       «別の戦略が同じ版に見える» ことになり、税務の記録として致命的。
    """
    a = flow_config(_cfg(flow_min_tan_odds=0.0))
    b = flow_config(_cfg(flow_min_tan_odds=20.0))
    assert a != b

"""CLI の既定値が FlowConfig と食い違っていないこと。

★2026-11-03 発覚。`flow-backtest` だけ --flow-minutes の既定が 11、
  --flow-lead-seconds の既定が 360 だった(他は全部 6 / 60、live も 6 / 60)。
  そのため**検証は起点11分、本番は起点6分**という別物を比べていた。
  明示すれば避けられるが、「明示し忘れた1回」が黙って別の設定になるのが問題。
"""

from __future__ import annotations

import re
from pathlib import Path

from hro_operations.flow_signal import FlowConfig
from hro_operations.race_day import DayConfig

_SRC = (Path(__file__).resolve().parents[1]
        / "hro_operations" / "__main__.py").read_text(encoding="utf-8")


# ★live と同じ設定で測る/本番へ渡す数字を作るコマンド。ここの既定がずれると、
#   «検証した設定» と «動いている設定» が黙って別物になる。
#   p_pk(flow-picks) / p_pf は netkeiba 前提で T-80/75 を既定にしてあり、
#   信号源が違うので別扱い(変えるときは意図して変えること)。
MUST_MATCH_LIVE = ("p_bt",   # flow-backtest  … 成績を測る
                   "p_th",   # flow-threshold … 本番へ渡す閾値を作る
                   "p_fs",   # flow-slice     … 帯を見る
                   "p_sw",   # flow-sweep
                   "p_cb",   # flow-combo
                   "p_fd",   # flow-debug
                   "p_fc")   # flow-coverage


def _defaults(flag: str) -> dict[str, int]:
    pat = rf'(\w+)\.add_argument\("{flag}",\s*type=int,\s*default=(\d+)'
    return {who: int(v) for who, v in re.findall(pat, _SRC)}


def test_flow_minutes_defaults_match_live():
    got = _defaults("--flow-minutes")
    assert got, "--flow-minutes が見つからない"
    want = FlowConfig().flow_minutes
    bad = {k: v for k, v in got.items() if k in MUST_MATCH_LIVE and v != want}
    assert not bad, f"live は {want} 分なのにずれている: {bad}"


def test_flow_lead_seconds_defaults_match_live():
    """★閾値の尺度は決定時点で変わる(T-60s +0.1675 / T-360s +0.0691)。
       ここがずれていると «T-120 の閾値を T-60 の本番へ» が起きる。"""
    got = _defaults("--flow-lead-seconds")
    assert got, "--flow-lead-seconds が見つからない"
    want = FlowConfig().lead_seconds
    bad = {k: v for k, v in got.items() if k in MUST_MATCH_LIVE and v != want}
    assert not bad, f"live は T-{want}s なのにずれている: {bad}"


def test_the_list_of_commands_is_not_silently_empty():
    """★MUST_MATCH_LIVE の名前が変わったら、検査が素通りする。"""
    got = _defaults("--flow-lead-seconds")
    assert set(MUST_MATCH_LIVE) <= set(got), set(MUST_MATCH_LIVE) - set(got)


def test_the_live_config_uses_the_same_defaults():
    """★検証と本番が同じ値から始まること。ここがずれると、
       «検証した設定» と «動いている設定» が別物になる。"""
    d = DayConfig(date="20261103", win_model="m", place_model="m",
                  results_path="r.jsonl")
    assert d.flow_minutes == FlowConfig().flow_minutes
    assert d.flow_lead_seconds == FlowConfig().lead_seconds
    assert d.flow_source == FlowConfig().source

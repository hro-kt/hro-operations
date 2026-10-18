"""朝の preflight が、自分で起こす収集の不在を理由に止まらないこと。

★2026-10-18 09:30 の実害:
    ✗ preflight: 速報(ts_sokuho_o1)が空です
    ✗ preflight: netkeiba(ts_netkeiba_o1)が空です
    走り出す前に落とします
  その収集を起動するのは**このオーケストレータ自身**。起動時点で空なのは常に正常。
  最終発走16:30の日の第1レースは09:50ごろで、09:30 は mins_to_first=20。
  判定が `mins > window`(20>20=False)だったため「もう収集していて当然」側に倒れた。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hro_operations.preflight import check  # noqa: E402


class _Cfg:
    flow_source = "netkeiba"
    flow_lead_seconds = 90
    flow_thresholds = {90: 0.0345}
    lead_seconds = 61
    deadline_lead_seconds = 60
    collect_within_minutes = 20


class _DB:
    def __init__(self, mins_to_first, rows=0):
        self.mins, self.rows = mins_to_first, rows

    def query(self, sql, key):
        if "information_schema" in sql:
            return [{"column_name": c} for c in
                    ("id", "strategy_version_id", "race_id", "amount")]
        if "FROM nl_ra" in sql or "mins_to_first" in sql:
            return [{"n": 36, "mins_to_first": self.mins}]
        return [{"rows": self.rows}]


def _problems(mins, *, starting, rows=0):
    out = check(_DB(mins, rows), "20261018", _Cfg(), collectors_starting=starting)
    return out["problems"], out["notes"]


def test_startup_does_not_demand_data_it_is_about_to_collect():
    """★収集ウィンドウに入っていても、起動時点で空なのは正常。止めてはいけない。"""
    probs, notes = _problems(5, starting=True)
    assert not [p for p in probs if "空です" in p], probs
    assert any("空です" in n for n in notes), "黙って無視もしない"
    assert any("これから起動する" in n for n in notes), notes


def test_the_2026_10_18_case_passes():
    """★実害そのもの。最終発走16:30の日の09:30、第1レースまで20分。"""
    probs, _ = _problems(20, starting=True)
    assert probs == [], probs


def test_a_standalone_runner_still_gets_a_problem():
    """★flow_day 単体は他で動いている収集に依存する。そちらでは催促してよい。"""
    probs, _ = _problems(5, starting=False)
    assert any("ts_sokuho_o1" in p for p in probs), probs
    assert any("ts_netkeiba_o1" in p for p in probs), probs


def test_exactly_at_the_window_edge_is_still_too_early():
    """★ちょうど window 分前は、収集がいま始まる瞬間。まだ空が正常。"""
    probs, _ = _problems(20, starting=False)
    assert not [p for p in probs if "空です" in p], probs


def test_inside_the_window_is_not_too_early():
    probs, _ = _problems(19, starting=False)
    assert any("空です" in p for p in probs), probs


def test_unknown_first_post_still_complains_when_not_starting():
    """★None を『分からないから催促しない』にはしない。

    None は「予定のレースが無い」= もう遅い時間、のことがある。
    そこで黙ると、収集が本当に死んでいる場合を隠す。
    救済は collectors_starting の側でやる。
    """
    probs, _ = _problems(None, starting=False)
    assert any("空です" in p for p in probs), probs
    probs2, _ = _problems(None, starting=True)
    assert not [p for p in probs2 if "空です" in p], probs2


def test_data_present_means_no_complaint_either_way():
    for starting in (True, False):
        probs, _ = _problems(5, starting=starting, rows=1200)
        assert not [p for p in probs if "空です" in p], (starting, probs)


def test_config_problems_are_still_fatal_at_startup():
    """★設定の不整合は朝に落として当然。収集の不在とは別物。"""
    class _Bad(_Cfg):
        flow_thresholds = {60: 0.1}        # キーが 90 でない

    out = check(_DB(120, 0), "20261018", _Bad(), collectors_starting=True)
    assert any("閾値のキー" in p for p in out["problems"]), out["problems"]

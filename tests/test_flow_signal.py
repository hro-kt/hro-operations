"""flow_signal のスコア計算(DB を使わない部分)のテスト。"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from hro_operations.flow_signal import FlowConfig, flow_orders, flow_scores

RACE = ("2026", "0919", "06", "04", "05", "11")


class FakeDB:
    """db.query(sql, params) -> list[dict] だけを模す。"""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query(self, sql, params):
        self.calls.append((sql, params))
        return self.rows


def _row(um, tan, fuku, which, ts=datetime(2026, 9, 19, 15, 43, tzinfo=timezone.utc)):
    return {"umaban": um, "tan_odds": tan, "fuku_odds_low": fuku, "ts": ts, "which": which}


def test_score_is_logit_difference_of_win_pool_share():
    # 2頭。late で 01 のオッズが下がる(=人気が集まる)→ 01 のスコアは正。
    rows = [_row("01", "20", "15", "late"), _row("02", "30", "20", "late"),
            _row("01", "30", "15", "early"), _row("02", "30", "20", "early")]
    sc = flow_scores(FakeDB(rows), RACE, FlowConfig())
    assert set(sc) == {"01", "02"}
    assert sc["01"]["score"] > 0 > sc["02"]["score"]
    # 手計算と一致すること(オッズは10倍整数文字列 → 2.0 / 3.0)
    s_late, s_early = 1 / 2.0 + 1 / 3.0, 1 / 3.0 + 1 / 3.0
    def logit(p):
        return math.log(p / (1 - p))
    assert sc["01"]["score"] == logit((1 / 2.0) / s_late) - logit((1 / 3.0) / s_early)


def test_missing_snapshot_yields_no_scores():
    assert flow_scores(FakeDB([_row("01", "20", "15", "late")]), RACE, FlowConfig()) == {}
    assert flow_scores(FakeDB([]), RACE, FlowConfig()) == {}


def test_invalid_odds_rows_are_skipped():
    """オッズが '0000'(発売前/取消)の馬は候補にしない。"""
    rows = [_row("01", "20", "15", "late"), _row("02", "0000", "0000", "late"),
            _row("01", "30", "15", "early"), _row("02", "0000", "0000", "early")]
    sc = flow_scores(FakeDB(rows), RACE, FlowConfig())
    assert set(sc) == {"01"}


def test_orders_only_above_threshold_and_under_max_odds():
    rows = [_row("01", "20", "15", "late"), _row("02", "30", "9999", "late"),
            _row("01", "30", "15", "early"), _row("02", "30", "9999", "early")]
    db = FakeDB(rows)
    orders = flow_orders(db, RACE, FlowConfig(threshold=0.0), 100, "flow_tan")
    assert [o.selection_id for o in orders] == ["01"]        # 02 はスコア負で除外
    o = orders[0]
    assert o.bet_type == "place" and o.amount == 100 and o.race_id == "2026091906040511"
    assert o.odds == 1.5 and "flow_tan=" in o.reason

    high = flow_orders(db, RACE, FlowConfig(threshold=99.0), 100, "flow_tan")
    assert high == []                                        # 閾値超えなし

    capped = flow_orders(db, RACE, FlowConfig(threshold=0.0, max_odds=1.2), 100, "flow_tan")
    assert capped == []                                      # オッズ上限で除外


def test_reason_survives_missing_timestamp():
    """ts が NULL でも理由文の整形で落ちない(発注を止めない)。"""
    rows = [_row("01", "20", "15", "late", ts=None), _row("02", "30", "20", "late", ts=None),
            _row("01", "30", "15", "early", ts=None), _row("02", "30", "20", "early", ts=None)]
    orders = flow_orders(FakeDB(rows), RACE, FlowConfig(threshold=0.0), 100, "flow_tan")
    assert [o.selection_id for o in orders] == ["01"]
    assert "late=None" in orders[0].reason


def test_flow_diagnose_collects_post_snapshots_and_scores():
    """発注が出ない理由(発走時刻・スナップ・スコア)を1回で切り分けられること。"""
    from hro_operations.flow_signal import flow_diagnose

    class DiagDB:
        def query(self, sql, params):
            # スコア用SQLにも ra CTE(FROM nl_ra)が入るので、判定順を間違えない
            if "UNION ALL" in sql:
                return [_row("01", "20", "15", "late"), _row("02", "30", "20", "late"),
                        _row("01", "30", "15", "early"), _row("02", "30", "20", "early")]
            if "count(*)" in sql:
                return [{"rows": 100, "snaps": 50, "horses": 2,
                         "first_ts": "a", "last_ts": "b"}]
            return [{"hasso_time": "1510", "post": "2026-09-19T15:10:00+09:00"}]

    d = flow_diagnose(DiagDB(), RACE, FlowConfig(threshold=0.0))
    assert d["post"]["hasso_time"] == "1510"
    assert d["snapshots"]["snaps"] == 50
    assert set(d["scores"]) == {"01", "02"}
    assert d["n_above"] == 1           # 01 だけが閾値超え

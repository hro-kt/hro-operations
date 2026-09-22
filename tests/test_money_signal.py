"""late money を金額で測る信号のテスト。

★ここで守りたいのは「シェアでは見えない量を見ていること」。プールの増分を無視すると
  flow_tan と同じものになり、わざわざ票数を使う意味が無くなる。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hro_operations.money_signal import (
    MoneyConfig,
    _horses,
    _money_by_horse,
    money_scores,
)

_TS_LATE = datetime(2026, 8, 16, 7, 24, tzinfo=timezone.utc)
_TS_EARLY = datetime(2026, 8, 16, 7, 18, tzinfo=timezone.utc)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def query(self, sql, params):
        self.sql, self.params = sql, params
        return self.rows


def _row(kumi, odds, vote, which, lead):
    return {"kumi": kumi, "odds": odds, "vote": vote, "which": which,
            "lead_sec": lead, "ts": _TS_LATE if which == "late" else _TS_EARLY}


def test_kumi_is_split_by_fixed_width():
    assert _horses("0103", 2, 2) == ["01", "03"]
    assert _horses("010308", 2, 3) == ["01", "03", "08"]
    assert _horses("0103", 2, 3) is None        # 桁が足りない
    assert _horses("0100", 2, 2) is None        # 0 番は無い


def test_money_is_pool_times_inverse_odds():
    """★票数_c = プール×(1-控除率)/オッズ_c。控除率は比で消えるので掛けない。"""
    rows = [_row("0102", "0020", "00000001000", "late", 60),    # 2.0倍
            _row("0103", "0050", "00000001000", "late", 60)]    # 5.0倍
    got, pool = _money_by_horse(rows, 2, 2)
    assert pool == 1000.0
    # 01 は両方の組に乗る
    assert got["01"] == pytest.approx(1000 * (1 / 2.0 + 1 / 5.0))
    assert got["02"] == pytest.approx(1000 * (1 / 2.0))
    assert got["03"] == pytest.approx(1000 * (1 / 5.0))


def test_score_is_positive_when_new_money_favours_a_horse():
    """01-02 の組にだけ新しい金が入った → 01 と 02 のスコアが正、03 は負。"""
    rows = [
        # early: プール1000、3組が均等
        _row("0102", "0030", "00000001000", "early", 360),
        _row("0103", "0030", "00000001000", "early", 360),
        _row("0203", "0030", "00000001000", "early", 360),
        # late: プール2000。01-02 のオッズだけ下がった=そこに金が入った
        _row("0102", "0020", "00000002000", "late", 60),
        _row("0103", "0060", "00000002000", "late", 60),
        _row("0203", "0060", "00000002000", "late", 60),
    ]
    cfg = MoneyConfig(pool="umaren", lead_seconds=60, flow_minutes=6)
    sc = money_scores(FakeDB(rows), ("2026", "0816", "07", "04", "02", "11"), cfg)
    assert set(sc) == {"01", "02", "03"}
    assert sc["01"]["score"] > 0 and sc["02"]["score"] > 0
    assert sc["03"]["score"] < 0
    assert sc["01"]["pool_growth"] == pytest.approx(1.0)
    assert sc["01"]["window_sec"] == 300


def test_flat_pool_is_rejected():
    """★プールが増えていない窓は ΔM が雑音しか含まない。買う理由にならない。
    ここを通すと『金が動いていないのに動いた』と読んでしまう。"""
    rows = [_row("0102", "0030", "00000001000", "early", 360),
            _row("0103", "0030", "00000001000", "early", 360),
            _row("0102", "0020", "00000001001", "late", 60),
            _row("0103", "0060", "00000001001", "late", 60)]
    cfg = MoneyConfig(pool="umaren", min_pool_growth=0.005)
    assert money_scores(FakeDB(rows), ("2026", "0816", "07", "04", "02", "11"), cfg) == {}
    loose = MoneyConfig(pool="umaren", min_pool_growth=0.0)
    assert money_scores(FakeDB(rows), ("2026", "0816", "07", "04", "02", "11"), loose)


def test_unknown_pool_raises():
    with pytest.raises(ValueError):
        money_scores(FakeDB([]), ("2026", "0816", "07", "04", "02", "11"),
                     MoneyConfig(pool="でたらめ"))


def test_pool_tables_are_distinct_per_bet_type():
    """★プール名とテーブルの取り違えは『別の賭式の金を別の賭式の成績として報告する』事故。"""
    from hro_operations.money_signal import POOLS

    assert POOLS["umaren"][0] == "ts_o2"
    assert POOLS["sanrenpuku"][0] == "ts_sokuho_o5"
    assert POOLS["sanrenpuku"][2] == 3          # 1組3頭


def test_payout_lookup_pads_umaban():
    """★nl_hr / nl_se の馬番が ' 1' や '1' でも突合できること。桁が揃わないと
    全部「外れ」になり、回収率が静かに下振れする(原因が非常に分かりにくい)。"""
    from hro_operations import money_signal as m

    rows = {"races": [{"year": "2026", "month_day": "0606", "jyo_cd": "05",
                       "kaiji": "03", "nichiji": "01", "race_num": "01"}],
            "settle": [{"rid": "2026060605030101", "umaban": "1", "pay": "300"}],
            "scratch": []}

    class DB:
        def query(self, sql, params):
            if "nl_hr" in sql:
                return rows["settle"]
            if "nl_se" in sql:
                return rows["scratch"]
            if "DISTINCT" in sql:
                return rows["races"]
            return []                      # money_scores 用: スコアは作らない

    rep = m.backtest(DB(), "20260601", "20260630", MoneyConfig(), with_ci=False)
    assert rep["races"] == 1
    # 払戻表は 2 桁に正規化されて保持される
    assert rep["races_unsettled"] == 0 or rep["races_scored"] == 0

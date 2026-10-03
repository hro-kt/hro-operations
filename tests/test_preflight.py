"""発注前チェック。

★3プロセスのどれかが死んでいても静かに0件になる。2026-09-26 は設定が反映されて
  いないことに1日気付かず全レースを空振りした。走り出す前に見える形にする。
"""
from __future__ import annotations

from hro_operations.preflight import check
from hro_operations.race_day import DayConfig


class FakeDB:
    def __init__(self, races=1, nk=100, sok=100):
        self.races, self.nk, self.sok = races, nk, sok

    def query(self, sql, _params):
        if "nl_ra" in sql:
            return [{"n": self.races, "upcoming": self.races,
                     "first_post": "0950", "last_post": "1610"}]
        n = self.nk if "ts_netkeiba_o1" in sql else self.sok
        return [{"rows": n, "races": 1 if n else 0, "last_at": None, "age_sec": 5}]


def _cfg(**kw):
    base = dict(date="20260927", win_model="", place_model="", results_path="",
                strategy="flow", flow_source="netkeiba", flow_lead_seconds=75,
                flow_minutes=6, flow_thresholds={75: 0.1533},
                deadline_lead_seconds=60, lead_seconds=70)
    base.update(kw)
    return DayConfig(**base)


def test_all_green():
    r = check(FakeDB(), "20260927", _cfg())
    assert r["problems"] == []


def test_missing_sokuho_is_reported_as_fatal():
    """★netkeiba は単勝しか出さない。複勝オッズ(JV由来)が無いと全頭落ちて0件になる。"""
    r = check(FakeDB(sok=0), "20260927", _cfg())
    assert any("複勝オッズが無いと全頭落ちて0件" in p for p in r["problems"])


def test_missing_netkeiba_is_reported():
    r = check(FakeDB(nk=0), "20260927", _cfg())
    assert any("netkeiba-odds を起動" in p for p in r["problems"])


def test_threshold_key_mismatch_is_reported():
    """★2026-09-26 の実害。キーが合わないと全レース見送りになる。"""
    r = check(FakeDB(), "20260927", _cfg(flow_thresholds={120: 0.1631}))
    assert any("閾値のキーに 75 がありません" in p for p in r["problems"])


def test_lead_order_is_checked():
    r = check(FakeDB(), "20260927", _cfg(flow_lead_seconds=60))
    assert any("リードの大小が不正" in p for p in r["problems"])


def test_no_races_is_reported():
    r = check(FakeDB(races=0), "20260927", _cfg())
    assert any("nl_ra に" in p for p in r["problems"])


# -- 収集ウィンドウ前の「空」は正常 ---------------------------------------------- #
class _FakeDB:
    def __init__(self, mins_to_first):
        self._mins = mins_to_first

    def query(self, sql, _params):
        if "FROM nl_ra" in sql:
            return [{"n": 24, "upcoming": 24, "mins_to_first": self._mins,
                     "first_post": "0950", "last_post": "1630"}]
        return [{"rows": 0, "races": 0, "last": None}]   # オッズは空


def _cfg_nk():
    from hro_operations.race_day import DayConfig

    return DayConfig(date="20261004", win_model="w", place_model="p",
                     results_path="r.jsonl", flow_source="netkeiba",
                     flow_lead_seconds=90, flow_minutes=6,
                     flow_thresholds={90: 0.0254})


def test_empty_odds_before_the_collection_window_is_not_a_problem():
    """★開催日の朝、ジョブは動いているのに「発注できません」と出た(2026-10-04)。

    netkeiba-odds は発走20分以内のレースしか取りに行かない(--within-minutes 20)。
    第1レースが09:50なら、09:30より前に空なのは**正常**。
    これを ✗ として出すと、朝に無用な復旧作業をさせることになる。
    """
    from hro_operations.preflight import check

    out = check(_FakeDB(mins_to_first=240.0), "20261004", _cfg_nk())
    joined = " ".join(out["problems"])
    assert "netkeiba(ts_netkeiba_o1)が空です" not in joined
    assert "速報(ts_sokuho_o1)が空です" not in joined
    assert any("正常です" in n for n in out["notes"]), out["notes"]


def test_empty_odds_inside_the_window_is_still_a_problem():
    """ウィンドウに入っているのに空なら、それは本当に止まっている。"""
    from hro_operations.preflight import check

    out = check(_FakeDB(mins_to_first=5.0), "20261004", _cfg_nk())
    joined = " ".join(out["problems"])
    assert "netkeiba(ts_netkeiba_o1)が空です" in joined
    assert "速報(ts_sokuho_o1)が空です" in joined

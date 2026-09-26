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

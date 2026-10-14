"""収集が「生きているが何も採れていない」状態の検知。

★2026-10-10: netkeiba のセッションが切れてもプロセスは生きたままで、1レース
  1スナップしか返さない状態が9分続いた。ジョブは running なので既存の再投入は
  効かず、午前を失った。
★沈黙そのものは異常ではない。収集は「発走N分以内」しか見に行かないので、
  レースが遠い時間帯は空で正常。区別しないと誤報だらけになる。
"""

from __future__ import annotations

import pytest

from hro_operations import collector_health as ch


class _Conn:
    def __init__(self, mins, ages):
        self.mins, self.ages = mins, ages
        self._r = None

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        if "min(EXTRACT" in q:
            self._r = (self.mins,)
        else:
            # ★EXTRACT(EPOCH FROM …) が先に来るので、素朴な split では拾えない
            import re
            tbl = re.search(r"FROM (ts_\w+)", q).group(1)
            age = self.ages.get(tbl)
            self._r = (age, "2026-10-15 12:00:00") if age is not None else (None, None)
        return self

    def fetchone(self):
        return self._r


def _check(mins, nk, sk, **kw):
    return ch.check(_Conn(mins, {"ts_netkeiba_o1": nk, "ts_sokuho_o1": sk}),
                    "20261015", **kw)


def test_silence_is_normal_when_no_race_is_near():
    """★レースが遠い時間帯に空なのは正常。ここを誤報にすると誰も見なくなる。"""
    r = _check(45.0, None, None)
    assert r["in_window"] is False and r["silent"] == []
    assert "対象時間外" in ch.describe(r)


def test_silence_is_normal_when_racing_is_over():
    r = _check(None, 9999, 9999)
    assert r["in_window"] is False and r["silent"] == []


def test_silent_netkeiba_is_caught_when_a_race_is_near():
    """★本番で踏んだ状況。発走が近いのに信号が来ていない。"""
    r = _check(5.0, 600, 10)
    assert r["silent"] == ["netkeiba"]
    assert "信号(単勝)" in ch.describe(r) and "600秒前" in ch.describe(r)


def test_silent_sokuho_is_caught():
    """★複勝オッズが止まると全頭落ちて0件になる。"""
    r = _check(5.0, 10, 900)
    assert r["silent"] == ["sokuho"]


def test_no_data_at_all_counts_as_silent():
    r = _check(3.0, None, None)
    assert set(r["silent"]) == {"netkeiba", "sokuho"}
    assert "データなし" in ch.describe(r)


def test_fresh_data_is_not_flagged():
    r = _check(5.0, 20, 30)
    assert r["silent"] == [] and "動いています" in ch.describe(r)


def test_threshold_is_lenient_enough_for_change_only_saving():
    """★netkeiba は2秒周期だが**変化したときだけ保存する**。無風なら数十秒空く。

    短すぎる閾値にすると、正常な静けさを異常と呼ぶ。
    """
    assert ch.SILENT_SEC >= 120
    r = _check(5.0, 90, 90)
    assert r["silent"] == []


def test_window_is_inside_the_collectors_own_window():
    """★収集側の既定(netkeiba 10分 / run_odds 20分)より内側で測る。

    外側で測ると、まだ取りに行かない時間帯を異常と呼んでしまう。
    """
    assert ch.WINDOW_MIN <= 10


@pytest.mark.parametrize("src,kind", [("netkeiba", "netkeiba_odds"),
                                      ("sokuho", "run_odds")])
def test_job_kind_maps_to_the_collector(src, kind):
    assert ch.job_kind(src) == kind


def test_unknown_source_has_no_job():
    assert ch.job_kind("nope") is None


def test_a_broken_table_does_not_hide_the_other_source():
    class _Bad(_Conn):
        def execute(self, sql, params=()):
            if "ts_netkeiba_o1" in sql:
                raise RuntimeError("relation does not exist")
            return super().execute(sql, params)

    r = ch.check(_Bad(5.0, {"ts_sokuho_o1": 900}), "20261015")
    assert any(s.get("error") for s in r["sources"])
    assert r["silent"] == ["sokuho"]

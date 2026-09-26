"""複数ホライズンの flow。

★守りたいのは「同一レース・同一分位で比べていること」。ここが崩れると
  信号の差とレース構成の差が混ざり、読み違える(2026-09-23 に実際にやった)。
"""
from __future__ import annotations

import pytest

from hro_operations.horizons import HorizonConfig, evaluate, multi_scores


class FakeDB:
    def __init__(self, rows_by_kind):
        self.k = rows_by_kind

    def query(self, sql, params):
        if "nl_hr" in sql:
            return self.k.get("settle", [])
        if "nl_se" in sql:
            return self.k.get("scratch", [])
        if "SELECT DISTINCT t.year" in sql:
            return self.k.get("races", [])
        return self.k.get("multi", [])


def _row(cut, um, tan, fuku="0110", lead=None):
    return {"cut": cut, "umaban": um, "tan_odds": tan, "fuku_odds_low": fuku,
            "lead_sec": cut if lead is None else lead}


def test_multi_scores_needs_every_cutoff():
    """★1つでも欠けたら {} を返す。欠けたまま比べると土俵が揃わない。"""
    cfg = HorizonConfig(source="ts", lead_seconds=60, origins=[180, 360])
    key = ("2026", "0926", "06", "04", "08", "11")

    full = [_row(60, "01", "0020"), _row(60, "02", "0060"),
            _row(180, "01", "0030"), _row(180, "02", "0050"),
            _row(360, "01", "0040"), _row(360, "02", "0040")]
    sc = multi_scores(FakeDB({"multi": full}), key, cfg)
    assert set(sc) == {180, 360}
    # 01 はオッズが下がり続けている = シェアが上がる → 正、02 は負
    assert sc[180]["01"] > 0 > sc[180]["02"]
    assert sc[360]["01"] > sc[180]["01"]        # 長い窓ほど動きが大きい

    missing = [r for r in full if r["cut"] != 360]
    assert multi_scores(FakeDB({"multi": missing}), key, cfg) == {}


def test_effective_lead_outside_tolerance_is_rejected():
    """★格子に穴があると別の時点を見ることになり、尺度が変わって閾値が比較できない。"""
    cfg = HorizonConfig(source="ts", lead_seconds=60, origins=[180])
    key = ("2026", "0926", "06", "04", "08", "11")
    rows = [_row(60, "01", "0020"), _row(60, "02", "0060"),
            _row(180, "01", "0030", lead=400), _row(180, "02", "0050", lead=400)]
    assert multi_scores(FakeDB({"multi": rows}), key, cfg) == {}


def test_missing_fuku_drops_the_horse():
    """★複勝オッズは発注する券種。無い馬は買えないので候補から外す。"""
    cfg = HorizonConfig(source="ts", lead_seconds=60, origins=[180])
    key = ("2026", "0926", "06", "04", "08", "11")
    rows = [_row(60, "01", "0020", fuku=""), _row(60, "02", "0060"),
            _row(180, "01", "0030", fuku=""), _row(180, "02", "0050")]
    sc = multi_scores(FakeDB({"multi": rows}), key, cfg)
    assert set(sc[180]) == {"02"}


def test_netkeiba_odds_are_read_as_plain_multipliers():
    """★JV は '428'=42.8 の10倍整数、netkeiba は 42.8 そのもの。"""
    cfg = HorizonConfig(source="netkeiba", lead_seconds=75, origins=[360])
    key = ("2026", "0926", "06", "04", "08", "11")
    rows = [_row(75, "01", 2.0), _row(75, "02", 6.0),
            _row(360, "01", 3.0), _row(360, "02", 5.0)]
    sc = multi_scores(FakeDB({"multi": rows}), key, cfg)
    assert sc[360]["01"] > 0 > sc[360]["02"]


def test_evaluate_compares_on_the_same_races():
    """★全ホライズンで測れて決済済みのレースだけを土俵にすること。"""
    cfg = HorizonConfig(source="ts", lead_seconds=60, origins=[180, 360])
    races = [{"year": "2026", "month_day": "0926", "jyo_cd": "06",
              "kaiji": "04", "nichiji": "08", "race_num": "11"}]
    multi = [_row(60, "01", "0020"), _row(60, "02", "0060"),
             _row(180, "01", "0030"), _row(180, "02", "0050"),
             _row(360, "01", "0040"), _row(360, "02", "0040")]
    db = FakeDB({"races": races, "multi": multi,
                 "settle": [{"rid": "2026092606040811", "umaban": "01", "pay": "150"}],
                 "scratch": []})
    r = evaluate(db, "20260926", "20260926", cfg)
    assert r["races_used"] == 1 and r["unsettled"] == 0
    assert set(r["per_horizon"]) == {180, 360}
    assert r["all"]["bets"] <= min(r["per_horizon"][c]["bets"] for c in (180, 360))
    assert r["any"]["bets"] >= max(r["per_horizon"][c]["bets"] for c in (180, 360))


def test_evaluate_excludes_unsettled_races():
    """★未確定を「外れ」と数えると回収率が0に張り付く。"""
    cfg = HorizonConfig(source="ts", lead_seconds=60, origins=[180])
    races = [{"year": "2026", "month_day": "0926", "jyo_cd": "06",
              "kaiji": "04", "nichiji": "08", "race_num": "11"}]
    multi = [_row(60, "01", "0020"), _row(60, "02", "0060"),
             _row(180, "01", "0030"), _row(180, "02", "0050")]
    r = evaluate(FakeDB({"races": races, "multi": multi, "settle": [], "scratch": []}),
                 "20260926", "20260926", cfg)
    assert r["races_used"] == 0 and r["unsettled"] == 1

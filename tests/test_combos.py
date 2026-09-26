"""組み合わせ券。

★ワイドは1レースに3組当たる。組ごとに判定しないと、買っていない組で当たり扱いになる。
"""
from __future__ import annotations

import pytest

from hro_operations.combos import evaluate


class FakeDB:
    def __init__(self, pays):
        self.pays = pays

    def query(self, _sql, params):
        return [p for p in self.pays if p["bt"] == params["bt"]]


def _row(rid, um, flow, ninki=8):
    return {"rid": rid, "umaban": um, "flow": flow, "ninki": ninki}


def _pay(rid, kumi, pay, bt="wide"):
    return {"rid": rid, "kumi": kumi, "pay": pay, "bt": bt}


def test_wide_is_settled_per_combination():
    """★ワイドは 01-02 / 01-03 / 02-03 の3組が当たる。買った組と照合すること。"""
    rows = [_row("R1", "01", 0.5), _row("R1", "05", 0.4)]
    db = FakeDB([_pay("R1", "0102", "300"), _pay("R1", "0103", "400"),
                 _pay("R1", "0203", "500")])
    r = evaluate(db, rows, "20260101", "20260131", "wide", threshold=0.3, mode="all")
    assert r["bets"] == 1 and r["hits"] == 0      # 01-05 は当たっていない

    rows2 = [_row("R1", "01", 0.5), _row("R1", "02", 0.4)]
    r2 = evaluate(db, rows2, "20260101", "20260131", "wide", threshold=0.3, mode="all")
    assert r2["bets"] == 1 and r2["hits"] == 1 and r2["returned"] == 300


def test_races_without_enough_picks_are_skipped():
    """1頭しか候補が無いレースは組が作れない。分母にも入れない。"""
    rows = [_row("R1", "01", 0.5)]
    r = evaluate(FakeDB([_pay("R1", "0102", "300")]), rows, "20260101", "20260131",
                 "wide", threshold=0.3, mode="all")
    assert r["bets"] == 0 and r["races_with_combo"] == 0 and r["races_seen"] == 1


def test_unsettled_races_are_excluded():
    """★払戻が無いレースは未確定。外れとして数えると回収率が沈む。"""
    rows = [_row("R1", "01", 0.5), _row("R1", "02", 0.4)]
    r = evaluate(FakeDB([]), rows, "20260101", "20260131", "wide", threshold=0.3,
                 mode="all")
    assert r["bets"] == 0 and r["races_with_combo"] == 0


def test_max_combos_limits_points_per_race():
    """★点数が増えると1点あたりの期待値が薄まる。上位から打ち切れること。"""
    rows = [_row("R1", f"0{i}", 0.9 - i * 0.1) for i in range(1, 5)]
    db = FakeDB([_pay("R1", "0102", "300")])
    r = evaluate(db, rows, "20260101", "20260131", "wide", threshold=0.3,
                 max_combos=2, mode="all")
    assert r["bets"] == 2          # C(4,2)=6 だが2組で打ち切り
    assert r["hits"] == 1          # スコア上位の 01-02 は含まれる


def test_ninki_band_applies_before_combining():
    rows = [_row("R1", "01", 0.5, ninki=3), _row("R1", "02", 0.4, ninki=8),
            _row("R1", "03", 0.35, ninki=9)]
    db = FakeDB([_pay("R1", "0203", "500")])
    r = evaluate(db, rows, "20260101", "20260131", "wide", threshold=0.3,
                 min_ninki=7, mode="all")
    assert r["bets"] == 1 and r["hits"] == 1      # 人気3番の01は除外され 02-03 になる


def test_unknown_bet_type_raises():
    with pytest.raises(ValueError):
        evaluate(FakeDB([]), [], "20260101", "20260131", "でたらめ", threshold=0.0)


def test_partners_mode_does_not_require_both_to_be_candidates():
    """★エッジは**馬ごと**に存在する。相手まで候補である必要はない。
    候補どうし(all)は「両方が候補」を要求するので対象レースが激減する
    (全帯で1,794中531レース)。軸×人気上位なら候補が1頭いれば組める。"""
    rows = [_row("R1", "01", 0.5, ninki=9),
            _row("R1", "02", 0.0, ninki=1), _row("R1", "03", 0.0, ninki=2),
            _row("R1", "04", 0.0, ninki=3)]
    db = FakeDB([_pay("R1", "0102", "800")])
    part = evaluate(db, rows, "1", "2", "wide", threshold=0.3, partners=3)
    assert part["bets"] == 3 and part["hits"] == 1      # 01-02 / 01-03 / 01-04
    both = evaluate(db, rows, "1", "2", "wide", threshold=0.3, mode="all")
    assert both["bets"] == 0                            # 候補が1頭なので組めない


def test_partners_excludes_the_axis_itself():
    """軸が人気上位にも入っている場合、自分自身と組まないこと。"""
    rows = [_row("R1", "01", 0.5, ninki=1), _row("R1", "02", 0.0, ninki=2),
            _row("R1", "03", 0.0, ninki=3)]
    r = evaluate(FakeDB([_pay("R1", "0102", "300")]), rows, "1", "2", "wide",
                 threshold=0.3, partners=3)
    assert r["bets"] == 2                               # 01-02 / 01-03 のみ


def test_duplicate_combos_are_not_double_counted():
    """★軸が2頭いると同じ組が2度作られる。重複して買うと点数が水増しされる。"""
    rows = [_row("R1", "01", 0.5, ninki=8), _row("R1", "02", 0.45, ninki=9),
            _row("R1", "03", 0.0, ninki=1)]
    r = evaluate(FakeDB([_pay("R1", "0103", "300")]), rows, "1", "2", "wide",
                 threshold=0.3, partners=1, max_combos=10)
    # 軸01×相手03 と 軸02×相手03 で 0103 / 0203 の2組(重複なし)
    assert r["bets"] == 2

"""DM/TM 評価。

★向きの取り違えが一番危ない。DM は**予想走破タイムなので小さいほど強い**、
  TM は**予測スコアなので大きいほど強い**。符号を間違えると相関の符号が反転し、
  「終盤の金は DM と逆に動く」という正反対の結論になる。
"""
from __future__ import annotations

from hro_operations.mining import MiningConfig, evaluate


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def query(self, _sql, _params):
        return self.rows


def _row(um, t1, t0, dm, tm, fin, pay=None):
    """t1/t0 は JV の10倍整数文字列。"""
    return {"rid": "2026092606040811", "umaban": um, "t1": t1, "t0": t0,
            "dm_time": dm, "tm_score": tm, "finish": fin, "i_jyo_cd": "",
            "fuku_pay": pay}


def test_dm_is_lower_is_better_and_tm_is_higher_is_better():
    """DM の予想タイムが速い馬ほど着順が良い標本では、DM↔着順 が**正**になること。"""
    rows = [
        # 予想タイム 1125→1130 の順に強く、着順もその順。TM は逆向き(大きいほど強い)
        _row("01", "0020", "0030", "11250", "0900", "1", "150"),
        _row("02", "0040", "0045", "11260", "0800", "2", "180"),
        _row("03", "0060", "0055", "11270", "0700", "3", "220"),
        _row("04", "0100", "0090", "11280", "0600", "4"),
        _row("05", "0200", "0180", "11290", "0500", "5"),
    ]
    r = evaluate(FakeDB(rows), "20260926", "20260926", MiningConfig())
    assert r["races_used"] == 1
    assert r["rho"]["dm_finish"] > 0.9      # 予想が当たっている
    assert r["rho"]["tm_finish"] > 0.9      # 符号反転が効いている
    assert r["rho"]["mkt_finish"] > 0.9     # 単勝オッズも小さいほど強い


def test_flow_direction_is_comparable_with_dm():
    """★flow は大きいほど強いので着順・DM と向きを合わせて符号反転している。
    ここを間違えると「終盤の金は DM と逆に動く」という正反対の結論になる。"""
    rows = [
        # 01 はオッズが下がった(買われた=flow 正)かつ DM 最速
        _row("01", "0020", "0040", "11250", "0900", "1", "150"),
        _row("02", "0050", "0045", "11260", "0800", "2", "180"),
        _row("03", "0070", "0060", "11270", "0700", "3", "220"),
        _row("04", "0120", "0100", "11280", "0600", "4"),
        _row("05", "0250", "0200", "11290", "0500", "5"),
    ]
    r = evaluate(FakeDB(rows), "20260926", "20260926", MiningConfig())
    assert r["rho"]["dm_flow"] > 0          # DM が速い馬に金が向かっている


def test_races_with_too_few_usable_horses_are_skipped():
    """★5頭未満は順位相関が意味を持たない(欠損で数頭に落ちた race を混ぜない)。"""
    rows = [_row("01", "0020", "0030", "11250", "0900", "1"),
            _row("02", "0040", "0045", "11260", "0800", "2")]
    r = evaluate(FakeDB(rows), "20260926", "20260926", MiningConfig())
    assert r["races_used"] == 0


def test_missing_dm_or_finish_drops_the_horse():
    rows = [_row("01", "0020", "0030", None, "0900", "1"),
            _row("02", "0040", "0045", "11260", "0800", "2"),
            _row("03", "0060", "0055", "11270", "0700", None),
            _row("04", "0100", "0090", "11280", "0600", "4"),
            _row("05", "0200", "0180", "11290", "0500", "5"),
            _row("06", "0300", "0280", "11300", "0400", "6")]
    r = evaluate(FakeDB(rows), "20260926", "20260926", MiningConfig())
    # 6頭中2頭が欠損 → 4頭では 5頭未満なので使わない
    assert r["races_used"] == 0


def test_decompose_splits_flow_into_dm_part_and_residual():
    """★分解は flow を過不足なく分ける(平均 + DM説明分 + 残差 = flow)。
    ここがずれると「どちらが効いているか」の判定そのものが嘘になる。"""
    from hro_operations.mining import decompose_race

    use = [{"dm": 11250, "flow": 0.5}, {"dm": 11260, "flow": 0.2},
           {"dm": 11270, "flow": -0.1}, {"dm": 11280, "flow": -0.2},
           {"dm": 11290, "flow": -0.4}]
    out = decompose_race(use)
    mean = sum(x["flow"] for x in use) / len(use)
    for x in out:
        assert abs(mean + x["dm_part"] + x["resid"] - x["flow"]) < 1e-9
    # ★DM は小さいほど強い。最速の馬の dm_part が最大になること(符号反転が効いている)
    assert out[0]["dm_part"] == max(x["dm_part"] for x in out)


def test_decompose_needs_variation_in_dm():
    """全馬の DM が同じなら分解できない(ゼロ除算/無意味な残差を避ける)。"""
    from hro_operations.mining import decompose_race

    use = [{"dm": 11250, "flow": 0.5}, {"dm": 11250, "flow": 0.2},
           {"dm": 11250, "flow": -0.1}]
    assert decompose_race(use) is None

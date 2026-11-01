"""同一レースで閾値を超えた頭数の下限。

★2026-11-02 実測(20260801-1031 / 単勝・人気7+・ts@T-60s・閾値+0.1255、270点):
    1頭だけ 135点 0.5904 / 2頭 70点 1.4314 / 3頭 57点 2.4509 / 4頭+ 8点 4.5750
  購入の**半分**が「1頭だけ」の帯に入り、そこが 0.59 で引いている。

★向きに注意。「1頭に金が集中しているほど良い(独走度)」の**逆**。
  直感と逆なので、2窓で確かめるまで live の設定には出さない。
★固定閾値だと「該当頭数が多い = よく動いたレース」になりやすく、
  値動き総量(race_move)と交絡しうる。必ず nsig と move を一緒に見ること。
"""

from __future__ import annotations

import pytest

from hro_operations.flow_signal import FlowConfig, apply_min_nsig, apply_quantile


def _rows(rids):
    details = [{"rid": r, "score": float(i)} for i, r in enumerate(rids)]
    bets = [(r, 100, 0, False) for r in rids]
    return details, bets


def test_a_race_with_a_single_pick_is_dropped():
    """★ここが肝。購入の半分を占める 0.59 の帯を外す。"""
    d, b = _rows(["A", "B", "B"])
    kd, kb = apply_min_nsig(d, b, 2)
    assert [x["rid"] for x in kd] == ["B", "B"]
    assert [x[0] for x in kb] == ["B", "B"]


def test_three_or_more_can_be_demanded():
    d, b = _rows(["A", "B", "B", "C", "C", "C"])
    kd, _ = apply_min_nsig(d, b, 3)
    assert {x["rid"] for x in kd} == {"C"}


def test_zero_and_one_change_nothing():
    """★既定(0)で従来の挙動。1 も「1頭以上」なので同じ。"""
    d, b = _rows(["A", "B", "B"])
    for n in (0, 1):
        assert apply_min_nsig(d, b, n) == (d, b)


def test_details_and_bets_stay_aligned():
    """★details[i] と bets[i] は同じ1点。ずれると別の馬の払戻を数える。"""
    d = [{"rid": "A", "score": 1.0}, {"rid": "B", "score": 2.0},
         {"rid": "B", "score": 3.0}]
    b = [("A", 100, 999, False), ("B", 100, 11, False), ("B", 100, 22, False)]
    kd, kb = apply_min_nsig(d, b, 2)
    assert [x["score"] for x in kd] == [2.0, 3.0]
    assert [x[2] for x in kb] == [11, 22]


def test_it_counts_what_was_selected_not_the_population():
    """★「選ばれた後」に数える。閾値で切る前に数えると母集団の頭数になる。
       分位で切る場合も、切った後の残り数が該当頭数そのもの。
    """
    d = [{"rid": "A", "score": s} for s in (9.0, 1.0, 1.0)] + \
        [{"rid": "B", "score": s} for s in (8.0, 7.0, 1.0)]
    b = [(x["rid"], 100, 0, False) for x in d]
    # 上位50% で切ると A は1頭、B は2頭残る
    kd, kb, _ = apply_quantile(d, b, 0.5)
    kd, kb = apply_min_nsig(kd, kb, 2)
    assert {x["rid"] for x in kd} == {"B"}


def test_empty_input_is_not_a_crash():
    assert apply_min_nsig([], [], 2) == ([], [])


def test_the_default_keeps_the_current_behaviour():
    assert FlowConfig().min_nsig == 0


# --- live 側も同じ規則で見送ること ------------------------------------------ #
def test_the_live_path_skips_the_race_and_says_why():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "flow_signal.py").read_text(encoding="utf-8")
    i = src.index("cfg.min_nsig > 1 and len(orders) < cfg.min_nsig")
    body = src[i:i + 1200]
    assert "レースごと見送ります" in body
    assert '"rule": "min_nsig"' in body, "判断ログに理由が残らない"
    assert "return []" in body


def test_the_live_skip_is_checked_before_the_per_race_cap():
    """★max_per_race で絞った後に数えると、上限のせいで見送ることになる。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "flow_signal.py").read_text(encoding="utf-8")
    assert (src.index("cfg.min_nsig > 1 and len(orders)")
            < src.index("cfg.max_per_race > 0 and len(orders)"))

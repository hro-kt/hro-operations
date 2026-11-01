"""スコアを t 統計量にする(票の薄さで割る)。

★動機: 人気薄ほどオッズが動きやすい。しかしそれは情報が入ったからとは限らず、
  **票が薄くて揺れている**だけでもそうなる。シェアの推定誤差は
      sd(log share_i) ≒ 1/√(その馬の票数)
  なので、同じ flow=+0.5 が人気2番なら 3σ、人気12番なら 0.8σ になる。
  同じ数字が別の意味を持ったまま、1本の閾値で切っていた。

★「率に変える」では直らない。flow_tan は既に率だから:
      logit差 = log(p1/p0) − log((1−p1)/(1−p0))
  人気薄では第2項がほぼ0なので log(相対変化) そのもの。
"""

from __future__ import annotations

import math

import pytest

from hro_operations.flow_signal import (FlowConfig, _logit, _percentile,
                                        apply_quantile, flow_scores)


# --- 前提の確認(これが崩れたら設計の根拠が消える) -------------------------- #
def test_flow_is_already_a_rate_for_longshots():
    """★人気薄では logit 差 = 相対変化の対数。だから率への置き換えは効かない。"""
    p0, p1 = 0.02, 0.022                      # +10%
    assert _logit(p1) - _logit(p0) == pytest.approx(math.log(1.1), abs=0.003)


def test_the_logit_favours_favourites_for_the_same_relative_move():
    """★人気薄が有利になっているわけではない。同じ率なら人気馬の方が高く出る
       ((1−p) の項)。だから「率にすれば公平」も成り立たない。"""
    rel = 1.1
    longshot = _logit(0.02 * rel) - _logit(0.02)
    favourite = _logit(0.40 * rel) - _logit(0.40)
    assert favourite > longshot


# --- t 統計量 --------------------------------------------------------------- #
def _t(flow: float, p0: float, p1: float, n: float) -> float:
    return flow / math.sqrt((1.0 / p0 + 1.0 / p1) / n)


def test_the_same_flow_means_less_on_a_thin_horse():
    """★ここが肝。同じ +0.5 でも、票が薄い馬の方が t は小さい。"""
    assert _t(0.5, 0.02, 0.022, 100_000) < _t(0.5, 0.20, 0.22, 100_000)


def test_a_bigger_pool_makes_the_same_move_more_meaningful():
    assert _t(0.5, 0.02, 0.022, 1_000_000) > _t(0.5, 0.02, 0.022, 100_000)


def test_the_statistic_keeps_the_sign():
    """★向きは変えない。資金が抜けた馬は負のまま。"""
    assert _t(-0.5, 0.02, 0.018, 100_000) < 0


# --- live では使わせない ---------------------------------------------------- #
def test_live_refuses_the_backtest_only_score():
    """★締切前に確定票数(nl_h1)は存在しない。黙って flow として計算すると、
       «検証したものと違う信号で発注する» ことになる。
    ★DB に触る前に落ちること(db=None を渡しても ValueError になる)。"""
    with pytest.raises(ValueError, match="live では使えません"):
        flow_scores(None, ("2026", "1024", "08", "4", "8", "12"),
                    FlowConfig(score="tstat"))


def test_the_default_score_is_unchanged():
    assert FlowConfig().score == "flow"


# --- 分位で切る ------------------------------------------------------------- #
def _d(score):
    return [{"score": s} for s in score]


def test_quantile_cuts_the_population_after_the_bands():
    details = _d([1.0, 2.0, 3.0, 4.0, 5.0])
    bets = [(f"r{i}", 100, 0, False) for i in range(5)]
    kept, kb, thr = apply_quantile(details, bets, 0.6)
    assert thr == pytest.approx(3.4)
    assert [d["score"] for d in kept] == [4.0, 5.0]
    assert len(kb) == 2


def test_quantile_keeps_details_and_bets_aligned():
    """★details[i] と bets[i] は同じ1点。ずれると別の馬の払戻を数える。"""
    details = _d([5.0, 1.0, 4.0, 2.0])
    bets = [("a", 100, 500, False), ("b", 100, 0, False),
            ("c", 100, 300, False), ("d", 100, 0, False)]
    kept, kb, _ = apply_quantile(details, bets, 0.5)
    assert [b[0] for b in kb] == ["a", "c"]
    assert [d["score"] for d in kept] == [5.0, 4.0]


def test_quantile_on_an_empty_population_is_not_a_crash():
    assert apply_quantile([], [], 0.95) == ([], [], float("inf"))


def test_percentile_interpolates():
    assert _percentile([0.0, 10.0], 0.5) == pytest.approx(5.0)
    assert _percentile([1.0, 2.0, 3.0], 1.0) == 3.0
    assert _percentile([1.0, 2.0, 3.0], 0.0) == 1.0

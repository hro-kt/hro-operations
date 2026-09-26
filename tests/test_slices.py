"""スライス集計。

★守りたいのは「本数が少ない帯に CI を出さない」こと。少数の帯に CI を付けると、
  たまたま良い帯を「強い」と読んでしまう。
"""
from __future__ import annotations

import pytest

from hro_operations.slices import _bucket_n, _bucket_odds, slice_details


def _d(rid, tan, payout, note="外れ", n=16, score=0.4):
    return {"rid": rid, "umaban": "01", "score": score, "lead": 60, "tan": tan,
            "fuku": 2.0, "n_horses": n, "amount": 100, "payout": payout, "note": note}


def test_buckets():
    assert _bucket_odds(1.8).startswith("1")
    assert _bucket_odds(3.0).startswith("2")
    assert _bucket_odds(100.0).startswith("7")
    assert _bucket_n(8).startswith("1")
    assert _bucket_n(18).startswith("4")


def test_slice_groups_and_computes_roi():
    det = [_d(f"2026092606{i:06d}", 3.0, 200, "的中") for i in range(5)]
    det += [_d(f"2026092609{i:06d}", 30.0, 0) for i in range(5)]
    out = {r["name"]: r for r in slice_details(det, "tan", min_bets=1)}
    assert out["2 2.0-4.0"]["roi"] == 2.0
    assert out["6 20-40"]["roi"] == 0.0


def test_ci_is_omitted_for_thin_buckets():
    """★本数が少ない帯に CI を出さない。出すと『この帯は強い』と誤読する。"""
    det = [_d(f"2026092606{i:06d}", 3.0, 200, "的中") for i in range(3)]
    out = slice_details(det, "tan", min_bets=30)
    assert out[0]["bets"] == 3 and out[0].get("ci") is None


def test_refund_is_not_counted_as_a_hit():
    """★返還は元金が戻るだけ。的中に数えると的中率が水増しされる。"""
    det = [_d("2026092606040811", 3.0, 100, "返還")]
    out = slice_details(det, "tan", min_bets=1)
    assert out[0]["roi"] == 1.0 and out[0]["hits"] == 0


def test_unknown_axis_raises():
    with pytest.raises(ValueError):
        slice_details([_d("2026092606040811", 3.0, 0)], "でたらめ")

from hro_operations.slices import slice_details


def _d(rid, fav_tan, fav_flow, payout):
    return {"rid": rid, "umaban": "01", "score": 0.1, "fav_tan": fav_tan,
            "fav_flow": fav_flow, "amount": 100, "payout": payout,
            "note": "的中" if payout else "外れ"}


def test_fav_axis_buckets_by_favorite_odds():
    ds = [_d("r1", 1.3, 0.1, 0), _d("r2", 1.3, 0.1, 0), _d("r3", 4.0, -0.1, 2000),
          _d("r4", None, None, 0)]
    out = {r["name"]: r for r in slice_details(ds, "fav", min_bets=99)}
    assert out["1 〜1.5倍"]["bets"] == 2 and out["1 〜1.5倍"]["roi"] == 0
    assert out["5 3.5倍〜(混戦)"]["roi"] == 20.0
    assert "0 不明" in out


def test_favflow_axis_sign():
    ds = [_d("r1", 1.3, 0.2, 0), _d("r2", 3.0, -0.2, 0)]
    names = {r["name"] for r in slice_details(ds, "favflow", min_bets=99)}
    assert names == {"4 人気馬に金が入った(>=+0.05)", "1 人気馬から金が抜けた(<-0.05)"}

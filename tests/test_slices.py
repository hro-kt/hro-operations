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


def test_zogen_bucket_uses_the_sign_field():
    """★増減の符号は zogen_fugo(+/-)にあり、zogen_sa は絶対値。符号を無視すると
    増と減が同じ帯に落ち、効果が打ち消し合って見えなくなる。"""
    from hro_operations.slices import _bucket_zogen

    assert _bucket_zogen({"zogen_fugo": "-", "zogen_sa": "012"}).startswith("1")
    assert _bucket_zogen({"zogen_fugo": "-", "zogen_sa": "004"}).startswith("2")
    assert _bucket_zogen({"zogen_fugo": "+", "zogen_sa": "000"}).startswith("3")
    assert _bucket_zogen({"zogen_fugo": "+", "zogen_sa": "004"}).startswith("4")
    assert _bucket_zogen({"zogen_fugo": "+", "zogen_sa": "012"}).startswith("5")
    assert _bucket_zogen({"zogen_sa": ""}).startswith("0")


def test_other_buckets():
    from hro_operations.slices import (_bucket_kyori, _bucket_ninki,
                                       _bucket_track, _bucket_waku)

    assert _bucket_ninki({"ninki": "01"}).startswith("1")
    assert _bucket_ninki({"ninki": "12"}).startswith("4")
    assert _bucket_ninki({"ninki": ""}).startswith("0")
    assert _bucket_waku({"waku": "3"}) == "3 3枠"
    assert _bucket_kyori({"kyori": "1200"}).startswith("1")
    assert _bucket_kyori({"kyori": "2400"}).startswith("4")
    assert _bucket_track({"track_cd": "11"}) == "1 芝"
    assert _bucket_track({"track_cd": "23"}) == "2 ダート"
    assert _bucket_track({"track_cd": "51"}).startswith("3")


def test_race_level_axes():
    """★レース単位の軸。「レースを見送れる方が強い」ことは分かっているが、
    どのレースを見送るべきかは未検証。信号が1頭に集中しているレースと
    散らばっているレースを分けて見る。"""
    det = [_d("R1", 3.0, 200, "的中", score=0.5),
           _d("R1", 8.0, 0, score=0.4),
           _d("R1", 15.0, 0, score=0.35),
           _d("R2", 5.0, 300, "的中", score=0.6)]
    ns = {r["name"]: r for r in slice_details(det, "nsig", min_bets=1)}
    assert ns["3 同レース3"]["bets"] == 3          # R1 は3頭
    assert ns["1 同レース1"]["bets"] == 1          # R2 は1頭
    top = {r["name"]: r for r in slice_details(det, "toponly", min_bets=1)}
    assert top["1 レース最高スコア"]["bets"] == 2   # 各レースの最高が1頭ずつ
    assert top["2 それ以外"]["bets"] == 2


def test_move_axis_measures_how_much_the_market_moved():
    """★「そのレースで市場がどれだけ動いたか」で切る。ほとんど動いていないレースの
    flow は雑音のはず、という仮説を測るための軸。絶対値の尺度は窓・信号源で変わるので
    四分位で切る。"""
    det = [_d(f"R{i}", 5.0, 150 if i % 2 else 0, "的中" if i % 2 else "外れ")
           for i in range(1, 9)]
    for i, d in enumerate(det, 1):
        d["race_move"] = 0.1 * i
    out = {r["name"]: r for r in slice_details(det, "move", min_bets=1)}
    assert len(out) == 4 and all(r["bets"] == 2 for r in out.values())

    # race_move が無い明細でも落ちない(0 として扱う)
    for d in det:
        d.pop("race_move")
    assert slice_details(det, "move", min_bets=1)


def test_raceno_axis():
    det = [_d("2026092706040901", 5.0, 0), _d("2026092706040912", 5.0, 0)]
    out = {r["name"]: r for r in slice_details(det, "raceno", min_bets=1)}
    assert set(out) == {"01R", "12R"}


def test_public_news_axes():
    """★終盤の資金移動が「情報を持った金」なのか「公開ニュース(騎手変更・馬場変更)への
    反応」なのかで、除外すべきか狙うべきかが逆になる。"""
    det = [_d("R1", 5.0, 0), _d("R2", 5.0, 0)]
    det[0].update(has_jc=True, has_cc=False, has_we=True)
    det[1].update(has_jc=False, has_cc=False, has_we=False)
    for ax, expect in (("jc", {"1 変更あり", "2 なし"}),
                       ("cc", {"2 なし"}),
                       ("we", {"1 変更あり", "2 なし"})):
        got = {r["name"] for r in slice_details(det, ax, min_bets=1)}
        assert got == expect, (ax, got)


def test_chokyo_axis_ranks_within_the_race():
    """★坂路時計の絶対値は時期・馬場・トレセンで動く。同一レース内の順位で見る。
    ★美浦/栗東の坂路のみなので、ウッドチップだけの馬や外国馬は行が無い。
    「データなし」も1つの帯として残す(それ自体が情報でありうる)。"""
    from hro_operations.slices import _bucket_chokyo

    assert _bucket_chokyo({"chokyo_rank": 1, "chokyo_n": 10}).startswith("1")
    assert _bucket_chokyo({"chokyo_rank": 5, "chokyo_n": 10}).startswith("2")
    assert _bucket_chokyo({"chokyo_rank": 9, "chokyo_n": 10}).startswith("3")
    assert _bucket_chokyo({"chokyo_rank": None, "chokyo_n": 10}).startswith("0")
    assert _bucket_chokyo({}).startswith("0")

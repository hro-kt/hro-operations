"""flow を特徴量にしたモデル。

★守りたいのは (1) リークを作らないこと (2) 目的関数が期待回収率であること。
  複勝の的中確率をそのまま最大化すると人気馬を選ぶだけになる。
"""
from __future__ import annotations

from hro_operations.model_flow import FEATURES, ModelConfig, load_rows


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def query(self, _sql, _params):
        return self.rows


def _row(um, t1, t0, pay, **kw):
    base = {"rid": "2026092606040811", "ymd": "20260926", "umaban": um,
            "t1": t1, "t0": t0, "f1": "0015", "ht1": "09261544", "ht0": "09261539",
            "lead1": 60, "pay": pay, "i_jyo_cd": "", "has_payout": True,
            "wakuban": "3", "zogen_fugo": "+", "zogen_sa": "004", "ba_taijyu": "480",
            "kyori": "1600", "track_cd": "11", "grade_cd": "", "nin1": "1"}
    base.update(kw)
    return base


def test_features_exclude_post_race_data():
    """★nl_se 由来(馬体重)は RACE蓄積でレース後配信。live で使えないので特徴量に入れない。
    ここが崩れると、検証では効くのに実戦で再現しないモデルができる。"""
    for banned in ("zogen", "ba_taijyu", "finish", "kakutei", "payout", "pay", "hit"):
        assert not any(banned in f for f in FEATURES), f"{banned} が特徴量に入っている"


def test_load_rows_builds_features_and_target():
    rows = [_row("01", "0020", "0030", "150"), _row("02", "0060", "0050", ""),
            _row("03", "0080", "0070", ""), _row("04", "0100", "0090", ""),
            _row("05", "0200", "0180", "")]
    out = load_rows(FakeDB(rows), "20260926", "20260926", ModelConfig())
    assert len(out) == 5
    d = {r["umaban"]: r for r in out}
    assert d["01"]["hit"] == 1 and d["02"]["hit"] == 0
    assert d["01"]["ninki"] == 1 and d["05"]["ninki"] == 5
    assert d["01"]["flow"] > 0            # オッズが下がった=買われた
    assert all(f in out[0] for f in FEATURES)


def test_refunds_are_excluded_from_training():
    """★返還(出走取消/除外)は勝ちでも負けでもない。学習に混ぜると外れとして学ぶ。"""
    rows = [_row("01", "0020", "0030", "150"), _row("02", "0060", "0050", ""),
            _row("03", "0080", "0070", ""), _row("04", "0100", "0090", ""),
            _row("05", "0200", "0180", "", i_jyo_cd="1")]
    out = load_rows(FakeDB(rows), "20260926", "20260926", ModelConfig())
    assert {r["umaban"] for r in out} == {"01", "02", "03", "04"}


def test_unsettled_and_degenerate_races_are_dropped():
    """★未確定レースを外れとして学ぶと、回収率が構造的に沈む。
    決定時点と起点が同じスナップのレースも flow が測れていないので使わない。"""
    base = [_row("01", "0020", "0030", "150"), _row("02", "0060", "0050", ""),
            _row("03", "0080", "0070", ""), _row("04", "0100", "0090", ""),
            _row("05", "0200", "0180", "")]
    unsettled = [{**r, "has_payout": False} for r in base]
    assert load_rows(FakeDB(unsettled), "20260926", "20260926", ModelConfig()) == []
    same = [{**r, "ht0": r["ht1"]} for r in base]
    assert load_rows(FakeDB(same), "20260926", "20260926", ModelConfig()) == []


def test_market_offset_is_on_by_default():
    """★複勝圏内の確率をそのまま学習させると、モデルは市場を再現することに容量を
    使い切り、市場とのズレ=誤差を買いに行く(2026-09-25 実測で 0.84 対 flow 1.13 の完敗)。
    市場の logit を init_score に置き、補正だけを学ばせるのが既定。"""
    assert ModelConfig().market_offset is True
    assert 0 < ModelConfig().place_takeout < 1


def test_default_target_is_net_return():
    """★確率を当てて p×オッズ で並べると、高配当馬で誤差がオッズ倍されて増幅する
    (実測 0.8316 / 的中12.7%)。買う基準そのもの=純収益を直接回帰する。
    払戻は裾が重いので刈り込みを持つ。"""
    c = ModelConfig()
    assert c.target == "return"
    assert c.winsor > 0

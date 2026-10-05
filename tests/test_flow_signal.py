"""flow_signal のスコア計算(DB を使わない部分)のテスト。"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from hro_operations.flow_signal import FlowConfig, flow_orders, flow_scores

RACE = ("2026", "0919", "06", "04", "05", "11")


class FakeDB:
    """db.query(sql, params) -> list[dict] だけを模す。"""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query(self, sql, params):
        self.calls.append((sql, params))
        return self.rows


# 実データでは late(決定時点)と early(起点)は別スナップ。同じ時刻を与えると
# 「flow が測れていない」扱いで除外されるため、既定を which ごとに分ける。
_TS_LATE = datetime(2026, 9, 19, 15, 43, tzinfo=timezone.utc)
_TS_EARLY = datetime(2026, 9, 19, 15, 37, tzinfo=timezone.utc)


_UNSET = object()      # ts=None(=DBのNULL)を明示したいテストと区別する


def _row(um, tan, fuku, which, ts=_UNSET):
    if ts is _UNSET:
        ts = _TS_LATE if which == "late" else _TS_EARLY
    return {"umaban": um, "tan_odds": tan, "fuku_odds_low": fuku, "ts": ts, "which": which}


def test_score_is_logit_difference_of_win_pool_share():
    # 2頭。late で 01 のオッズが下がる(=人気が集まる)→ 01 のスコアは正。
    rows = [_row("01", "20", "15", "late"), _row("02", "30", "20", "late"),
            _row("01", "30", "15", "early"), _row("02", "30", "20", "early")]
    sc = flow_scores(FakeDB(rows), RACE, FlowConfig())
    assert set(sc) == {"01", "02"}
    assert sc["01"]["score"] > 0 > sc["02"]["score"]
    # 手計算と一致すること(オッズは10倍整数文字列 → 2.0 / 3.0)
    s_late, s_early = 1 / 2.0 + 1 / 3.0, 1 / 3.0 + 1 / 3.0
    def logit(p):
        return math.log(p / (1 - p))
    assert sc["01"]["score"] == logit((1 / 2.0) / s_late) - logit((1 / 3.0) / s_early)


def test_missing_snapshot_yields_no_scores():
    assert flow_scores(FakeDB([_row("01", "20", "15", "late")]), RACE, FlowConfig()) == {}
    assert flow_scores(FakeDB([]), RACE, FlowConfig()) == {}


def test_invalid_odds_rows_are_skipped():
    """オッズが '0000'(発売前/取消)の馬は候補にしない。"""
    rows = [_row("01", "20", "15", "late"), _row("02", "0000", "0000", "late"),
            _row("01", "30", "15", "early"), _row("02", "0000", "0000", "early")]
    sc = flow_scores(FakeDB(rows), RACE, FlowConfig())
    assert set(sc) == {"01"}


def test_orders_only_above_threshold_and_under_max_odds():
    rows = [_row("01", "20", "15", "late"), _row("02", "30", "9999", "late"),
            _row("01", "30", "15", "early"), _row("02", "30", "9999", "early")]
    db = FakeDB(rows)
    orders = flow_orders(db, RACE, FlowConfig(threshold=0.0), 100, "flow_tan")
    assert [o.selection_id for o in orders] == ["01"]        # 02 はスコア負で除外
    o = orders[0]
    assert o.bet_type == "place" and o.amount == 100 and o.race_id == "2026091906040511"
    assert o.odds == 1.5 and "flow_tan=" in o.reason

    high = flow_orders(db, RACE, FlowConfig(threshold=99.0), 100, "flow_tan")
    assert high == []                                        # 閾値超えなし

    capped = flow_orders(db, RACE, FlowConfig(threshold=0.0, max_odds=1.2), 100, "flow_tan")
    assert capped == []                                      # オッズ上限で除外


def test_reason_survives_missing_timestamp():
    """ts が NULL でも理由文の整形で落ちない(発注を止めない)。"""
    rows = [_row("01", "20", "15", "late", ts=None), _row("02", "30", "20", "late", ts=None),
            _row("01", "30", "15", "early", ts=None), _row("02", "30", "20", "early", ts=None)]
    orders = flow_orders(FakeDB(rows), RACE, FlowConfig(threshold=0.0), 100, "flow_tan")
    assert [o.selection_id for o in orders] == ["01"]
    assert "late=None" in orders[0].reason


def test_flow_diagnose_collects_post_snapshots_and_scores():
    """発注が出ない理由(発走時刻・スナップ・スコア)を1回で切り分けられること。"""
    from hro_operations.flow_signal import flow_diagnose

    class DiagDB:
        def query(self, sql, params):
            # スコア用SQLにも ra CTE(FROM nl_ra)が入るので、判定順を間違えない
            if "UNION ALL" in sql:
                return [_row("01", "20", "15", "late"), _row("02", "30", "20", "late"),
                        _row("01", "30", "15", "early"), _row("02", "30", "20", "early")]
            if "count(*)" in sql:
                return [{"rows": 100, "snaps": 50, "horses": 2,
                         "first_ts": "a", "last_ts": "b"}]
            return [{"hasso_time": "1510", "post": "2026-09-19T15:10:00+09:00"}]

    d = flow_diagnose(DiagDB(), RACE, FlowConfig(threshold=0.0))
    assert d["post"]["hasso_time"] == "1510"
    assert d["snapshots"]["snaps"] == 50
    assert set(d["scores"]) == {"01", "02"}
    assert d["n_above"] == 1           # 01 だけが閾値超え


def test_flow_coverage_separates_snapshot_time_from_fetch_time():
    """「T-60秒のスナップが在る」と「締切前に取り込めていた」は別物。両方見えること。"""
    from datetime import datetime, timedelta, timezone

    from hro_operations.flow_signal import flow_coverage

    post = datetime(2026, 9, 19, 15, 4, tzinfo=timezone.utc)

    class CovDB:
        def query(self, sql, params):
            return [
                # 間に合っている: T-60s のスナップを T-120s に取得
                {"jyo_cd": "06", "race_num": "10", "hasso_time": "1504", "post": post,
                 "snaps": 300, "late_ts": post - timedelta(seconds=60),
                 "early_ts": post - timedelta(minutes=6),
                 "late_fetched_at": post - timedelta(seconds=120)},
                # 遅れている: 取り込みが決定時点の後(検証はできるが発注には使えない)
                {"jyo_cd": "09", "race_num": "11", "hasso_time": "1545", "post": post,
                 "snaps": 300, "late_ts": post - timedelta(seconds=60),
                 "early_ts": None,
                 "late_fetched_at": post - timedelta(seconds=10)},
                # スナップ自体が無い
                {"jyo_cd": "09", "race_num": "12", "hasso_time": "1620", "post": post,
                 "snaps": 0, "late_ts": None, "early_ts": None, "late_fetched_at": None},
            ]

    rows = flow_coverage(CovDB(), "20260919", FlowConfig())
    assert rows[0]["late_lead_sec"] == 60 and rows[0]["fetch_margin_sec"] == 60
    assert rows[0]["has_early"] is True
    assert rows[1]["fetch_margin_sec"] == -50      # 決定時点より後に取り込んだ
    assert rows[1]["has_early"] is False
    assert rows[2]["late_lead_sec"] is None and rows[2]["fetch_margin_sec"] is None


def test_lead_scan_measures_agreement_with_reference():
    """決定時点を早めたときに同じ馬を選べるかを、順位相関・傾き・重なりで測る。"""
    from hro_operations.flow_signal import lead_scan

    class ScanDB:
        """基準(lead=60)と対象(lead=90)で少しだけ違うスコアになるよう返す。"""

        def query(self, sql, params):
            if "UNION ALL" not in sql:
                return [{"hasso_time": "1450", "post": None}]
            late_tan = "20" if params["lead"] == 60 else "21"
            return [_row("01", late_tan, "15", "late"), _row("02", "30", "20", "late"),
                    _row("03", "40", "30", "late"),
                    _row("01", "30", "15", "early"), _row("02", "30", "20", "early"),
                    _row("03", "40", "30", "early")]

    rows = lead_scan(ScanDB(), [RACE], [60, 90], FlowConfig(threshold=0.0))
    assert [r["lead"] for r in rows] == [60, 90]
    assert abs(rows[0]["rho"] - 1.0) < 1e-9 and abs(rows[0]["slope"] - 1.0) < 1e-9  # 基準と同一
    assert rows[0]["jaccard"] == 1.0
    assert rows[1]["races"] == 1 and rows[1]["rho"] is not None


def test_threshold_from_uses_upper_quantile_of_scores():
    """決定時点や信号源を変えると尺度が変わるので、同じ条件で閾値を取り直す。"""
    from hro_operations.flow_signal import threshold_from

    class ThrDB:
        def query(self, sql, params):
            if "UNION ALL" not in sql:
                return [{"hasso_time": "1450", "post": None}]
            return [_row("01", "20", "15", "late"), _row("02", "30", "20", "late"),
                    _row("01", "30", "15", "early"), _row("02", "30", "20", "early")]

    res = threshold_from(ThrDB(), [RACE] * 5, FlowConfig(), quantile=0.5)
    assert res["races"] == 5 and res["n"] == 10
    assert res["threshold"] is not None and res["n_above"] == 5


def test_usable_snapshot_reports_what_is_in_hand_at_decision_time():
    """締切直前に判断するとき、実際に手元にある最新スナップと、
    検証と同じスナップが間に合ったかを分けて出す。"""
    from datetime import datetime, timedelta, timezone

    from hro_operations.flow_signal import usable_snapshot

    post = datetime(2026, 9, 19, 5, 50, tzinfo=timezone.utc)

    class UsableDB:
        def query(self, sql, params):
            return [
                # 60秒前のスナップが判断時刻(発走70秒前)の5秒前に届いた → 使える
                {"jyo_cd": "06", "race_num": "10", "hasso_time": "1450", "post": post,
                 "usable_ts": post - timedelta(seconds=60),
                 "want_seen": post - timedelta(seconds=75)},
                # 60秒前のスナップは判断時刻より後に届いた → 使えない(最新は120秒前)
                {"jyo_cd": "09", "race_num": "11", "hasso_time": "1530", "post": post,
                 "usable_ts": post - timedelta(seconds=120),
                 "want_seen": post - timedelta(seconds=55)},
            ]

    rows = usable_snapshot(UsableDB(), "20260919", FlowConfig(), margin_seconds=10)
    assert rows[0]["usable_lead_sec"] == 60 and rows[0]["want_margin_sec"] == 5
    assert rows[1]["usable_lead_sec"] == 120 and rows[1]["want_margin_sec"] == -15


def test_flow_scores_rejects_same_snapshot_for_decision_and_baseline():
    """決定時点と起点が同じスナップなら「測れていない」として何も返さない。

    ★0 を返すと「資金が動かなかった(score=0)」と見分けが付かず、threshold_from の
    分位点が構造的ゼロで薄まって閾値が実際より低く出る。発走直前のスナップが
    取れていない日が混ざると起きる(2026-09-20 の速報がまさにこれだった)。
    """
    same = "2026-09-20 11:41:00+09:00"
    rows = []
    for um, odds in (("01", "0030"), ("02", "0050"), ("03", "0070")):
        for which in ("late", "early"):
            rows.append({"umaban": um, "tan_odds": odds, "fuku_odds_low": "0015",
                         "ts": same, "which": which})

    class _DB:
        def query(self, sql, params):
            return rows

    cfg = FlowConfig(lead_seconds=120, flow_minutes=6, source="sokuho")
    assert flow_scores(_DB(), ("2026", "0920", "09", "04", "06", "12"), cfg) == {}


def test_flow_scores_still_works_when_snapshots_differ():
    """別スナップなら通常どおりスコアを返す(上の除外が効きすぎていないこと)。"""
    rows = []
    for um, late_o, early_o in (("01", "0030", "0035"), ("02", "0050", "0048"),
                                ("03", "0070", "0069")):
        rows.append({"umaban": um, "tan_odds": late_o, "fuku_odds_low": "0015",
                     "ts": "2026-09-20 16:08:00+09:00", "which": "late"})
        rows.append({"umaban": um, "tan_odds": early_o, "fuku_odds_low": "0015",
                     "ts": "2026-09-20 16:04:00+09:00", "which": "early"})

    class _DB:
        def query(self, sql, params):
            return rows

    cfg = FlowConfig(lead_seconds=120, flow_minutes=6, source="sokuho")
    out = flow_scores(_DB(), ("2026", "0920", "09", "04", "06", "12"), cfg)
    assert set(out) == {"01", "02", "03"}
    assert out["01"]["score"] > 0        # 単勝が縮んだ=シェアが増えた
    assert out["03"]["score"] < 0


# --------------------------------------------------------------------------- #
# flow-backtest(モデルも候補CSVも使わない回収率)
# --------------------------------------------------------------------------- #
def _bt_row(rid, um, t1, t0, pay, ts1="16:08", ts0="16:04"):
    return {"rid": rid, "ymd": "20260919", "umaban": um, "t1": t1, "f1": "0015",
            "t0": t0, "ht1": ts1, "ht0": ts0, "pay": pay}


def test_backtest_settles_on_confirmed_place_payout():
    """判断はスナップのオッズ、決済は確定複勝(nl_hr.pay)で行う。

    ★パリミュチュエルなので判断時のオッズでは払われない。ここを取り違えると
    回収率が実際より良く出る。
    """
    from hro_operations.flow_signal import backtest

    rows = [_bt_row("R1", "01", "0020", "0030", "180"),      # 単勝が縮む→スコア正→買う
            _bt_row("R1", "02", "0030", "0030", None),
            _bt_row("R1", "03", "0070", "0060", None),
            _bt_row("R2", "01", "0020", "0030", None),       # 買うが外れ
            _bt_row("R2", "02", "0030", "0030", None),
            _bt_row("R2", "03", "0070", "0060", None)]
    r = backtest(FakeDB(rows), "20260919", "20260919",
                 FlowConfig(source="ts", threshold=0.0), amount=100)
    assert r["bets"] == 2 and r["hits"] == 1
    assert r["staked"] == 200 and r["returned"] == 180
    assert abs(r["roi"] - 0.9) < 1e-9


def test_backtest_excludes_races_where_flow_was_not_measurable():
    """決定時点と起点が同じスナップのレースは購入せず、件数だけ報告する。"""
    from hro_operations.flow_signal import backtest

    rows = [_bt_row("R1", "01", "0020", "0030", "180"),
            _bt_row("R1", "02", "0070", "0060", None),
            _bt_row("R3", "01", "0020", "0030", "999", "16:04", "16:04")]
    r = backtest(FakeDB(rows), "20260919", "20260919",
                 FlowConfig(source="ts", threshold=0.0), amount=100)
    assert r["races_degenerate"] == 1 and r["races_scored"] == 1
    assert r["returned"] == 180            # R3 の 999 は混ざらない


def test_backtest_max_odds_filters_longshots():
    from hro_operations.flow_signal import backtest

    rows = [_bt_row("R1", "01", "0900", "1200", "5000"),     # 単勝90.0倍
            _bt_row("R1", "02", "0070", "0060", None)]
    cfg = FlowConfig(source="ts", threshold=0.0)
    assert backtest(FakeDB(rows), "20260919", "20260919", cfg)["bets"] == 1
    assert backtest(FakeDB(rows), "20260919", "20260919", cfg, max_odds=50.0)["bets"] == 0


def test_bootstrap_resamples_whole_races_not_horses():
    """同一レース内の馬は独立でないので、レースごと丸ごと抜き差しする。"""
    from hro_operations.flow_signal import _bootstrap_roi

    # 1レースだけなら、何度抽出しても同じレースしか出ない=CIは点になる
    one = _bootstrap_roi([("R1", 100, 300), ("R1", 100, 0)], n_boot=200)
    assert one["lo"] == one["hi"] == 1.5
    two = _bootstrap_roi([("R1", 100, 300), ("R2", 100, 0)], n_boot=500)
    assert two["lo"] < two["hi"]           # レースが2つあれば幅が出る


def test_month_chunks_cover_range_without_gaps_or_overlap():
    """期間を暦月で切る。文字列比較なので '0231' のような非実在日を上端に使ってよい
    (SQL 側も year||month_day の文字列 BETWEEN で絞るため)。"""
    from hro_operations.__main__ import _month_chunks

    ch = _month_chunks("20260101", "20260920")
    assert ch[0] == ("20260101", "20260131") and ch[-1] == ("20260901", "20260920")
    assert len(ch) == 9
    # 隣り合う区間が重ならない(重なると同じレースを二重計上する)
    for (_, b), (a2, _) in zip(ch, ch[1:]):
        assert b < a2
    # 月内に収まる範囲は分割されない
    assert _month_chunks("20260115", "20260120") == [("20260115", "20260120")]


def test_backtest_treats_scratched_horses_as_refund_not_loss():
    """★出走取消/発走除外/競走除外 は**返還**。払戻表に行が立たないので、
    異常区分を見ないと全損として数えてしまい回収率が不当に下がる。"""
    from hro_operations.flow_signal import backtest

    rows = [
        {**_bt_row("R1", "01", "0020", "0030", None), "i_jyo_cd": "1"},   # 出走取消→返還
        {**_bt_row("R1", "02", "0070", "0060", None), "i_jyo_cd": "0"},
        {**_bt_row("R2", "01", "0020", "0030", None), "i_jyo_cd": "4"},   # 競走中止→外れ
        {**_bt_row("R2", "02", "0070", "0060", None), "i_jyo_cd": "0"},
    ]
    r = backtest(FakeDB(rows), "20260921", "20260921",
                 FlowConfig(source="sokuho", threshold=0.0), amount=100)
    assert r["bets"] == 2 and r["refunds"] == 1
    assert r["staked"] == 200 and r["returned"] == 100      # 返還100 + 外れ0
    assert r["hits"] == 0                                   # 返還は的中に数えない
    assert r["hit_rate"] == 0.0                             # 分母も返還を除く(1件)


# --------------------------------------------------------------------------- #
# リード別の閾値(配信遅れで決定時点がレースごとに変わる)
# --------------------------------------------------------------------------- #
def _lead_row(um, tan, fuku, which, lead):
    r = _row(um, tan, fuku, which)
    r["lead_sec"] = lead
    return r


def test_threshold_is_chosen_by_the_lead_actually_used():
    """★配信遅れで T-120s になったり T-180s になったりする(2026-09-21 実測で41%/59%)。
    スコアの尺度もリードで変わる(ts@60 比の傾き 0.454 / 0.252)ので、単一の閾値だと
    片方でほぼ0件になる。しかも**黙って0件**になるのが最悪。"""
    from hro_operations.flow_signal import threshold_for

    cfg = FlowConfig(thresholds={120: 0.16, 180: 0.08})
    assert threshold_for(cfg, 120) == 0.16
    assert threshold_for(cfg, 180) == 0.08
    # 実測リードは分格子なので 60 の倍数に丸める(117秒→120)
    assert threshold_for(cfg, 117) == 0.16
    # 未設定のリードは見送る(近い値を流用すると尺度がずれた閾値で買うことになる)
    assert threshold_for(cfg, 240) is None
    # thresholds 未指定なら従来どおり単一閾値
    assert threshold_for(FlowConfig(threshold=0.2802), 120) == 0.2802


def test_flow_orders_uses_per_lead_threshold():
    rows = [_lead_row("01", "20", "15", "late", 180), _lead_row("02", "30", "20", "late", 180),
            _lead_row("01", "30", "15", "early", 540), _lead_row("02", "30", "20", "early", 540)]
    # 01 のスコアは約 +0.18。T-180s の閾値 0.08 なら買うが、T-120s の 0.30 では買わない
    buy = flow_orders(FakeDB(rows), RACE,
                      FlowConfig(thresholds={120: 0.30, 180: 0.08}), 100, "flow")
    assert [o.selection_id for o in buy] == ["01"]
    assert "@T-180s" in buy[0].reason          # どのリードで判定したか追える

    skip = flow_orders(FakeDB(rows), RACE,
                       FlowConfig(thresholds={120: 0.30}), 100, "flow")
    assert skip == []                          # T-180s の閾値が無いので見送り


def test_backtest_details_record_the_lead_actually_used():
    """本数が少ない日は集計値より1点ずつの中身を見たい。実測リードも添える
    (配信遅れで当日それが手元にあったとは限らないので、突き合わせに要る)。"""
    from hro_operations.flow_signal import backtest

    rows = [{**_bt_row("R1", "01", "0020", "0030", "180"), "lead1": 120},
            {**_bt_row("R1", "02", "0070", "0060", None), "lead1": 120},
            {**_bt_row("R2", "01", "0020", "0030", None), "lead1": 180}]
    r = backtest(FakeDB(rows), "20260921", "20260921",
                 FlowConfig(source="sokuho", threshold=0.0), amount=100)
    det = {d["rid"]: d for d in r["details"]}
    assert det["R1"]["umaban"] == "01" and det["R1"]["payout"] == 180
    assert det["R1"]["note"] == "的中" and det["R2"]["note"] == "外れ"
    assert det["R1"]["lead"] == 120 and det["R2"]["lead"] == 180


def test_month_chunked_run_keeps_bet_details():
    """★CLI は期間を月で割って合算する。明細を引き継がないと、購入件数だけ出て
    明細が空になる(2026-09-21 に実際に起きた)。"""
    import inspect

    from hro_operations import __main__ as m

    src = inspect.getsource(m._cmd_flow_backtest)
    assert 'details += part.get("details")' in src
    assert 'r["details"] = details' in src


def test_backtest_excludes_races_whose_results_are_not_loaded_yet():
    """★払戻が1行も無いレースは「未確定」であって「全部外れ」ではない。

    2026-09-21 に実際に起きた: 開催当日は払戻(nl_hr)がまだ配信されていないのに
    6点すべてを外れとして数え、回収率 0.0000 と表示していた。
    """
    from hro_operations.flow_signal import backtest

    settled = [{**_bt_row("R1", "01", "0020", "0030", "180"), "has_payout": True},
               {**_bt_row("R1", "02", "0070", "0060", None), "has_payout": True}]
    pending = [{**_bt_row("R2", "01", "0020", "0030", None), "has_payout": False},
               {**_bt_row("R2", "02", "0070", "0060", None), "has_payout": False}]
    r = backtest(FakeDB(settled + pending), "20260921", "20260921",
                 FlowConfig(source="sokuho", threshold=0.0), amount=100)
    assert r["races_unsettled"] == 1
    assert r["bets"] == 1 and r["returned"] == 180      # R2 は買ったことにしない
    assert r["roi"] == 1.8


def test_race_day_imports_without_the_model_stack():
    """★flow はモデルを使わないのに race_day が冒頭で hro_backtest(LightGBM)を
    読み込んでいたため、モデル一式を入れていない機械(IPAT を叩く Windows)で
    ModuleNotFoundError になり、model-free のはずの戦略が動かせなかった。"""
    import inspect

    from hro_operations import race_day

    src = inspect.getsource(race_day)
    head = src[:src.index("def ")]
    assert "from hro_backtest import harness" not in head, "冒頭で読んではいけない"
    # 使う場所では遅延 import されていること
    assert src.count("from hro_backtest import harness") == 2


# --------------------------------------------------------------------------- #
# 時刻の整合(リードは「発走の何秒前」= 大きいほど早い)
# --------------------------------------------------------------------------- #
def _day_cfg(**kw):
    from hro_operations.race_day import DayConfig

    base = dict(date="20260922", win_model="", place_model="", results_path="",
                strategy="flow", deadline_lead_seconds=60)
    return DayConfig(**{**base, **kw})


def test_check_timing_accepts_the_real_operating_configuration():
    """★実運用の設定 (決定 発走-120s / 投票開始 発走-70s / 締切 発走-60s) を通すこと。

    以前ここを >= で書いており、**正しい設定のほうを弾いていた**(2026-09-22 に
    live 投入が止まった)。リードは大きいほど早い時刻なので flow_lead > lead > deadline。
    """
    from hro_operations.race_day import check_timing

    check_timing(_day_cfg(flow_lead_seconds=120, lead_seconds=70))


def test_check_timing_rejects_snapshot_later_than_the_vote():
    """スナップが投票時刻より後(= まだ存在しない)なら止める。"""
    import pytest

    from hro_operations.race_day import TimingError, check_timing

    with pytest.raises(TimingError, match="まだ存在しません"):
        check_timing(_day_cfg(flow_lead_seconds=60, lead_seconds=70))


def test_check_timing_rejects_voting_after_the_deadline():
    """投票開始が締切以降なら、全件が締切超過で捨てられるので止める。"""
    import pytest

    from hro_operations.race_day import TimingError, check_timing

    with pytest.raises(TimingError, match="締切"):
        check_timing(_day_cfg(flow_lead_seconds=120, lead_seconds=50))


# --------------------------------------------------------------------------- #
# 実効窓(起点リード − 決定リード)の一致
# --------------------------------------------------------------------------- #
def _win_row(um, tan, fuku, which, lead):
    r = _row(um, tan, fuku, which)
    r["lead_sec"] = lead
    return r


def test_flow_orders_skips_races_whose_window_differs_from_intent():
    """★格子に穴があると実効窓が意図とずれ、窓が長いほどスコアが大きく出る。
    日によって窓が違うと絶対閾値が比較できない。

    2026-09-22 の実害: ポーリングが82秒周期だった日(窓が長い)で取った閾値0.1631を、
    窓が正しく短い日に当てて候補0になった。
    """
    cfg = FlowConfig(lead_seconds=120, flow_minutes=6, threshold=0.0)   # 想定窓 240s
    ok = [_win_row("01", "20", "15", "late", 120), _win_row("02", "30", "20", "late", 120),
          _win_row("01", "30", "15", "early", 360), _win_row("02", "30", "20", "early", 360)]
    assert [o.selection_id for o in flow_orders(FakeDB(ok), RACE, cfg, 100, "flow")] == ["01"]

    # 起点が 600s にずれた(実効窓 480s = 想定240s から +240s)→ 見送り
    skew = [_win_row("01", "20", "15", "late", 120), _win_row("02", "30", "20", "late", 120),
            _win_row("01", "30", "15", "early", 600), _win_row("02", "30", "20", "early", 600)]
    assert flow_orders(FakeDB(skew), RACE, cfg, 100, "flow") == []

    # 1格子(60秒)のずれは許容(既定 window_tolerance_sec=60)
    near = [_win_row("01", "20", "15", "late", 120), _win_row("02", "30", "20", "late", 120),
            _win_row("01", "30", "15", "early", 420), _win_row("02", "30", "20", "early", 420)]
    assert [o.selection_id for o in flow_orders(FakeDB(near), RACE, cfg, 100, "flow")] == ["01"]


def test_netkeiba_compare_sql_joins_on_the_same_announcement_minute():
    """★秒単位の動きが本物か(公式の分更新の補間か)を見分けるための突き合わせ。

    同じ「発表分」で両者の値を比べる。補間なら分境界の間を単調に動くだけで、
    公式には無い値が出ない。一致率と「分内で何種類の値が出たか」で判定する。
    """
    import re

    import pglast

    from hro_operations.flow_signal import _SQL_NK_COMPARE

    pglast.parse_sql(re.sub(r"%\((\w+)\)s", r"$1", _SQL_NK_COMPARE))
    # 公式は10倍整数、netkeiba は倍。単位を揃えずに比べると全件不一致になる
    assert "tan_odds::numeric / 10.0" in _SQL_NK_COMPARE
    # 分キーで結合する(観測時刻そのものでは一致しない)
    assert "minute_key" in _SQL_NK_COMPARE


# --- netkeiba を信号源にする(締切直前まで判断時点を寄せるため) --------------- #
def _nk_row(um, tan, fuku, which, lead):
    """netkeiba 由来の行。単勝は **NUMERIC のそのままの倍率**(JV の10倍整数ではない)。"""
    ts = _TS_LATE if which == "late" else _TS_EARLY
    return {"umaban": um, "tan_odds": tan, "fuku_odds_low": fuku, "ts": ts,
            "lead_sec": lead, "which": which}


def test_netkeiba_odds_are_read_as_plain_multipliers():
    """★JV は '428'=42.8 の10倍整数、netkeiba は 42.8 そのもの。同じ関数で読むと
    isdigit が False で全頭 None になり『スナップショット不足』に化ける。"""
    from hro_operations.flow_signal import _num, _num_plain

    assert _num("428") == 42.8
    assert _num_plain(42.8) == 42.8
    assert _num_plain("42.8") == 42.8
    assert _num_plain(0) is None
    assert _num_plain(None) is None
    assert _num("42.8") is None          # 10倍整数として読むと壊れることの明示


def test_flow_scores_with_netkeiba_source():
    from hro_operations.flow_signal import FlowConfig, flow_scores

    # late で 01 が売れて(オッズ低下)、02 が売れ残る
    rows = [_nk_row("01", 2.0, "0110", "late", 75), _nk_row("02", 6.0, "0220", "late", 75),
            _nk_row("01", 3.0, "0110", "early", 360), _nk_row("02", 5.0, "0220", "early", 360)]
    cfg = FlowConfig(source="netkeiba", lead_seconds=75, flow_minutes=6)
    sc = flow_scores(FakeDB(rows), ("2026", "0926", "06", "04", "08", "11"), cfg)
    assert set(sc) == {"01", "02"}
    assert sc["01"]["score"] > 0 > sc["02"]["score"]
    assert sc["01"]["fuku_odds"] == 11.0          # 複勝は JV 由来(10倍整数)のまま
    assert sc["01"]["window_sec"] == 285          # 360-75


def test_netkeiba_threshold_is_not_snapped_to_the_60s_grid():
    """★netkeiba は実時刻基準でリードが 75 秒などになる。60秒格子に丸めると
    存在しないキー(60/120)を引いて**黙って見送る**。"""
    from hro_operations.flow_signal import FlowConfig, threshold_for

    cfg = FlowConfig(source="netkeiba", lead_seconds=75, thresholds={75: 0.12})
    assert threshold_for(cfg, 76) == 0.12
    grid = FlowConfig(source="sokuho", lead_seconds=120, thresholds={120: 0.15})
    assert threshold_for(grid, 118) == 0.15       # 格子ソースは従来どおり丸める


def test_backtest_does_not_silently_read_another_source():
    """★--flow-source netkeiba で ts_sokuho_o1 を読むと、別ソースの数字を
    netkeiba の成績として報告することになる。"""
    import re

    import pglast

    from hro_operations.flow_signal import _SQL_BT_NK, _SQL_NETKEIBA

    for q in (_SQL_NETKEIBA, _SQL_BT_NK):
        built = q.replace("{BET}", "'fuku'")      # 券種は差し込んでから検査する
        pglast.parse_sql(re.sub(r"%\((\w+)\)s", r"$1", built))
        assert "ts_netkeiba_o1" in q
    assert "observed_at" in _SQL_BT_NK and "hasso_time <=" not in _SQL_BT_NK


def test_missing_fuku_odds_is_named_as_the_cause(caplog):
    """★複勝オッズは常に JV-Link(ts_sokuho_o1)由来。netkeiba は単勝しか出さないので、
    速報ポーリングが止まると全頭落ちて「スコア算出不可」に見える。
    原因を名指ししないと、動いている netkeiba 側を触って1日溶かす。"""
    import logging

    from hro_operations.flow_signal import FlowConfig, flow_scores

    rows = [_nk_row("01", 2.0, None, "late", 75), _nk_row("02", 6.0, None, "late", 75),
            _nk_row("01", 3.0, None, "early", 360), _nk_row("02", 5.0, None, "early", 360)]
    cfg = FlowConfig(source="netkeiba", lead_seconds=75, flow_minutes=6)
    with caplog.at_level(logging.WARNING):
        assert flow_scores(FakeDB(rows), ("2026", "0926", "06", "04", "08", "11"), cfg) == {}
    assert "複勝オッズ" in caplog.text and "poll-odds" in caplog.text


def test_diagnostics_do_not_read_another_source_table():
    """★netkeiba を指定したのに JV 用の診断SQLを流用すると、**JV のスナップ格子**を
    表示する。2026-09-26 に実害: netkeiba は30秒刻みなのに「0s,60s,120s…」と出て、
    診断を信じると原因の切り分けを誤る。"""
    import re

    import pglast

    from hro_operations.flow_signal import (
        _SQL_DIAG_NETKEIBA,
        _SQL_DIAG_SOKUHO,
        _SQL_DIAG_TS,
        _SQL_GRID_NETKEIBA,
    )

    for q in (_SQL_DIAG_NETKEIBA, _SQL_GRID_NETKEIBA):
        pglast.parse_sql(re.sub(r"%\((\w+)\)s", r"$1", q))
        assert "ts_netkeiba_o1" in q and "observed_at" in q
        assert "ts_sokuho_o1" not in q and "FROM ts_o1" not in q
    assert "ts_netkeiba_o1" not in _SQL_DIAG_TS
    assert "ts_netkeiba_o1" not in _SQL_DIAG_SOKUHO


def test_netkeiba_compare_sql_columns_match_the_keys_python_reads():
    """★SQL が日本語の列名を返していて、Python 側の r["n"] が KeyError になった
    (2026-09-26)。列名とキーの対応をテストで固定する。"""
    import re

    import pglast

    from hro_operations.flow_signal import _SQL_NK_COMPARE, netkeiba_compare

    pglast.parse_sql(re.sub(r"%\((\w+)\)s", r"$1", _SQL_NK_COMPARE))
    needed = ["jyo_cd", "race_num", "minute_key", "n", "nk_snaps", "horses",
              "nk_pairs", "jv_values", "agree", "first_at", "last_at"]
    for col in needed:
        assert re.search(rf"\bAS {col}\b", _SQL_NK_COMPARE) or f"nk.{col}" in _SQL_NK_COMPARE, col

    class DB:
        def query(self, _sql, _params):
            return [dict.fromkeys(needed, 1)]

    assert set(netkeiba_compare(DB(), "20260926")[0]) == set(needed)


def test_tan_odds_band_filters_orders():
    """★回収率はオッズ帯で大きく違う(2026-09-25 実測, 3期間):
    2.0-4.0倍 1.038/1.025/1.046 / 20-40倍 1.584/1.255/1.566 / 40倍超 0.546/0.903/2.617。
    帯で絞れること、帯未指定なら従来どおり全通しであることを固定する。"""
    from hro_operations.flow_signal import FlowConfig, _in_tan_band

    band = FlowConfig(min_tan_odds=20.0, max_tan_odds=40.0)
    assert not _in_tan_band(band, 19.9)
    assert _in_tan_band(band, 20.0)
    assert _in_tan_band(band, 40.0)
    assert not _in_tan_band(band, 40.1)

    # 帯未指定なら全通し(既存の挙動を変えない)
    off = FlowConfig()
    assert _in_tan_band(off, 1.1) and _in_tan_band(off, 999.0)
    assert _in_tan_band(off, None)
    # 帯を指定したのにオッズが取れない馬は買わない(黙って通すと帯の意味が無くなる)
    assert not _in_tan_band(band, None)


def test_order_reason_records_the_win_odds():
    """★回収率がオッズ帯で大きく違う(20-40倍 1.45 / 全帯 1.08)。決定時点の単勝オッズを
    残さないと、ライブの結果を帯別に評価できない。odds 欄は発注する複勝の値なので別に持つ。"""
    from hro_operations.flow_signal import FlowConfig, flow_orders

    rows = [_row("01", "0200", "0035", "late"), _row("02", "0600", "0090", "late"),
            _row("01", "0300", "0035", "early"), _row("02", "0500", "0090", "early")]
    cfg = FlowConfig(source="sokuho", lead_seconds=60, flow_minutes=6, threshold=0.0)
    orders = flow_orders(FakeDB(rows), ("2026", "0926", "06", "04", "08", "11"),
                         cfg, 100, "test")
    assert orders, "候補が1件も出ていない"
    o = orders[0]
    assert "tan=20.0" in o.reason and "fuku=3.5" in o.reason
    assert o.odds == 3.5                 # 発注は複勝


def test_ninki_is_derived_from_win_odds_not_the_db_column():
    """★人気は決定時点の単勝オッズ順から導出する。DB の tan_ninki は信号源によって
    有無が違い(netkeiba は持たない)、そのままだとバックテストとライブで別定義になる。"""
    from hro_operations.flow_signal import FlowConfig, _in_ninki_band, assign_ninki, flow_scores

    sc = {"03": {"tan_odds": 5.0}, "01": {"tan_odds": 2.0}, "02": {"tan_odds": 5.0}}
    assign_ninki(sc)
    assert sc["01"]["ninki"] == 1
    assert sc["02"]["ninki"] == 2       # 同値は馬番順
    assert sc["03"]["ninki"] == 3

    rows = [_row("01", "0200", "0035", "late"), _row("02", "0600", "0090", "late"),
            _row("01", "0300", "0035", "early"), _row("02", "0500", "0090", "early")]
    got = flow_scores(FakeDB(rows), ("2026", "0926", "06", "04", "08", "11"),
                      FlowConfig(source="sokuho", lead_seconds=60, flow_minutes=6))
    assert got["01"]["ninki"] == 1 and got["02"]["ninki"] == 2

    band = FlowConfig(min_ninki=7, max_ninki=10)
    assert not _in_ninki_band(band, 6)
    assert _in_ninki_band(band, 7) and _in_ninki_band(band, 10)
    assert not _in_ninki_band(band, 11)
    assert not _in_ninki_band(band, None)      # 帯指定時に不明なら買わない
    assert _in_ninki_band(FlowConfig(), None)  # 帯未指定なら従来どおり


def test_bet_type_switches_the_payout_lookup():
    """★エッジは人気7番以降=中穴に集中している。単勝は配当が大きいぶん効率が
    良い可能性があり、確定払戻は nl_hr に年単位で揃っているので測るだけなら安い。
    券種の差し込みが両方の SQL に効いていることを固定する。"""
    import re

    import pglast

    from hro_operations.flow_signal import _SQL_BT, _SQL_BT_NK, FlowConfig

    for q in (_SQL_BT, _SQL_BT_NK):
        assert "{BET}" in q and "'fuku'" not in q
        built = q.replace("{TABLE}", "ts_o1").replace("{BET}", "'tan'")
        pglast.parse_sql(re.sub(r"%\((\w+)\)s", r"$1", built))
        assert built.count("'tan'") == 2      # has_payout と払戻 JOIN の両方
    assert FlowConfig().bet_type == "fuku"


def test_live_orders_use_the_configured_bet_type():
    """★単勝×人気7+×上位5% が OOS 1.6690(P=0.007)で複勝(1.0768)を大きく上回ったので
    券種を可変にした。バックテストの券種名(nl_hr)と hro_buyer の券種名は**別物**なので、
    変換を通すこと(fuku→place / tan→win)。既定は従来どおり複勝。"""
    from hro_operations.flow_signal import _BET_TYPE, FlowConfig, flow_orders

    assert _BET_TYPE == {"fuku": "place", "tan": "win"}
    assert FlowConfig().bet_type == "fuku"

    rows = [_row("01", "0200", "0035", "late"), _row("02", "0600", "0090", "late"),
            _row("01", "0300", "0035", "early"), _row("02", "0500", "0090", "early")]
    key = ("2026", "0926", "06", "04", "08", "11")
    base = dict(source="sokuho", lead_seconds=60, flow_minutes=6, threshold=0.0)
    assert flow_orders(FakeDB(rows), key, FlowConfig(**base), 100, "t")[0].bet_type == "place"
    win = flow_orders(FakeDB(rows), key, FlowConfig(bet_type="tan", **base), 100, "t")[0]
    assert win.bet_type == "win"
    assert "ken=win" in win.reason and "ninki=" in win.reason


def test_normalize_divides_by_the_race_noise_level():
    """★実測(2026-09-29, ts@T-60s を2期間): レース全体の値動きが**小さい**ほど回収率が高い
    (下位25% 1.182/1.235 → 上位25% 0.994/0.985、単調・両期間一致)。
    race_move はノイズの水準、flow はシグナル。割れば S/N 比になる。
    ★尺度が変わるので閾値は必ず取り直す。"""
    from hro_operations.flow_signal import FlowConfig, flow_scores

    rows = [_row("01", "0020", "0035", "late"), _row("02", "0060", "0090", "late"),
            _row("01", "0030", "0035", "early"), _row("02", "0050", "0090", "early")]
    key = ("2026", "0926", "06", "04", "08", "11")
    base = dict(source="sokuho", lead_seconds=60, flow_minutes=6)

    raw = flow_scores(FakeDB(rows), key, FlowConfig(**base))
    nrm = flow_scores(FakeDB(rows), key, FlowConfig(normalize=True, **base))
    move = raw["01"]["race_move"]
    assert move > 0
    assert abs(nrm["01"]["score"] - raw["01"]["score"] / move) < 1e-9
    # 符号と順位は変わらない(同一レース内では単調変換)
    assert (nrm["01"]["score"] > nrm["02"]["score"]) == (raw["01"]["score"] > raw["02"]["score"])
    assert FlowConfig().normalize is False      # 既定は従来どおり


def test_small_fields_are_excluded_when_min_horses_is_set():
    """★複勝は**出走8頭以上で3着まで、5〜7頭は2着まで、4頭以下は発売なし**。
    2着までの複勝は別物なので、混ぜると条件の違うレースをまとめて最適化することになる。
    単勝には関係ないので既定は 0(制限なし)。"""
    from hro_operations.flow_signal import FlowConfig, flow_orders

    # 6頭立て(複勝は2着まで)
    rows = []
    for i, (t1, t0) in enumerate(
            [("0020", "0030"), ("0060", "0050"), ("0080", "0070"),
             ("0100", "0090"), ("0200", "0180"), ("0300", "0280")], 1):
        um = f"{i:02d}"
        rows.append(_row(um, t1, "0035", "late"))
        rows.append(_row(um, t0, "0035", "early"))
    key = ("2026", "0926", "06", "04", "08", "11")
    base = dict(source="sokuho", lead_seconds=60, flow_minutes=6, threshold=0.0)

    assert flow_orders(FakeDB(rows), key, FlowConfig(**base), 100, "t")      # 制限なしなら買う
    assert flow_orders(FakeDB(rows), key, FlowConfig(min_horses=6, **base), 100, "t")
    assert not flow_orders(FakeDB(rows), key, FlowConfig(min_horses=8, **base), 100, "t")
    assert FlowConfig().min_horses == 0


def test_backtest_sqls_share_the_tail_and_resolve_every_column():
    """★_SQL_BT と _SQL_BT_NK は末尾(SELECT と JOIN)を共有する。以前は NK 側がコピーで、
    _SQL_BT への修正が片方にしか当たらず腐った(2026-10-01: NK だけ
    `LEFT JOIN fk USING` が残り nl_se の year と衝突して AmbiguousColumn。
    作った時から壊れていて、一度も走らせていなかったので露見しなかった)。

    ★pglast のパースは**列を解決しない**。末尾が参照する別名(ra2.kyori 等)を
    両方の ra CTE が持っていることを明示的に確かめる。
    """
    import re

    import pglast

    from hro_operations.flow_signal import _BT_TAIL, _SQL_BT, _SQL_BT_NK

    for q in (_SQL_BT, _SQL_BT_NK):
        built = q.replace("{TABLE}", "ts_o1").replace("{BET}", "'fuku'")
        pglast.parse_sql(re.sub(r"%\((\w+)\)s", r"$1", built))
        ra = built[built.index("WITH ra AS ("):built.index("),", built.index("WITH ra AS ("))]
        for col in ("kyori", "track_cd", "grade_cd"):
            assert f"ra2.{col}" not in _BT_TAIL or col in ra, f"{col} が ra CTE に無い"
        # ★fk は ON で結合する(USING は左側の同名列と衝突する)
        assert "JOIN fk USING" not in built
    assert "{F1}" in _BT_TAIL and "{EXTRA_JOIN}" in _BT_TAIL


def test_shared_tail_only_references_columns_both_ctes_provide():
    """★末尾を共有する以上、そこが参照する l./e. の列は**両方の CTE**が出していないと
    実行時に UndefinedColumn になる。2026-10-01 に l.nin1 で落ちた(しかも nin1 は
    Python 側で一度も読んでいない死んだ列だった)。
    pglast のパースは列を解決しないので、ここで機械的に照合する。"""
    import re

    from hro_operations.flow_signal import _BT_TAIL, _SQL_BT, _SQL_BT_NK

    refs = set(re.findall(r"\b([le])\.([a-z_0-9]+)", _BT_TAIL))
    for name, q in (("BT", _SQL_BT), ("NK", _SQL_BT_NK)):
        for alias, cte in (("l", "late"), ("e", "early")):
            body = q[q.index(cte + " AS ("):]
            body = body[:body.index("\n)")]
            outs = set(re.findall(r"AS ([a-z_0-9]+)", body)) | \
                set(re.findall(r"t\.([a-z_0-9]+)", body))
            for a, col in refs:
                if a != alias:
                    continue
                assert col in outs, f"{name}.{cte} が {alias}.{col} を出していない"


# -- 閾値の母集団と買う側の母集団は一致していなければならない --------------------- #
def _sc(**by_um):
    """{馬番: {score, tan_odds, fuku_odds}} を作る。ninki は単勝オッズ順に振る。"""
    from hro_operations.flow_signal import assign_ninki

    sc = {um: {"score": v[0], "tan_odds": v[1], "fuku_odds": v[2]}
          for um, v in by_um.items()}
    assign_ninki(sc)
    return sc


def test_eligible_applies_the_ninki_band():
    """★人気7+ で買うなら、閾値もその部分集合で取る。

    単勝×人気7+×上位5% の OOS 1.6690 は「**人気7+ の中での**上位5%」。
    全馬の95%点を人気7+ に当てると選別率が5%から大きくずれる。
    """
    from hro_operations.flow_signal import FlowConfig, eligible

    sc = _sc(**{"01": (0.5, 2.0, 1.2), "02": (0.4, 5.0, 2.0), "03": (0.3, 9.0, 3.0),
                "04": (0.2, 20.0, 5.0), "05": (0.1, 30.0, 7.0), "06": (0.6, 40.0, 9.0),
                "07": (0.7, 50.0, 11.0), "08": (0.8, 60.0, 13.0)})
    cfg = FlowConfig(min_ninki=7)
    assert sorted(eligible(cfg, sc)) == ["07", "08"]
    assert sorted(eligible(FlowConfig(), sc)) == sorted(sc)


def test_eligible_drops_small_fields_entirely():
    """複勝の払戻対象頭数が変わるレースは丸ごと外す。"""
    from hro_operations.flow_signal import FlowConfig, eligible

    sc = _sc(**{"01": (0.5, 2.0, 1.2), "02": (0.4, 5.0, 2.0)})
    assert eligible(FlowConfig(min_horses=8), sc) == {}
    assert eligible(FlowConfig(min_horses=2), sc) == sc


def test_threshold_and_orders_use_the_same_population():
    """★同じ選別を2か所に書くと必ず片方だけ育って食い違う。

    threshold_from は分位を取る側、flow_orders は買う側。どちらも eligible() を
    通すこと(スコアの閾値だけが flow_orders 側の追加条件)。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "flow_signal.py").read_text(encoding="utf-8")
    body = src[src.index("def threshold_from("):src.index("def _in_tan_band(")]
    assert "eligible(cfg, sc)" in body, "threshold_from が母集団を絞っていない"

    i = src.index("def flow_orders(")
    j = src.index("\ndef ", i + 1)          # ★次の関数まで。backtest は別経路で独自に絞る
    orders = src[i:j]
    assert "eligible(cfg, sc).items()" in orders, "flow_orders が eligible を通っていない"
    # 個別の絞り込みが flow_orders に残っていない(= eligible に一本化されている)
    for leaked in ("_in_tan_band(cfg,", "_in_ninki_band(cfg,"):
        assert leaked not in orders, f"flow_orders に選別が残っている: {leaked}"


# -- 複勝オッズの欠けが単勝を巻き込まないこと ------------------------------------ #
def _rows(tan_late, tan_early, fuku):
    """flow_scores が読む行の形。netkeiba は単勝のみ、複勝は ts_sokuho_o1 由来。"""
    out = []
    for um in tan_late:
        out.append({"umaban": um, "which": "late", "tan_odds": tan_late[um],
                    "fuku_odds_low": fuku.get(um), "ts": 2, "lead_sec": 90})
        out.append({"umaban": um, "which": "early", "tan_odds": tan_early[um],
                    "fuku_odds_low": fuku.get(um), "ts": 1, "lead_sec": 360})
    return out


class _DB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _sql, _params):
        return self._rows


def _scores(cfg, fuku):
    from hro_operations.flow_signal import flow_scores

    tan_late = {"01": 2.0, "02": 5.0, "03": 9.0, "04": 20.0,
                "05": 30.0, "06": 40.0, "07": 50.0, "08": 60.0}
    tan_early = {k: v * 1.1 for k, v in tan_late.items()}
    db = _DB(_rows(tan_late, tan_early, fuku))
    return flow_scores(db, ("2026", "1004", "08", "04", "02", "05"), cfg)


def test_missing_place_odds_does_not_drop_horses_for_win_bets():
    """★単勝を買うのに複勝オッズで馬を落としていた。

    2026-10-03 の再起動で速報ポーリング(ts_sokuho_o1)が止まり、3レースが
    「8/8 頭を除外」で丸ごと消えた。単勝なら複勝オッズは要らない。
    """
    from hro_operations.flow_signal import FlowConfig

    all_um = [f"0{i}" for i in range(1, 9)]
    none_fuku: dict[str, float] = {}
    assert sorted(_scores(FlowConfig(source="netkeiba", bet_type="tan"), none_fuku)) == all_um
    assert _scores(FlowConfig(source="netkeiba", bet_type="fuku"), none_fuku) == {}


def test_partial_place_odds_gap_does_not_shift_ninki_for_win_bets():
    """★部分欠けの方が危ない。

    一部の馬だけ落ちると assign_ninki が残った馬で人気を振り直すので、
    「人気7番以降」が**別の馬**を指す。単勝ではそもそも落とさない。
    """
    from hro_operations.flow_signal import FlowConfig

    # 人気1・2(単勝が安い馬)だけ複勝オッズが欠けている状況
    partial = {f"0{i}": 1.5 + i for i in range(3, 9)}
    sc = _scores(FlowConfig(source="netkeiba", bet_type="tan"), partial)
    assert len(sc) == 8
    # 単勝オッズ順に人気が振られている(落ちた馬がいないので正しい)
    assert sc["01"]["ninki"] == 1 and sc["08"]["ninki"] == 8
    assert [u for u, d in sc.items() if d["ninki"] >= 7] == ["07", "08"]


def test_coverage_warning_fires_when_the_data_is_narrower(capsys):
    """★要求した期間と実際に取れた期間のずれを黙らせない。

    2026-10-03 に2回続けて踏んだ: 1年を指定したのに ts_sokuho_o1 は速報の
    リアルタイム収集で直近1か月しか無く、49レース18本を「1年の検証」として
    読みかけた。信号源ごとに保有期間がまったく違う。
    """
    from hro_operations.__main__ import _warn_coverage

    races = [("2026", "0802", "05", "03", "04", "01"),
             ("2026", "0809", "05", "03", "05", "01")]
    _warn_coverage("20250901", "20260831", races, "sokuho")
    out = capsys.readouterr().out
    assert "20260802〜20260809" in out and "2 開催日" in out
    assert "⚠" in out and "狭い" in out

    _warn_coverage("20260802", "20260809", races, "ts")
    assert "⚠" not in capsys.readouterr().out


def test_order_reason_survives_missing_place_odds():
    """★単勝では複勝オッズが None でありうる(31256e0)。理由文の整形で落とさない。

    2026-10-04 に flow-picks が TypeError で止まった。発注直前ではなく
    BetOrder 生成時なので、live でも同じ場所で落ちていた。
    """
    from hro_operations.flow_signal import FlowConfig, flow_orders

    cfg = FlowConfig(source="netkeiba", bet_type="tan", min_ninki=7,
                     thresholds={90: -9.0}, lead_seconds=90, flow_minutes=6)
    sc = _scores(cfg, {})          # 複勝オッズ無し
    assert sc, "前提: 単勝なら複勝オッズ無しでもスコアは出る"

    class _DB2:
        def query(self, _s, _p):
            tan_late = {"01": 2.0, "02": 5.0, "03": 9.0, "04": 20.0,
                        "05": 30.0, "06": 40.0, "07": 50.0, "08": 60.0}
            return _rows(tan_late, {k: v * 1.1 for k, v in tan_late.items()}, {})

    orders = flow_orders(_DB2(), ("2026", "1004", "08", "04", "02", "05"),
                         cfg, 1000, "test")
    assert orders, "候補が出ていない"
    assert all("fuku=-" in o.reason for o in orders)
    assert all(o.bet_type == "win" and o.odds > 0 for o in orders)


def test_analysis_commands_expose_the_live_selection():
    """★運用している条件(単勝×人気7+)でスライス/掃引できること。

    flow-slice のハンドラは getattr(args, "min_ninki", 0) で読む作りだったのに
    パーサに引数が無く、**常に既定値(複勝・絞り無し)に落ちていた**
    (2026-10-05 発覚)。flow-threshold で直したのと同じ母集団ずれ。
    見ている集合と買っている集合が違うと、どんな分析も意味を持たない。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "__main__.py").read_text(encoding="utf-8")
    for name in ("flow-slice", "flow-sweep", "flow-threshold", "flow-backtest"):
        i = src.index(f'add_parser("{name}"')
        j = src.index("set_defaults(func=", i)
        block = src[i:j]
        assert "--min-ninki" in block, f"{name} に --min-ninki が無い"
        if name != "flow-threshold":      # 閾値はスコアの分位なので券種に依らない
            assert "--bet-type" in block, f"{name} に --bet-type が無い"


# -- 着順を問う券種(馬単/三連単) ------------------------------------------------- #
class _ComboDB:
    """払戻表を返すだけ。kumi は JRA 表記(馬単 '0203' = 1着02→2着03)。"""

    def __init__(self, pays):
        self._pays = pays          # [(rid, kumi, pay), ...]

    def query(self, _sql, params):
        return [{"rid": r, "kumi": k, "pay": p} for r, k, p in self._pays
                if True]


def _combo_rows():
    """1レース: 軸=05(人気8・flow 高)、人気上位 01/02/03。"""
    return [
        {"rid": "R1", "umaban": "05", "ninki": 8, "flow": 0.50},
        {"rid": "R1", "umaban": "01", "ninki": 1, "flow": 0.00},
        {"rid": "R1", "umaban": "02", "ninki": 2, "flow": 0.00},
        {"rid": "R1", "umaban": "03", "ninki": 3, "flow": 0.00},
    ]


def test_umatan_fixes_the_axis_in_first_place():
    """★軸が**1着**のときだけ当たること。

    flow_tan は同じ馬・同じシグナルで 単勝 1.7000 / 複勝 1.2832(ts T-60s)。
    「勝つこと」を当てているので、着順を問う券種は軸を1着に固定して試す。
    """
    from hro_operations.combos import evaluate

    # 軸05が1着・01が2着 → '0501' が的中
    db = _ComboDB([("R1", "0501", "5000")])
    rep = evaluate(db, _combo_rows(), "20260101", "20260102", "umatan",
                   threshold=0.1, partners=3, max_combos=9, amount=100)
    assert rep["bets"] == 3, rep          # 相手3頭 × 1通り
    assert rep["returned"] == 5000, rep

    # 逆順 '0105'(01が1着)では当たらない
    db2 = _ComboDB([("R1", "0105", "5000")])
    rep2 = evaluate(db2, _combo_rows(), "20260101", "20260102", "umatan",
                    threshold=0.1, partners=3, max_combos=9, amount=100)
    assert rep2["returned"] == 0, "軸が2着でも当たってしまっている"


def test_umaren_still_ignores_order():
    """着順を問わない券種は従来どおり(並べ替えて照合)。"""
    from hro_operations.combos import evaluate

    db = _ComboDB([("R1", "0105", "3000")])     # 馬連は昇順表記
    rep = evaluate(db, _combo_rows(), "20260101", "20260102", "umaren",
                   threshold=0.1, partners=3, max_combos=9, amount=100)
    assert rep["returned"] == 3000, rep


def test_sanrentan_buys_both_orders_of_the_partners():
    """三連単は軸1着固定でも、相手2頭の順序ぶん点数が増える。"""
    from hro_operations.combos import evaluate

    db = _ComboDB([("R1", "050201", "90000")])
    rep = evaluate(db, _combo_rows(), "20260101", "20260102", "sanrentan",
                   threshold=0.1, partners=3, max_combos=99, amount=100)
    assert rep["bets"] == 6, rep           # 3頭から2頭の順列
    assert rep["returned"] == 90000, rep


def test_every_payout_bet_type_is_reachable():
    """nl_hr が持つ組み合わせ券は全部評価できること(測れない券種を残さない)。"""
    from hro_operations.combos import COMBO_TYPES

    # nl_hr.bet_type: tan/fuku/waku/umaren/wide/umatan/sanrenfuku/sanrentan
    for bt in ("umaren", "wide", "umatan", "sanrenfuku", "sanrentan"):
        assert any(v[0] == bt for v in COMBO_TYPES.values()), bt


def test_every_combo_type_has_a_display_name():
    """★券種を足して表示名を忘れると KeyError で落ち、**集計し終えた分まで捨てる**。

    2026-10-06 に実際に踏んだ: 103秒かけてワイド/馬連/三連複まで出した直後に
    umatan で落ち、全部やり直しになった。
    """
    from pathlib import Path

    from hro_operations.combos import COMBO_TYPES

    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "__main__.py").read_text(encoding="utf-8")
    i = src.index('names = {"wide"')
    block = src[i:src.index('if not r["bets"]', i)]   # ★lab の組み立てまで含める
    for bet in COMBO_TYPES:
        assert f'"{bet}"' in block, f"{bet} の表示名が無い"
    assert "names.get(" in block, "未知の券種で落ちない作りにすること"


def test_partner_by_flow_picks_the_top_flow_horses_not_the_favourites():
    """★相手を flow 順で選べること。

    軸は「勝つ馬」を当てるが、相手に要るのは「2着に来る確率」。
    人気(市場の最終評価)が最良とは限らないので、両方試せるようにする。
    """
    from hro_operations.combos import evaluate

    rows = [
        {"rid": "R1", "umaban": "05", "ninki": 8, "flow": 0.50},   # 軸
        {"rid": "R1", "umaban": "09", "ninki": 9, "flow": 0.30},   # flow2位・人気薄
        {"rid": "R1", "umaban": "01", "ninki": 1, "flow": 0.01},   # 人気1位
        {"rid": "R1", "umaban": "02", "ninki": 2, "flow": 0.00},
    ]
    # ★閾値は 09(flow 0.30)が**軸にならない**値にする。軸が2頭になると
    #   相手の選び方ではなく軸の数で点数が変わり、比較にならない。
    # flow 相手1頭 → 09 が相手。'0509' が的中
    db = _ComboDB([("R1", "0509", "7000")])
    rep = evaluate(db, rows, "20260101", "20260102", "umatan",
                   threshold=0.4, partners=1, partner_by="flow",
                   max_combos=9, amount=100)
    assert rep["bets"] == 1 and rep["returned"] == 7000, rep
    assert rep["partner_by"] == "flow"

    # 人気 相手1頭 → 01 が相手。'0509' では当たらない
    rep2 = evaluate(db, rows, "20260101", "20260102", "umatan",
                    threshold=0.4, partners=1, partner_by="ninki",
                    max_combos=9, amount=100)
    assert rep2["bets"] == 1 and rep2["returned"] == 0, rep2


def test_partner_by_flow_excludes_the_axis_before_taking_the_top_n():
    """★軸は自分が flow 最上位でありがち。先に除かないと相手が N-1 頭になる。"""
    from hro_operations.combos import evaluate

    rows = [{"rid": "R1", "umaban": "05", "ninki": 8, "flow": 0.90},   # 軸=flow1位
            {"rid": "R1", "umaban": "09", "ninki": 9, "flow": 0.30},
            {"rid": "R1", "umaban": "01", "ninki": 1, "flow": 0.20},
            {"rid": "R1", "umaban": "02", "ninki": 2, "flow": 0.10}]
    rep = evaluate(_ComboDB([("R1", "9999", "0")]), rows, "20260101", "20260102",
                   "umatan", threshold=0.5, partners=3, partner_by="flow",
                   max_combos=9, amount=100)
    assert rep["bets"] == 3, f"相手が3頭になっていない: {rep['bets']}"

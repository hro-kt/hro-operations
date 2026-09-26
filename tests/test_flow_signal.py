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
        pglast.parse_sql(re.sub(r"%\((\w+)\)s", r"$1", q))
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

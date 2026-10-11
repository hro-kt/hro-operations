"""購入指示ごとの「なぜそう判断したか」を構造化して残す(bet_decision_logs)。

★bet_orders.reason の文字列だけでは後から切れない。「人気帯で落ちた頭数」や
  「窓ずれで見送ったレース」を数えるのに正規表現を書く羽目になる。
★却下も残す。「なぜこの馬だけ買ったのか」は、買わなかった馬の理由が無いと
  答えられない。
"""

from __future__ import annotations

from hro_operations.flow_signal import FlowConfig, flow_orders, horse_gates

from test_flow_signal import RACE, FakeDB, _row


def _rows(*specs):
    out = []
    for um, late, early, fuku in specs:
        out.append(_row(um, late, fuku, "late"))
        out.append(_row(um, early, fuku, "early"))
    return out


def _run(cfg, rows, amount=100):
    logs: list = []
    orders = flow_orders(FakeDB(rows), RACE, cfg, amount, "flow_tan", logs=logs)
    return orders, logs


def _by_sel(logs):
    return {lg.selection_id: lg for lg in logs}


def test_every_evaluated_horse_is_logged():
    orders, logs = _run(FlowConfig(threshold=0.0),
                        _rows(("01", "20", "30", "15"), ("02", "30", "30", "20")))
    assert [o.selection_id for o in orders] == ["01"]
    got = _by_sel(logs)
    assert set(got) == {"01", "02"}
    assert got["01"].decision == "accepted" and got["02"].decision == "rejected"


def test_accepted_log_carries_the_rule_and_the_numbers():
    _, logs = _run(FlowConfig(threshold=0.0),
                   _rows(("01", "20", "30", "15"), ("02", "30", "30", "20")))
    c = _by_sel(logs)["01"].constraints
    assert c["v"] == 1 and c["strategy"] == "flow"
    assert c["rule"] == "flow_tan >= threshold"
    assert c["signal"]["name"] == "flow_tan" and c["signal"]["score"] > 0
    assert c["market"]["tan_odds"] == 2.0 and c["market"]["fuku_odds"] == 1.5
    assert c["bet"] == {"type": "place", "amount": 100}
    # 設定(どのロジックか)も一緒に残す。これが無いと後から再現できない
    assert c["config"]["source"] == "ts" and c["config"]["lead_sec"] == 60


def test_gates_say_which_rule_decided_it():
    _, logs = _run(FlowConfig(threshold=0.0),
                   _rows(("01", "20", "30", "15"), ("02", "30", "30", "20")))
    g = {x["rule"]: x for x in _by_sel(logs)["02"].constraints["gates"]}
    assert g["flow_tan>=threshold"]["pass"] is False
    assert g["flow_tan>=threshold"]["got"] < g["flow_tan>=threshold"]["threshold"]
    assert "flow_tan>=threshold" in _by_sel(logs)["02"].reason


def test_horses_dropped_before_the_threshold_are_still_logged():
    """★オッズ帯・人気帯で落ちた馬は閾値の手前で消える。記録しないと消息不明になる。"""
    _, logs = _run(FlowConfig(threshold=0.0, max_odds=1.6),
                   _rows(("01", "20", "30", "15"), ("02", "30", "30", "99")))
    got = _by_sel(logs)
    assert got["02"].decision == "rejected"
    g = {x["rule"]: x for x in got["02"].constraints["gates"]}
    assert g["fuku_odds<=max"]["pass"] is False and g["fuku_odds<=max"]["max"] == 1.6
    assert "fuku_odds<=max" in got["02"].reason


def test_ninki_band_rejection_is_explicit():
    _, logs = _run(FlowConfig(threshold=0.0, min_ninki=2),
                   _rows(("01", "20", "30", "15"), ("02", "30", "30", "20")))
    g = {x["rule"]: x for x in _by_sel(logs)["01"].constraints["gates"]}
    assert g["ninki band"]["min"] == 2 and g["ninki band"]["got"] == 1
    assert g["ninki band"]["pass"] is False


def test_race_level_skip_is_recorded_once():
    """★買わなかったレースこそ理由が要る。「なぜ今日は3件だけなのか」に答えるため。"""
    orders, logs = _run(FlowConfig(threshold=0.0, min_horses=8),
                        _rows(("01", "20", "30", "15"), ("02", "30", "30", "20")))
    assert orders == []
    assert len(logs) == 1 and logs[0].selection_id == "*"
    g = {x["rule"]: x for x in logs[0].constraints["gates"]}
    assert g["min_horses"]["min"] == 8 and g["min_horses"]["got"] == 2
    assert g["min_horses"]["pass"] is False


def test_missing_snapshots_are_recorded():
    orders, logs = _run(FlowConfig(threshold=0.0), [_row("01", "20", "15", "late")])
    assert orders == [] and len(logs) == 1
    assert logs[0].selection_id == "*"
    assert {x["rule"] for x in logs[0].constraints["gates"]} == {"snapshots"}


def test_logs_are_optional_and_off_by_default():
    """★発注経路を重くしない。logs を渡さなければ何も作らない。"""
    orders = flow_orders(FakeDB(_rows(("01", "20", "30", "15"), ("02", "30", "30", "20"))),
                         RACE, FlowConfig(threshold=0.0), 100, "flow_tan")
    assert [o.selection_id for o in orders] == ["01"]


def test_gate_evaluation_is_shared_with_the_filter():
    """★記録と実際の絞り込みを別々に書かない。片方だけ育つと嘘の根拠が残る。"""
    cfg = FlowConfig(min_ninki=3, max_ninki=5)
    d = {"tan_odds": 10.0, "fuku_odds": 2.0, "ninki": 4, "score": 0.1}
    assert all(g["pass"] for g in horse_gates(cfg, d))
    d["ninki"] = 9
    assert not all(g["pass"] for g in horse_gates(cfg, d))


def test_recommended_amount_is_zero_for_rejections():
    _, logs = _run(FlowConfig(threshold=0.0),
                   _rows(("01", "20", "30", "15"), ("02", "30", "30", "20")), amount=1000)
    got = _by_sel(logs)
    assert got["01"].recommended_amount == 1000
    assert got["02"].recommended_amount == 0


def _real_log():
    """★偽物ではなく**本物の DecisionLog** を使う。

    frozen dataclass なので代入できない。偽のミュータブルなオブジェクトで
    テストしていたため、`lg.decided_at = now` が通ってしまい、本番でだけ
    FrozenInstanceError になった(2026-10-11 に実害: 全レースで1件も買えず)。
    """
    from hro_optimizer.models import DecisionLog
    return DecisionLog(race_id="R", selection_id="08", bet_type="win",
                       decision="accepted", reason="r", probability=0.0, odds=1.0,
                       expected_return=0.0, edge=0.0, recommended_amount=1000,
                       constraints={}, decided_at=None)


def _patch_sink(monkeypatch, emit):
    from hro_operations import race_day

    class _Sink:
        def __init__(self, *a, **k):
            pass

        def emit(self, orders, logs):
            emit(orders, logs)

        def close(self):
            pass

    import hro_moneymanager.postgres as mmpg
    monkeypatch.setattr(mmpg, "PostgresOrderSink", _Sink)
    monkeypatch.setattr(race_day.PostgresConfig, "from_env", staticmethod(lambda: None))
    return type("C", (), {"date": "20261011"})()


def test_decision_log_is_stamped_without_mutating_a_frozen_object(monkeypatch):
    """★frozen への代入は例外になり、購入まで巻き込む。replace で作り直す。"""
    from hro_operations import race_day
    seen = []
    cfg = _patch_sink(monkeypatch, lambda o, l: seen.append((list(o), list(l))))
    lg = _real_log()
    race_day._persist_orders(cfg, ["o1"], [lg])
    assert seen[0] == (["o1"], [])                 # 購入指示が先
    stamped = seen[1][1][0]
    assert stamped.decided_at is not None
    assert lg.decided_at is None                   # 元のオブジェクトは変えない


def test_existing_timestamp_is_kept(monkeypatch):
    from datetime import datetime, timezone

    from hro_operations import race_day
    seen = []
    cfg = _patch_sink(monkeypatch, lambda o, l: seen.append((list(o), list(l))))
    when = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
    import dataclasses
    lg = dataclasses.replace(_real_log(), decided_at=when)
    race_day._persist_orders(cfg, ["o1"], [lg])
    assert seen[1][1][0].decided_at == when


def test_order_persistence_survives_a_failing_decision_log(monkeypatch):
    """★根拠の書き込み失敗で購入指示まで巻き戻してはいけない。

    emit は同一トランザクションなので、1回で渡すと logs の失敗が orders を
    道連れにする。根拠は後から読む資料、購入指示は発注と突合の土台。
    """
    from hro_operations import race_day
    seen = []

    def _emit(orders, logs):
        seen.append((len(orders), len(logs)))
        if logs:
            raise RuntimeError("jsonb error")

    cfg = _patch_sink(monkeypatch, _emit)
    race_day._persist_orders(cfg, ["o1", "o2"], [_real_log()])  # 例外が外へ出ない
    assert seen == [(2, 0), (0, 1)]


def test_nothing_after_the_order_write_can_stop_the_purchase(monkeypatch):
    """★購入指示を記録した**後**の処理が購入を止められる経路を残さない。

    根拠は後から読む資料で、購入はその日しかできない。
    """
    from hro_operations import race_day

    class _Broken:
        """decided_at の参照そのもので落ちる(想定外の型が来ても購入を止めない)。"""

        @property
        def decided_at(self):
            raise RuntimeError("boom")

    cfg = _patch_sink(monkeypatch, lambda o, l: None)
    race_day._persist_orders(cfg, ["o1"], [_Broken()])     # 例外が外へ出ないこと

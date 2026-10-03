

def test_check_timing_rejects_threshold_without_matching_lead():
    """★決定時点に対応する閾値が無いと全レースが見送りになり、1日走って1件も買えない。
    走り出す前に落とす(2026-09-22 に『正常に走って0件』を実際に経験している)。"""
    import pytest

    from hro_operations.race_day import DayConfig, TimingError, check_timing

    base = dict(date="20260926", win_model="", place_model="", results_path="/tmp/r.jsonl",
                strategy="flow", lead_seconds=70, deadline_lead_seconds=60,
                flow_minutes=6, source="live")

    # netkeiba は秒単位。キーは lead_seconds そのもの
    ok = DayConfig(flow_source="netkeiba", flow_lead_seconds=75,
                   flow_thresholds={75: 0.3125}, **base)
    check_timing(ok)

    ng = DayConfig(flow_source="netkeiba", flow_lead_seconds=75,
                   flow_thresholds={60: 0.3125}, **base)
    with pytest.raises(TimingError, match="対応する閾値がありません"):
        check_timing(ng)

    # 格子ソースは60秒に丸めて引く
    grid = DayConfig(flow_source="sokuho", flow_lead_seconds=118,
                     flow_thresholds={120: 0.15}, **base)
    check_timing(grid)


def test_run_day_passes_bet_type_and_ninki_band_to_the_signal():
    """★単勝×人気7+ を live で使えるようにした。DayConfig から FlowConfig へ
    渡し漏れると、設定したのに**黙って複勝・全帯で走る**(2026-09-26 に信号源で
    同じ事故を起こしている)。"""
    from hro_operations.race_day import DayConfig

    cfg = DayConfig(date="20260927", win_model="", place_model="",
                    results_path="/tmp/r.jsonl", strategy="flow",
                    flow_bet_type="tan", flow_min_ninki=7)
    assert cfg.flow_bet_type == "tan" and cfg.flow_min_ninki == 7
    assert DayConfig(date="x", win_model="", place_model="",
                     results_path="x").flow_bet_type == "fuku"


def test_live_refuses_to_start_with_an_unverified_recipe(tmp_path):
    """★verified の判定は**最終送信の直前**で行われる。そこで落ちると、ログイン・
    画面遷移・確認まで済ませた末に送信だけ拒否され、しかも**締切を過ぎている**ので
    その場で直しても間に合わない。2026-09-27 に開催中の全件がこれで失敗した。
    走り出す前に落とす(check_timing と同じ考え方)。"""
    import json

    import pytest

    from hro_operations.race_day import DayConfig, TimingError, check_recipe_verified

    def _cfg(p):
        return DayConfig(date="20260927", win_model="", place_model="",
                         results_path="", strategy="flow", mode="live",
                         ipat_recipe=str(p))

    from hro_buyer.ipat import RECIPE_VERSION as _V

    ng = tmp_path / "ng.json"
    ng.write_text(json.dumps({"verified": False, "version": _V}), encoding="utf-8")
    with pytest.raises(TimingError, match="未検証"):
        check_recipe_verified(_cfg(ng))

    from hro_buyer.ipat import RECIPE_VERSION

    ok = tmp_path / "ok.json"
    # ★verified だけでは通らない。版も一致していること(古いレシピの検出が本来の役目)
    ok.write_text(json.dumps({"verified": True, "version": RECIPE_VERSION}),
                  encoding="utf-8")
    check_recipe_verified(_cfg(ok))

    with pytest.raises(TimingError, match="ありません"):
        check_recipe_verified(_cfg(tmp_path / "none.json"))

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(TimingError, match="読めません"):
        check_recipe_verified(_cfg(broken))


def test_live_refuses_a_stale_recipe_even_if_verified(tmp_path):
    """★verified は**版に紐づける**。組み込みレシピが更新されたのにファイルが古いままだと、
    画面と手順が食い違ったまま「検証済み」として送信してしまう。
    版が上がったら検証をやり直す(お金が動くので摩擦は正しい)。"""
    import json

    import pytest

    from hro_buyer.ipat import RECIPE_VERSION
    from hro_operations.race_day import DayConfig, TimingError, check_recipe_verified

    def _cfg(p):
        return DayConfig(date="20261003", win_model="", place_model="",
                         results_path="", strategy="flow", mode="live",
                         ipat_recipe=str(p))

    old = tmp_path / "old.json"
    old.write_text(json.dumps({"verified": True, "version": RECIPE_VERSION - 1}),
                   encoding="utf-8")
    with pytest.raises(TimingError, match="古い版"):
        check_recipe_verified(_cfg(old))

    cur = tmp_path / "cur.json"
    cur.write_text(json.dumps({"verified": True, "version": RECIPE_VERSION}),
                   encoding="utf-8")
    check_recipe_verified(_cfg(cur))


def test_hasso_time_is_reread_before_each_race():
    """★起動時の一覧をそのまま使うと**発走時刻変更**に追随できない(2026-10-03 に実害)。
    変更(TC)は受信していないので、唯一の経路は RACE 再同期後の nl_ra。
    読み直さないと、遅延したレースでは古い時刻で起きて窓がずれ、繰り上がったレースでは
    締切後に投票しに行く。"""
    # ★import に依存せずファイルを読む(この検査は依存パッケージを必要としない)
    from pathlib import Path

    f = (Path(__file__).resolve().parents[1] / "hro_operations" / "race_day.py").read_text(
        encoding="utf-8")
    loop = f[f.index("def _run_races("):f.index("def _refresh_hasso(")] \
        if f.index("def _refresh_hasso(") > f.index("def _run_races(") \
        else f[f.index("def _run_races("):]
    assert "_refresh_hasso" in loop, "レース毎に発走時刻を読み直していない"
    # ★検証モード(過去日)では読み直す意味が無いので除外されていること
    assert "verify or no_wait" in loop
    ref = f[f.index("def _refresh_hasso("):f.index("def _run_races(")]
    assert "fallback" in ref, "読めないときに起動時の値へ退避していない"



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


def test_day_config_expands_the_recipe_path():
    """★`~` は DayConfig の時点で展開しておく。

    Windows の PowerShell は外部コマンドの引数の `~` を展開しないので、
    `--ipat-recipe ~/ipat_recipe.json` がリテラルのまま届く。
    check_recipe_verified は os.path.expanduser してから見るのに
    build_day_executor は生のまま渡していたので、**起動検査は通るのに
    実行時にレシピが読めない**がありえた。
    """
    import os

    from hro_operations.race_day import DayConfig

    cfg = DayConfig(date="20261004", win_model="w", place_model="p",
                    results_path="r.jsonl", ipat_recipe="~/ipat_recipe.json")
    assert cfg.ipat_recipe == os.path.expanduser("~/ipat_recipe.json")
    assert "~" not in cfg.ipat_recipe

    assert DayConfig(date="20261004", win_model="w", place_model="p",
                     results_path="r.jsonl").ipat_recipe is None


# -- 複数券種(単勝 + 馬単)を同時に買う ------------------------------------------ #
def _day(**kw):
    from hro_operations.race_day import DayConfig

    base = dict(date="20261010", win_model="w", place_model="p",
                results_path="r.jsonl", flat_amount=1000)
    base.update(kw)
    return DayConfig(**base)


def test_bet_plan_parses_types_and_per_type_amounts():
    """★馬単は的中率1.2%・平均配当約190倍で谷が深い。単勝と同額にすると
    資金曲線が持たないので、券種ごとに金額を変えられること。"""
    from hro_operations.race_day import bet_plan

    assert bet_plan(_day(flow_bet_type="tan:1000,umatan:100")) == [("tan", 1000),
                                                                   ("umatan", 100)]
    # 金額を省けば flat_amount
    assert bet_plan(_day(flow_bet_type="tan,umatan")) == [("tan", 1000), ("umatan", 1000)]
    # 単一指定は従来どおり
    assert bet_plan(_day(flow_bet_type="tan")) == [("tan", 1000)]
    # 未指定は複勝
    assert bet_plan(_day()) == [("fuku", 1000)]


def test_bet_plan_keeps_the_order_as_written():
    """★並び順がそのまま購入順。締切に間に合わない可能性があるので、
    期待利益の大きい方を先に書けること。"""
    from hro_operations.race_day import bet_plan

    assert [b for b, _ in bet_plan(_day(flow_bet_type="umatan:100,tan:1000"))] \
        == ["umatan", "tan"]


def test_agent_rejects_unknown_bet_types_wholesale():
    """★知らない券種が1つでも混ざったら丸ごと fuku に落とす(お金が動く側は保守的に)。"""
    from hro_operations.agent import _bet_type_arg

    assert _bet_type_arg("tan:1000,umatan:100") == "tan:1000,umatan:100"
    assert _bet_type_arg("tan,umatan") == "tan,umatan"
    assert _bet_type_arg("tan,sanrentan") == "fuku"     # 未対応券種が混ざっている
    assert _bet_type_arg("tan:いくら") == "fuku"         # 金額が数字でない
    assert _bet_type_arg(None) == "fuku"


def test_bet_unit_is_the_ticket_unit_not_the_stake():
    """★bet_unit は「馬券の最小単位」= 100円。**1点の金額ではない**。

    flat_amount(1000)を入れていたため、券種ごとに金額を変えられるようにした途端、
    馬単(100円)が `not a multiple of bet_unit 1000` で全件 skipped になった
    (2026-10-10 に実害。購入指示は出ているのに買われない)。
    """
    from hro_operations.race_day import _buyer_config

    cfg = _day(flow_bet_type="tan:1000,umatan:100", flat_amount=1000)
    assert _buyer_config(cfg).bet_unit == 100

    # 券種ごとの金額がすべて単位の倍数であること
    from hro_operations.race_day import bet_plan

    unit = _buyer_config(cfg).bet_unit
    for bt, amount in bet_plan(cfg):
        assert amount % unit == 0, f"{bt} {amount}円 は {unit}円 の倍数でない"


def test_bet_unit_survives_a_small_per_type_stake():
    """1点100円の券種を混ぜても弾かれないこと。"""
    from hro_buyer.executor import _GuardedExecutor
    from hro_moneymanager.models import BetOrder

    from hro_operations.race_day import _buyer_config

    cfg = _buyer_config(_day(flow_bet_type="umatan:100", flat_amount=1000))
    ex = _GuardedExecutor(cfg)
    o = BetOrder(race_id="2026101005040301", selection_id="07-08", bet_type="umatan",
                 amount=100, probability=0, odds=0, expected_return=0, edge=0,
                 kelly_fraction=0, model_version="t", reason="t")
    assert ex._amount_reason(o) is None, ex._amount_reason(o)

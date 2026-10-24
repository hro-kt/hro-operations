"""preselect はどの式別を先に選ぶか。

★2026-10-24 京都12R、締切28.8秒の窓で画面移動(preselect)をやり直し、
  4件(¥2,900)を落とした。前倒しは **効いていなかった**。

  原因: race_day が `cfg.bet_types[0]` を渡していた。これは**モデル戦略**の
  券種(既定 place)で、flow の券種は `cfg.flow_bet_type`(tan/fuku/umatan)。
  flow で単勝を買う日に複勝を先に選んでいたので、preselect のキャッシュ
  (レース, 式別)が submit 時に一致せず、毎レース窓の中でやり直していた。

  しかも語も違う。IPAT 側の鍵は win/place/umatan で、設定の tan/fuku を
  そのまま渡すと SHIKIBETSU_CODE に無く、preselect は**黙って何もしない**。
  間違え方が2通りあり、どちらも静かに失敗する。
"""

from __future__ import annotations

from hro_buyer.vote import SHIKIBETSU_CODE

from hro_operations.race_day import DayConfig, bet_plan, first_shikibetsu


def _cfg(**kw) -> DayConfig:
    return DayConfig(date="20261024", win_model="m", place_model="m",
                     results_path="r.jsonl", **kw)


def _flow(bet_type: str, **kw) -> DayConfig:
    return _cfg(strategy="flow", flow_bet_type=bet_type, **kw)


def test_flow_preselects_the_bet_type_it_will_actually_buy():
    """★ここが肝。cfg.bet_types(モデル用)を見てはいけない。"""
    cfg = _flow("tan:2000,umatan:300")
    assert cfg.bet_types == ("place",)          # モデル側の既定は触っていない
    assert first_shikibetsu(cfg) == "win"       # それでも単勝を先に選ぶ


def test_the_result_is_a_name_ipat_understands():
    """★tan/fuku を渡すと SHIKIBETSU_CODE に無く、preselect は黙って帰る。"""
    for spec in ("tan", "fuku", "umatan", "tan:2000,umatan:300", "fuku:100"):
        got = first_shikibetsu(_flow(spec))
        assert got in SHIKIBETSU_CODE, f"{spec} -> {got}"


def test_the_order_of_bet_types_is_the_purchase_order():
    """★bet_plan の並び順がそのまま購入順(docstring)。先頭を先に選ぶ。"""
    assert first_shikibetsu(_flow("umatan:300,tan:2000")) == "umatan"
    assert first_shikibetsu(_flow("tan:2000,umatan:300")) == "win"


def test_it_matches_what_bet_plan_will_hand_to_the_executor():
    """★2箇所で別々に推測しない。実際に買う券種の先頭と一致していること。"""
    from hro_operations.flow_signal import BET_TYPE_CANON

    for spec in ("tan:2000,umatan:300", "fuku", "umatan:100,fuku:100"):
        cfg = _flow(spec)
        first = bet_plan(cfg)[0][0]
        assert first_shikibetsu(cfg) == BET_TYPE_CANON[first], spec


def test_the_model_strategy_still_uses_its_own_bet_types():
    cfg = _cfg(strategy="model", bet_types=("wide",))
    assert first_shikibetsu(cfg) == "wide"


def test_there_is_only_one_place_that_maps_the_names():
    """★変換表を2つ持つと、片方だけ直して黙って外れる。"""
    from hro_operations import flow_signal

    assert flow_signal._BET_TYPE is flow_signal.BET_TYPE_CANON


def test_race_day_passes_the_resolved_bet_type_to_preselect():
    """★呼び出し側が cfg.bet_types[0] に戻っていないこと。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "race_day.py").read_text(encoding="utf-8")
    i = src.index("pre(race_id,")
    assert "first_shikibetsu" in src[i - 200:i + 80]
    assert "pre(race_id, cfg.bet_types[0]" not in src

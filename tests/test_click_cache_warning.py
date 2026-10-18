"""クリック候補の学習が冷えている日に、朝のうちに気づく。

★2026-10-18 実測: 学習が無いと 1レース目の click_any が 9,266ms。
  うち約9.1秒は**当たらない候補3つ**が per_click(3秒)ずつタイムアウトした時間。
  判断から受付までの予算は29秒なので、1レース目だけ締切に間に合わないことがある。
  100円の live-test で温めておけば避けられる。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hro_operations.race_day import warn_click_cache_cold  # noqa: E402


def test_cold_cache_warns(monkeypatch, caplog):
    import hro_buyer.ipat as ip

    monkeypatch.setattr(ip, "load_click_cache", lambda p: {})
    with caplog.at_level(logging.WARNING):
        assert warn_click_cache_cold() is True
    assert "学習がまだありません" in caplog.text
    assert "live-test" in caplog.text, "何をすればよいかが出ていない"


def test_warm_cache_is_silent(monkeypatch, caplog):
    import hro_buyer.ipat as ip

    monkeypatch.setattr(ip, "load_click_cache", lambda p: {"a || b": "b"})
    with caplog.at_level(logging.WARNING):
        assert warn_click_cache_cold() is False
    assert caplog.text.strip() == ""


def test_it_never_blocks_the_day(monkeypatch):
    """★止めない。学習が無くても買える。"""
    import hro_buyer.ipat as ip

    monkeypatch.setattr(ip, "load_click_cache",
                        lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        warn_click_cache_cold()
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"例外で開催日を止めている: {e}") from e


def test_it_is_called_from_the_recipe_check():
    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "race_day.py").read_text(encoding="utf-8")
    i = src.index("def check_recipe_verified")
    j = src.index("def warn_click_cache_cold")
    assert "warn_click_cache_cold()" in src[i:j], "朝の確認から呼ばれていない"


def test_the_verified_check_still_runs():
    """★関数の途中に別の定義を差し込むと、後半が丸ごと死にコードになる。

    2026-10-18 にやってしまい、verified=false のレシピを素通しにしていた。
    既存テストが捕まえたが、位置関係も固定しておく。
    """
    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "race_day.py").read_text(encoding="utf-8")
    i = src.index("def check_recipe_verified")
    j = src.index("def warn_click_cache_cold")
    body = src[i:j]
    assert 'if not recipe.get("verified"):' in body, "verified の検査が関数の外に出ている"
    assert body.index('recipe.get("verified")') < body.index("warn_click_cache_cold()")

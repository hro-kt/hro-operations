"""見送りの理由をログに出す。

★2026-10-17: 「4件発注 -> {'skipped': 4} (intended=0円)」としか出ず、
  「購入指示は出ているのに買われない」を調べるのに JSONL を開く必要があった。
  理由は executor が message に入れている。その場に出すだけでよい。
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hro_operations.race_day import _log_skips, skip_reason  # noqa: E402


@dataclass
class _R:
    status: str
    message: str
    bet_type: str = "win"
    selection_id: str = "08"
    amount: int = 2000


class _Res:
    def __init__(self, results):
        self.results = results


# --------------------------------------------------------------------------- #
# 理由の切り出し
# --------------------------------------------------------------------------- #
def test_prefix_is_stripped():
    assert skip_reason("[LIVE] skipped: past voting deadline") == "past voting deadline"
    assert skip_reason("[PAPER] skipped: over daily cap") == "over daily cap"
    assert skip_reason("[DRY-RUN] skipped: amount 2000 > max") == "amount 2000 > max"


def test_duplicates_group_together():
    """★鍵が入ったままだと1件ごとに別グループになり、まとめる意味が無くなる。"""
    a = skip_reason("[LIVE] skipped: duplicate order (idempotency_key=R1|win|08)")
    b = skip_reason("[LIVE] skipped: duplicate order (idempotency_key=R1|umatan|08>03)")
    assert a == b == "duplicate order"


def test_empty_message_is_not_blank():
    assert skip_reason("") == "(理由なし)"
    assert skip_reason(None) == "(理由なし)"


# --------------------------------------------------------------------------- #
# ログ
# --------------------------------------------------------------------------- #
def test_reasons_are_logged_with_counts(caplog):
    res = _Res([
        _R("skipped", "[LIVE] skipped: duplicate order (idempotency_key=a)"),
        _R("skipped", "[LIVE] skipped: duplicate order (idempotency_key=b)"),
        _R("skipped", "[LIVE] skipped: past voting deadline"),
    ])
    with caplog.at_level(logging.INFO):
        _log_skips("R1", res)
    text = caplog.text
    assert "duplicate order" in text and "past voting deadline" in text
    assert "見送り 2件" in text and "見送り 1件" in text


def test_all_skipped_is_a_warning_and_says_so(caplog):
    """★これが『指示は出たのに買われない』の形。INFO に埋もれさせない。"""
    res = _Res([_R("skipped", "[LIVE] skipped: duplicate order (idempotency_key=a)")] * 4)
    with caplog.at_level(logging.INFO):
        _log_skips("R1", res)
    assert "1件も買っていません" in caplog.text
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "全件見送りなのに WARNING が出ていない"


def test_partial_skip_is_not_a_warning(caplog):
    res = _Res([_R("submitted", ""),
                _R("skipped", "[LIVE] skipped: over race cap")])
    with caplog.at_level(logging.INFO):
        _log_skips("R1", res)
    assert "over race cap" in caplog.text
    assert "1件も買っていません" not in caplog.text


def test_nothing_is_logged_when_all_succeeded(caplog):
    with caplog.at_level(logging.INFO):
        _log_skips("R1", _Res([_R("submitted", "")]))
    assert caplog.text.strip() == ""


def test_an_example_order_is_shown(caplog):
    """★どの買い目が落ちたかが分からないと、再現できない。"""
    res = _Res([_R("skipped", "[LIVE] skipped: over daily cap",
                   bet_type="umatan", selection_id="08>03", amount=300)])
    with caplog.at_level(logging.INFO):
        _log_skips("R1", res)
    assert "umatan" in caplog.text and "08>03" in caplog.text and "300" in caplog.text

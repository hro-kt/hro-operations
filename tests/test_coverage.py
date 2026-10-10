"""網羅性の記録と、年次の税務集計。

★雑所得(ハズレ馬券も経費)の主張で中核になるのは「個々の馬券の的中に着目しない
  網羅的な購入」であること。全部買うことではなく、**レースごとの恣意的な選択を
  していない**こと。同じ規則を全レースに当てた記録がそれを示す。
★穴(規則を当てられなかったレース)を隠さないこと。都合の悪い行を落とした記録は、
  落としていない行まで疑われる。
"""

from __future__ import annotations

import pytest

from hro_operations.coverage import (
    BOUGHT,
    EVALUATED,
    NO_CANDIDATE,
    NO_DATA,
    NOT_RUNNING,
    RACE_SKIPPED,
    classify,
    summarize,
)
from hro_operations.tax_report import ICHIJI_DEDUCTION, ichiji_shotoku, zatsu_shotoku


# --- 分類 ---------------------------------------------------------------

def test_bought_wins_over_everything():
    st, _ = classify(n_bought=2, dlogs=[], runner_up=False)
    assert st == BOUGHT


def test_evaluated_but_no_horse_passed():
    st, why = classify(
        n_bought=0,
        dlogs=[{"selection_id": "01", "reason": "flow_tan>=threshold(-0.21)"},
               {"selection_id": "02", "reason": "ninki band(2)"}],
        runner_up=True)
    assert st == NO_CANDIDATE and "2 頭" in why


def test_race_level_skip_is_distinguished():
    st, why = classify(
        n_bought=0,
        dlogs=[{"selection_id": "*", "reason": "出走 6 頭が下限 8 頭未満"}],
        runner_up=True)
    assert st == RACE_SKIPPED and "下限" in why


def test_missing_snapshots_are_not_counted_as_a_rule_skip():
    """★スナップ不足は『規則で見送った』のではなく『当てられなかった』。

    一緒にすると、収集の失敗が選別の結果に化けて網羅率が実態より良く見える。
    """
    st, _ = classify(
        n_bought=0,
        dlogs=[{"selection_id": "*", "reason": "スナップショット不足でスコアを出せない"}],
        runner_up=True)
    assert st == NO_DATA and st not in EVALUATED


def test_runner_down_is_recorded_as_such():
    """★穴は隠さない。ランナーが動いていなかったレースはそう書く。"""
    st, why = classify(n_bought=0, dlogs=[], runner_up=False)
    assert st == NOT_RUNNING and "動いていなかった" in why


def test_runner_up_but_no_record_is_a_data_gap():
    st, _ = classify(n_bought=0, dlogs=[], runner_up=True)
    assert st == NO_DATA


def test_evaluated_set_excludes_the_gaps():
    assert EVALUATED == {BOUGHT, NO_CANDIDATE, RACE_SKIPPED}
    assert NO_DATA not in EVALUATED and NOT_RUNNING not in EVALUATED


# --- 集計 ---------------------------------------------------------------

def _row(status, n=0, amount=0):
    return {"status": status, "n_bought": n, "amount": amount}


def test_ratio_denominator_includes_the_gaps():
    """★分母から穴を外すと網羅率が自動的に100%になり、指標として無意味になる。"""
    rep = summarize([_row(BOUGHT, 2, 2000), _row(NO_CANDIDATE),
                     _row(NOT_RUNNING), _row(NO_DATA)])
    assert rep["races"] == 4 and rep["evaluated"] == 2
    assert rep["evaluated_ratio"] == 0.5
    assert rep["bought_races"] == 1 and rep["tickets"] == 2 and rep["amount"] == 2000


def test_summarize_handles_an_empty_day():
    rep = summarize([])
    assert rep["races"] == 0 and rep["evaluated_ratio"] is None


# --- 所得の計算 ---------------------------------------------------------

def test_zatsu_is_payout_minus_every_ticket():
    """雑所得: ハズレ馬券も経費。特別控除は無い。"""
    assert zatsu_shotoku(bought_all=1_000_000, payout=1_200_000) == 200_000


def test_ichiji_only_deducts_the_winning_tickets_and_halves():
    """一時所得: 的中した馬券の購入額だけが経費。50万控除のうえ 1/2。"""
    # 払戻 1,200,000 / 的中分の購入 80,000
    got = ichiji_shotoku(bought_hit=80_000, payout=1_200_000)
    assert got == (1_200_000 - 80_000 - ICHIJI_DEDUCTION) // 2 == 310_000


def test_ichiji_never_goes_negative():
    assert ichiji_shotoku(bought_hit=10_000, payout=100_000) == 0


def test_the_two_treatments_differ_a_lot_on_the_same_facts():
    """★同じ事実から大きく違う数字が出る。だから両方を並べて出す。"""
    bought_all, bought_hit, payout = 1_000_000, 80_000, 1_200_000
    assert zatsu_shotoku(bought_all, payout) == 200_000
    assert ichiji_shotoku(bought_hit, payout) == 310_000


@pytest.mark.parametrize("payout,hit,expect", [
    (0, 0, 0),                       # 全ハズレ
    (500_000, 500_000, 0),           # 控除内
    (2_000_000, 100_000, 700_000),
])
def test_ichiji_table(payout, hit, expect):
    assert ichiji_shotoku(hit, payout) == expect


# --- 戦略書 -------------------------------------------------------------

def _rep():
    from hro_operations.tax_report import ichiji_shotoku
    return {
        "year": "2026",
        "totals": {"bought": 1_284_000, "payout": 1_531_200, "pnl": 247_200,
                   "roi": 1.1925, "days": 14, "receipts": 312},
        "monthly": [{"ym": "202610", "days": 6, "receipts": 140,
                     "bought": 540_000, "payout": 702_400,
                     "races": 480, "evaluated": 452, "bought_races": 118,
                     "gaps": 28, "tickets": 260}],
        "months_active": 1,
        "coverage": {"races": 480, "evaluated": 452, "evaluated_ratio": 452 / 480,
                     "bought_races": 118, "bought_ratio": 118 / 480,
                     "gaps": 28, "tickets": 260,
                     "by_status": {"bought": 118, "no_candidate": 310,
                                   "race_skipped": 24, "not_running": 20,
                                   "no_data": 8},
                     "by_month": {}},
        "manual": {"n": 1, "amount": 400,
                   "rows": [{"budget_key": "20261010", "race_id": "R",
                             "bet_type": "umatan", "selection_id": "07-08",
                             "amount": 400, "receipt": "0001"}]},
        "strategy_versions": [],
        "seals": {"links": 16, "days": 14, "last": "2026-11-30"},
        "settled": {"n_hit": 41, "n_settled": 312, "hit_cost": 62_000,
                    "cost": 1_284_000, "payout": 1_531_200, "complete": True},
        "tax": {"ichiji": ichiji_shotoku(62_000, 1_531_200),
                "ichiji_deduction": ICHIJI_DEDUCTION, "zatsu": 247_200},
    }


def _doc(rep=None, versions=None):
    from hro_operations.strategy_doc import render
    return render(rep or _rep(), versions=versions or [], now="2026-12-01 09:00 JST")


def test_doc_states_it_is_generated_not_written():
    """★手で書いた戦略書は『後から書いたのでは』の疑いを払えない。"""
    d = _doc()
    assert "自動生成" in d and "手で書いたものではなく" in d


def test_doc_shows_the_gaps_in_the_coverage_table():
    """★穴を落とした表を出すと、落としていない行まで疑われる。"""
    d = _doc()
    assert "ランナーが動いていなかった" in d and "スナップショット不足" in d
    assert "分母に含めています" in d


def test_doc_separates_manual_purchases():
    d = _doc()
    assert "手動による購入が 1 件" in d and "07-08" in d


def test_doc_says_so_when_there_is_no_manual_purchase():
    rep = _rep()
    rep["manual"] = {"n": 0, "amount": 0, "rows": []}
    assert "すべて自動購入です" in _doc(rep)


def test_doc_puts_both_treatments_side_by_side_without_concluding():
    """★結論は書かない。事実と両方の計算を並べるところまで。"""
    d = _doc()
    assert "雑所得として" in d and "一時所得として" in d
    assert "判断は本文書の範囲外" in d


def test_doc_reports_unsettled_instead_of_guessing():
    rep = _rep()
    rep["tax"]["ichiji"] = None
    assert "算出不能" in _doc(rep)


def test_doc_lists_the_strategy_versions_with_hashes():
    v = [{"version": 1, "effective_from": "20261010", "effective_to": None,
          "params_hash": "cac702861dd0ee6712",
          "params": {"flow": {"source": "netkeiba", "lead_seconds": 90,
                              "flow_minutes": 6, "thresholds": {"90": 0.1368},
                              "min_ninki": 7},
                     "bets": [{"bet_type": "tan", "amount": 1000}]}}]
    d = _doc(versions=v)
    assert "cac702861dd0ee67" in d and "netkeiba" in d and "min_ninki=7" in d


def test_doc_names_the_source_tables():
    d = _doc()
    for t in ("ipat_receipts", "bet_orders", "bet_decision_logs",
              "strategy_versions", "race_coverage", "tax_ledger_seals"):
        assert t in d


def test_doc_monthly_row_does_not_confuse_amount_with_race_count():
    """★monthly 行は購入**額**(bought)とレース**数**(bought_races)を両方持つ。

    取り違えると黙って別の数字が出る(月別の表に 540,000 R と並ぶ)。
    """
    d = _doc()
    line = next(x for x in d.splitlines() if x.startswith("| 202610 |"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    assert cells[3] == "540,000"      # 購入額
    assert cells[7] == "118"          # 買ったレース数


# --- 戦略書の保存 -------------------------------------------------------

class _DocConn:
    def __init__(self, rows=None):
        self.rows = rows or []       # (revision, content_hash)
        self._r = []

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        if q.startswith("SELECT revision, content_hash FROM strategy_documents"):
            self._r = [self.rows[-1]] if self.rows else []
        elif q.startswith("INSERT INTO strategy_documents"):
            self.rows.append((params[1], params[3]))
            self._r = []
        else:                                # pragma: no cover
            raise AssertionError(q[:60])
        return self

    def fetchone(self):
        return self._r[0] if self._r else None


def test_saving_a_document_starts_at_revision_1():
    from hro_operations.strategy_doc import save
    conn = _DocConn()
    r = save(conn, "2026", "# doc")
    assert r["status"] == "saved" and r["revision"] == 1 and len(r["hash"]) == 64


def test_regenerating_the_same_document_does_not_add_a_revision():
    """★生成しただけで版が増えると、何版が提出したものか分からなくなる。"""
    from hro_operations.strategy_doc import save
    conn = _DocConn()
    save(conn, "2026", "# doc")
    r = save(conn, "2026", "# doc")
    assert r["status"] == "unchanged" and r["revision"] == 1
    assert len(conn.rows) == 1


def test_a_changed_document_appends_a_revision():
    """★古い版は消さない。提出済みの資料が消えるのは最悪。"""
    from hro_operations.strategy_doc import save
    conn = _DocConn()
    save(conn, "2026", "# doc")
    r = save(conn, "2026", "# doc v2")
    assert r["status"] == "saved" and r["revision"] == 2
    assert len(conn.rows) == 2


def test_summary_carries_the_headline_numbers():
    from hro_operations.strategy_doc import summary_of
    s = summary_of(_rep())
    assert s["bought"] == 1_284_000 and s["payout"] == 1_531_200
    assert s["races"] == 480 and s["bought_races"] == 118

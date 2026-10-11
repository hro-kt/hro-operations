"""開催中に投票履歴を取り込む(当日のリターンを見るため)。

★IPAT は同時セッションを嫌う。別ジョブでログインしに行くと投票側を奪いかねない。
  セッションを握っている executor が片手間に取る。
★払戻は IPAT の受付明細にその日のうちに載る。bet_settlements は nl_hr 待ちで
  開催の3〜5日後にしか埋まらない。
★**締切前には絶対にやらない**。画面を履歴へ移すので、戻る前に締切が来たら
  そのレースを落とす。
"""

from __future__ import annotations

import pytest

from hro_operations import race_day as rd


class _Client:
    def __init__(self, rows=None, fail=False):
        self.rows = rows if rows is not None else [{"receipt": "0001"}]
        self.fail = fail
        self.calls: list[str] = []

    def open_vote_history(self):
        self.calls.append("history")
        return not self.fail

    def vote_history_rows(self):
        return list(self.rows)

    def goto_bet_basic(self):
        self.calls.append("back")
        return True


class _Ex:
    def __init__(self, client):
        self.client = client


def _cfg(mode="live"):
    return rd.DayConfig(date="20261012", win_model="", place_model="",
                        results_path="", strategy="flow", mode=mode)


@pytest.fixture(autouse=True)
def _reset():
    rd._vh_last.clear()
    yield
    rd._vh_last.clear()


def _patch_import(monkeypatch, calls):
    import hro_buyer.__main__ as bm
    monkeypatch.setattr(bm, "_cmd_ipat_detail_all",
                        lambda c, r, save=None: calls.append((list(r), save)) or 0)


def test_imports_when_there_is_slack(monkeypatch):
    calls = []
    _patch_import(monkeypatch, calls)
    c = _Client()
    assert rd.refresh_vote_history(_cfg(), _Ex(c), 600.0) is True
    assert calls == [(["0001"], "20261012")]


def test_never_runs_close_to_a_deadline(monkeypatch):
    """★画面を履歴へ移すので、戻る前に締切が来たらそのレースを落とす。"""
    calls = []
    _patch_import(monkeypatch, calls)
    c = _Client()
    assert rd.refresh_vote_history(_cfg(), _Ex(c), 60.0) is False
    assert c.calls == [] and calls == []


def test_always_returns_to_the_voting_screen(monkeypatch):
    """★履歴を開いたまま次のレースへ行くと投票できない。"""
    _patch_import(monkeypatch, [])
    c = _Client()
    rd.refresh_vote_history(_cfg(), _Ex(c), 600.0)
    assert c.calls[-1] == "back"


def test_returns_to_the_voting_screen_even_on_failure(monkeypatch):
    _patch_import(monkeypatch, [])
    c = _Client(fail=True)
    assert rd.refresh_vote_history(_cfg(), _Ex(c), 600.0) is False
    assert "back" in c.calls


def test_a_failure_does_not_raise(monkeypatch):
    """★表示のための処理。購入を止めない。"""
    import hro_buyer.__main__ as bm
    monkeypatch.setattr(bm, "_cmd_ipat_detail_all",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    c = _Client()
    assert rd.refresh_vote_history(_cfg(), _Ex(c), 600.0) is False
    assert "back" in c.calls


def test_rate_limited(monkeypatch):
    """★毎レース取りに行かない。IPAT の画面遷移はただではない。"""
    calls = []
    _patch_import(monkeypatch, calls)
    c = _Client()
    assert rd.refresh_vote_history(_cfg(), _Ex(c), 600.0) is True
    assert rd.refresh_vote_history(_cfg(), _Ex(c), 600.0) is False
    assert len(calls) == 1


def test_paper_mode_never_touches_ipat(monkeypatch):
    calls = []
    _patch_import(monkeypatch, calls)
    c = _Client()
    assert rd.refresh_vote_history(_cfg(mode="paper"), _Ex(c), 600.0) is False
    assert c.calls == []


def test_no_executor_is_a_no_op(monkeypatch):
    assert rd.refresh_vote_history(_cfg(), None, 600.0) is False

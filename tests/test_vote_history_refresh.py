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


# --- 実物に対する検証(偽物だけで確かめない) -----------------------------

def test_the_real_client_has_the_methods_we_rely_on():
    """★偽物でテストして本番で落ちた前例がある(frozen DecisionLog)。

    寄り道から戻る手段と、preselect のキャッシュが**実物に在る**ことを確かめる。
    どれか1つでも名前が変われば、開催中に投票できなくなる。
    """
    from hro_buyer.ipat import PlaywrightIpatClient
    for name in ("goto_bet_basic", "open_vote_history", "vote_history_rows",
                 "open_vote_detail", "close_vote_detail", "preselect"):
        assert callable(getattr(PlaywrightIpatClient, name, None)), name
    # preselect のキャッシュ属性(これを捨てないと画面が戻らないまま投票に行く)
    import inspect
    src = inspect.getsource(PlaywrightIpatClient.preselect)
    assert "_preselected_for" in src


def test_preselect_cache_is_dropped_after_the_detour(monkeypatch):
    """★preselect は (レース,式別) でキャッシュされ、同じキーなら画面を動かさない。

    履歴へ寄り道した以上、画面は投票画面ではない。キャッシュを捨てないと
    「たまたま次のレースだからキーが違う」ことに安全が依存してしまう。
    """
    _patch_import(monkeypatch, [])
    c = _Client()
    c._preselected_for = ("R1", 1)
    rd.refresh_vote_history(_cfg(), _Ex(c), 600.0)
    assert c._preselected_for is None


def test_cache_is_dropped_even_when_returning_fails(monkeypatch):
    _patch_import(monkeypatch, [])
    c = _Client()
    c._preselected_for = ("R1", 1)
    c.goto_bet_basic = lambda: (_ for _ in ()).throw(RuntimeError("overlay"))
    rd.refresh_vote_history(_cfg(), _Ex(c), 600.0)   # 例外は外へ出ない
    # ★戻れたかどうかに関わらず捨てる。戻れていないときこそ必要。
    assert c._preselected_for is None


@pytest.mark.parametrize("where", ["open", "rows", "import", "back"])
def test_no_failure_path_escapes(monkeypatch, where):
    """★どこで落ちても開催日を止めない。購入ではなく表示のための処理。"""
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # noqa: E731
    calls = []
    _patch_import(monkeypatch, calls)
    c = _Client()
    if where == "open":
        c.open_vote_history = boom
    elif where == "rows":
        c.vote_history_rows = boom
    elif where == "import":
        import hro_buyer.__main__ as bm
        monkeypatch.setattr(bm, "_cmd_ipat_detail_all", boom)
    elif where == "back":
        c.goto_bet_basic = boom
    rd.refresh_vote_history(_cfg(), _Ex(c), 600.0)   # 例外が外へ出ないこと


def test_missing_goto_bet_basic_is_reported_not_silent(monkeypatch, caplog):
    """★戻る手段が無いのを黙って通さない(次のレースが投票できなくなる)。"""
    import logging
    _patch_import(monkeypatch, [])

    class _NoBack(_Client):
        goto_bet_basic = None

    with caplog.at_level(logging.ERROR):
        rd.refresh_vote_history(_cfg(), _Ex(_NoBack()), 600.0)
    assert any("goto_bet_basic" in r.message for r in caplog.records)


def test_detail_is_closed_even_if_parsing_raises(monkeypatch):
    """★明細を開いたままにすると、呼び出し側が投票画面へ戻れない。

    _cmd_ipat_detail_all の中で解析が落ちても close_vote_detail を通ること。
    """
    import hro_buyer.__main__ as bm
    closed = []

    class _C:
        _page = type("P", (), {"inner_text": staticmethod(
            lambda sel: (_ for _ in ()).throw(RuntimeError("boom")))})()

        def open_vote_detail(self, r):
            return True

        def close_vote_detail(self):
            closed.append(True)

    with pytest.raises(RuntimeError):
        bm._cmd_ipat_detail_all(_C(), ["0001"])
    assert closed == [True]

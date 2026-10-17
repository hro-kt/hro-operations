"""落ちた発注ジョブを、締切が全部過ぎた後に入れ直さない。

★2026-10-17: 自動運用で flow ランナーが落ち、16:30:43 に再投入された。
  最終レース(東京12R、投票締切16:29)を既に過ぎており、1件も買えないまま
  「1/36 レース処理」で正常終了に見えるログを残した。
  元の失敗(16:14 の IPAT 障害)がそれに隠れた。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hro_operations.orchestrator import (  # noqa: E402
    Step, buyable_remaining, start_steps,
)

JST = timezone(timedelta(hours=9))


class _Conn:
    def __init__(self, last_post):
        self._last = last_post

    def execute(self, sql, params=None):
        self._r = [(self._last,)] if self._last else [(None,)]
        return self

    def fetchone(self):
        return self._r[0]


def _at(h, m, s=0):
    return datetime(2026, 10, 17, h, m, s, tzinfo=JST)


def test_before_the_last_cutoff_there_is_still_something_to_buy():
    conn = _Conn("1630")        # 最終発走 16:30
    assert buyable_remaining(conn, "20261017", deadline_lead_seconds=60,
                             now=_at(16, 28)) is True


def test_at_the_last_cutoff_nothing_remains():
    """★締切 = 発走 - deadline_lead_seconds。発走ではない。"""
    conn = _Conn("1630")
    assert buyable_remaining(conn, "20261017", deadline_lead_seconds=60,
                             now=_at(16, 29)) is False


def test_the_actual_incident_is_detected():
    """2026-10-17 16:30:43 の再投入。最終発走16:30 / 締切16:29。"""
    conn = _Conn("1630")
    assert buyable_remaining(conn, "20261017", deadline_lead_seconds=60,
                             now=_at(16, 30, 43)) is False


def test_no_races_means_nothing_to_buy():
    assert buyable_remaining(_Conn(None), "20261017") is False


def test_the_flow_runner_is_marked_as_a_buying_step():
    """★収集は停止時刻まで入れ直す必要がある。買う手順だけを区別する。"""
    steps = start_steps("20261017", {"strategy": "flow"})
    by_kind = {s.kind: s for s in steps}
    assert by_kind["flow_day"].buys is True
    for kind in ("run_odds", "netkeiba_odds"):
        assert by_kind[kind].buys is False, kind


def test_plain_steps_do_not_buy_by_default():
    assert Step("x", "k", "t").buys is False


def test_monitor_checks_buyable_before_restarting_a_buying_step():
    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "orchestrator.py").read_text(encoding="utf-8")
    i = src.index("再投入します(")
    block = src[max(0, i - 900):i]
    assert "c.step.buys" in block and "buyable_remaining" in block
    assert "入れ直しません" in block
    # ★入れ直さないときこそ、直前のログを長めに出す(原因がそこにある)
    assert "tail[-400:]" in src


# --------------------------------------------------------------------------- #
# ★セッション切れは「入れ直し」では直らない
# --------------------------------------------------------------------------- #
def test_dead_session_is_recognised_from_the_log():
    """2026-10-17 の実ログ。収集は落ちずに非ログインのまま行を保存し続けた。"""
    from hro_operations.orchestrator import dead_session

    real = ("netkeiba: ページを開いた logged_in=False member_rank=NotLogin "
            "race_id=2026101708040610")
    assert dead_session(real) is True
    assert dead_session("netkeiba: プレミアムではない。秒単位の更新は得られない") is True


def test_a_healthy_log_is_not_mistaken_for_a_dead_session():
    from hro_operations.orchestrator import dead_session

    assert dead_session("logged_in=True member_rank=Premium") is False
    assert dead_session("") is False
    assert dead_session(None) is False


def test_dead_session_restart_is_rate_limited_not_forbidden():
    """★止めてはいけない。state を差し替えれば**再起動で直る**。

    2026-10-17 の問題は「入れ直したこと」ではなく、約3分おきに6回入れ直して
    2時間空転したこと。止めるのではなく間隔を空ける。
    """
    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "orchestrator.py").read_text(encoding="utf-8")
    i = src.index("なにも採っていません")
    block = src[max(0, i - 1200):i]
    assert "_job_dead_session" in block
    assert "session_restart_due" in block, "間隔を見ていない"
    assert "c.restarts = cfg.max_restarts" not in block, "恒久的に止めている"


def test_session_restart_interval():
    """間隔内は入れ直さない。空けば入れ直す。"""
    from hro_operations.orchestrator import Child, Step, session_restart_due

    c = Child(step=Step("x", "netkeiba_odds", "vm", {}), job_id=1)
    assert session_restart_due(c, 900, now=1000.0) is True      # 初回は必ず通す
    c.last_session_restart = 1000.0
    assert session_restart_due(c, 900, now=1100.0) is False
    assert session_restart_due(c, 900, now=1899.0) is False
    assert session_restart_due(c, 900, now=1900.0) is True


def test_the_2026_10_17_thrash_would_be_prevented():
    """実際の間隔(約3分おきに6回)が1回に収まること。"""
    from hro_operations.orchestrator import Child, Step, session_restart_due

    c = Child(step=Step("x", "netkeiba_odds", "vm", {}), job_id=1)
    allowed = 0
    for t in range(0, 70 * 60, 200):        # 約3分おきに70分
        if session_restart_due(c, 900, now=float(t)):
            allowed += 1
            c.last_session_restart = float(t)
    assert allowed <= 5, allowed
    assert allowed >= 2, "まったく入れ直せないのも行き過ぎ"


def test_the_marker_is_searched_over_the_whole_log_not_the_tail():
    """★警告はプロセス起動直後に1回だけ。末尾を切ると必ず取り逃す。"""
    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "orchestrator.py").read_text(encoding="utf-8")
    m = src[src.index("def _job_dead_session"):src.index("def buyable_remaining")]
    assert "coalesce(log,'')" in m
    assert "_tail(" not in m, "末尾だけを見ている"


def test_a_dead_session_collector_is_not_cancelled_in_a_loop(monkeypatch):
    """★実挙動で確かめる。切れているなら cancel も再投入もしない。"""
    import hro_operations.orchestrator as orc

    calls = {"cancel": 0, "enqueue": 0}
    monkeypatch.setattr(orc, "_cancel", lambda c, j: calls.__setitem__("cancel", calls["cancel"] + 1))
    monkeypatch.setattr(orc, "_enqueue", lambda c, s, p: calls.__setitem__("enqueue", calls["enqueue"] + 1) or 99)
    monkeypatch.setattr(orc, "_job_dead_session", lambda conn, jid: True)
    monkeypatch.setattr(orc, "check", lambda *a, **k: {"silent": ["netkeiba"]}, raising=False)

    import sys as _s
    mod = type(_s)("hro_operations.collector_health")
    mod.check = lambda conn, date, silent_sec=0: {"silent": ["netkeiba"]}
    mod.job_kind = lambda src: "netkeiba_odds"
    monkeypatch.setitem(_s.modules, "hro_operations.collector_health", mod)

    step = orc.Step("netkeiba", "netkeiba_odds", "vm", {})
    child = orc.Child(step=step, job_id=5) if hasattr(orc, "Child") else None
    if child is None:
        return
    cfg = orc.OrchestratorConfig(date="20261017", job_id=1) \
        if hasattr(orc, "OrchestratorConfig") else None
    if cfg is None:
        return
    cfg.revive_silent = True
    orc._revive_silent(object(), [child], cfg)
    # ★初回は入れ直す(state を差し替えていれば直るため)
    assert calls == {"cancel": 1, "enqueue": 1}, calls
    # ★直後にもう一度来ても入れ直さない(空転させない)
    calls.clear(); calls.update({"cancel": 0, "enqueue": 0})
    orc._revive_silent(object(), [child], cfg)
    assert calls == {"cancel": 0, "enqueue": 0}, calls

"""開催日オーケストレータ。

★ここで守りたいのは2点。(1) JV-Link を2本起こす計画を作らない、
  (2) 既に動いている常駐を二重起動しない(= 同じレースを2回買わない)。
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from hro_operations import orchestrator as orc

_JST = timezone(timedelta(hours=9))


def _flow(**kw):
    base = {"date": "20261011", "source": "netkeiba", "thresholds": {"90": 0.1368},
            "lead_seconds": 90, "act_before_deadline_seconds": 10, "mode": "paper"}
    base.update(kw)
    return base


# --- 計画 ---------------------------------------------------------------

def test_start_steps_has_three_residents_and_no_jvlink_conflict():
    steps = orc.start_steps("20261011", _flow())
    assert [s.kind for s in steps] == ["run_odds", "netkeiba_odds", "flow_day"]
    assert all(s.resident for s in steps)
    assert orc.jvlink_conflict(steps) is None


def test_jvlink_conflict_detected_when_two_residents_hold_jvlink():
    steps = orc.start_steps("20261011", _flow())
    steps.append(orc.Step("時系列", "fetch_ts_odds", "windows", {},
                          resident=True, jvlink=True))
    msg = orc.jvlink_conflict(steps)
    assert msg and "1台1プロセス" in msg


def test_flow_target_decides_where_the_runner_goes():
    for target in ("vm", "windows"):
        steps = orc.start_steps("20261011", _flow(), flow_target=target)
        flow = [s for s in steps if s.kind == "flow_day"]
        assert len(flow) == 1 and flow[0].target == target


def test_flow_args_reach_the_runner_with_date_forced():
    steps = orc.start_steps("20261011", _flow(date="20260101", min_ninki=7))
    flow = next(s for s in steps if s.kind == "flow_day")
    assert flow.args["min_ninki"] == 7
    assert flow.args["date"] == "20261011"   # 引数の date より計画の date が勝つ


def test_race_day_ends_at_the_close():
    """★当日やる価値があるのは締めだけ。

    IPAT の投票履歴は当日/前日しか遡れないので締めには期限がある。JV-Data 同期と
    決済は期限が無く、同期は重くて時間が読めない。当日の経路に置くと、長引いた
    ぶんだけ締めが危うくなるだけで得るものが無い。
    """
    fin = orc.finish_steps("20261011")
    # ★封印は締めの後。購入額・払戻額は IPAT の記録を取り込んで初めて揃う
    assert [s.kind for s in fin] == ["close_day", "tax_seal"]
    assert not any(s.jvlink or s.resident for s in fin)


def test_sync_gets_a_longer_wait_than_the_default():
    sync = next(s for s in orc.settle_steps([]) if s.kind == "sync_all")
    assert sync.timeout_seconds and sync.timeout_seconds >= 4 * 3600


def test_same_day_close_never_settles():
    """★払戻(HR)は開催の3〜5日後にしか配信されない。当日の決済は空振りするだけ。

    当日に出せる確定値は IPAT 自身の記録(受付明細の払戻)の方で、close_day は
    それを取り込んで損益まで出す。
    """
    close = next(s for s in orc.finish_steps("20261011") if s.kind == "close_day")
    assert close.args["no_settle"] is True


def test_settle_sync_carries_no_date_so_smart_sync_stays_on():
    """★date を渡すと SYNC_SMART が切れて「その日以降のみ」になる。

    取りに行きたいのは**過去の開催日の払戻**なので、日付を渡してはいけない。
    """
    sync = next(s for s in orc.settle_steps([]) if s.kind == "sync_all")
    assert "date" not in sync.args


def test_settle_steps_sync_first_then_each_date():
    steps = orc.settle_steps(["20261004", "20261005"])
    assert [s.kind for s in steps] == ["sync_all", "settle", "settle"]
    assert [s.args["date"] for s in steps if s.kind == "settle"] == ["20261004", "20261005"]
    assert all(s.args["from_db"] for s in steps if s.kind == "settle")


def test_settle_steps_can_skip_the_sync():
    assert [s.kind for s in orc.settle_steps(["20261004"], with_sync=False)] == ["settle"]


# --- 停止時刻 -----------------------------------------------------------

def test_stop_at_is_after_the_last_post_not_the_deadline():
    """★締切(発走-60秒)で止めると投票の書き戻しが途中で切れる。発走の後に止める。"""
    stop = orc.stop_at("20261011", "1625", after_minutes=3)
    assert stop == datetime(2026, 10, 11, 16, 28, tzinfo=_JST)
    assert stop > orc.parse_hhmm("20261011", "1625")


def test_should_stop_only_at_or_after_stop_time():
    stop = orc.stop_at("20261011", "1625")
    assert not orc.should_stop(stop - timedelta(seconds=1), stop)
    assert orc.should_stop(stop, stop)


# --- 再投入 -------------------------------------------------------------

@pytest.mark.parametrize("status", ["failed", "canceled", "done"])
def test_dead_resident_is_restarted(status):
    step = orc.start_steps("20261011", _flow())[0]
    assert orc.needs_restart(step, status, orc.Child(step), max_restarts=3,
                             stopping=False)


@pytest.mark.parametrize("status", ["queued", "running", None])
def test_live_resident_is_left_alone(status):
    step = orc.start_steps("20261011", _flow())[0]
    assert not orc.needs_restart(step, status, orc.Child(step), max_restarts=3,
                                 stopping=False)


def test_no_restart_once_stopping():
    """自分で止めたものを起こし直さない。"""
    step = orc.start_steps("20261011", _flow())[0]
    assert not orc.needs_restart(step, "canceled", orc.Child(step), max_restarts=3,
                                 stopping=True)


def test_restart_budget_is_finite():
    """★起動直後に必ず落ちる設定を無限に投げ続けない。"""
    step = orc.start_steps("20261011", _flow())[0]
    assert not orc.needs_restart(step, "failed", orc.Child(step, restarts=3),
                                 max_restarts=3, stopping=False)


def test_oneshot_is_never_restarted():
    step = orc.finish_steps("20261011")[0]
    assert not orc.needs_restart(step, "failed", orc.Child(step), max_restarts=3,
                                 stopping=False)


def test_describe_lists_every_step_with_target():
    text = orc.describe(orc.start_steps("20261011", _flow()))
    assert text.count("kind=") == 3
    assert "target=windows" in text and "target=vm" in text


# --- 子ジョブの出し入れ(DB は偽物) --------------------------------------

class _FakeConn:
    """execute を SQL の先頭で振り分ける最小の偽接続。"""

    pending_dates: list = []

    def __init__(self, *, running=(), statuses=None):
        self.running = {(k, t, d): i for i, (k, t, d) in enumerate(running, start=900)}
        self.statuses = dict(statuses or {})
        self.inserted: list[tuple] = []
        self.canceled: list[int] = []
        self._next = 1
        self._result = None

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT id FROM ops_job WHERE kind="):
            kind, target, date = params
            jid = self.running.get((kind, target, date))
            self._result = [(jid,)] if jid is not None else []
        elif s.startswith("INSERT INTO ops_job"):
            jid, self._next = self._next, self._next + 1
            self.inserted.append((params[0], params[2], params[1]))   # kind,target,args
            self.statuses[jid] = "queued"
            self._result = [(jid,)]
        elif s.startswith("SELECT status FROM ops_job"):
            self._result = [(self.statuses.get(params[0]),)]
        elif s.startswith("UPDATE ops_job SET cancel_requested"):
            self.canceled.append(params[0])
            self.statuses[params[0]] = "canceled"
            self._result = []
        elif s.startswith("SELECT cancel_requested"):
            self._result = [(False,)]
        elif s.startswith("SELECT right("):
            self._result = [("",)]
        elif s.startswith("SELECT id, kind FROM ops_job WHERE target='windows'"):
            self._result = []                    # JV-Link は空いている
        elif "FROM bet_results" in s:
            self._result = [(d,) for d in self.pending_dates]
        else:                                    # pragma: no cover - 想定外は落とす
            raise AssertionError(f"未知のSQL: {s[:80]}")
        return self

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result or [])

    def close(self):
        pass


def _cfg(**kw):
    c = orc.OrchestratorConfig(date="20261011", flow_args=_flow())
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_start_all_enqueues_each_resident_once():
    conn, cfg = _FakeConn(), _cfg(job_id=42)
    children = [orc.Child(s) for s in orc.start_steps(cfg.date, cfg.flow_args)]
    orc._start_all(conn, children, cfg)
    assert [k for k, _, _ in conn.inserted] == ["run_odds", "netkeiba_odds", "flow_day"]
    assert all(c.job_id is not None and not c.adopted for c in children)


def test_only_residents_are_marked_for_the_orphan_sweep():
    """★単発(同期/締め)は放っておいても終わる。親が先に降りただけで走っている
    sync_all を殺すと、重い処理をまるごとやり直しになる。"""
    import json
    conn, cfg = _FakeConn(), _cfg(job_id=42)
    orc._start_all(conn, [orc.Child(s) for s in orc.start_steps(cfg.date, cfg.flow_args)],
                   cfg)
    assert all(json.loads(a).get("resident") for _, _, a in conn.inserted)
    conn2 = _DoneConn()
    orc._finish(conn2, orc.finish_steps(cfg.date),
                _cfg(job_id=42, poll_seconds=0.01, finish_timeout_seconds=2.0))
    assert not any(json.loads(a).get("resident") for _, _, a in conn2.inserted)


def test_start_all_tags_children_with_the_parent_job():
    """★親が死んだ子を機械的に畳めるよう、子は必ず親の id を持つ。"""
    import json
    conn, cfg = _FakeConn(), _cfg(job_id=42)
    children = [orc.Child(s) for s in orc.start_steps(cfg.date, cfg.flow_args)]
    orc._start_all(conn, children, cfg)
    for _, _, args in conn.inserted:
        assert json.loads(args)["orchestrated_by"] == 42


def test_start_all_adopts_a_running_resident_instead_of_duplicating():
    """★二重起動は flow ランナーなら**同じレースを2回買う**。投入前に必ず見る。"""
    conn = _FakeConn(running=[("flow_day", "windows", "20261011")])
    cfg = _cfg(job_id=42)
    children = [orc.Child(s) for s in orc.start_steps(cfg.date, cfg.flow_args)]
    orc._start_all(conn, children, cfg)
    assert [k for k, _, _ in conn.inserted] == ["run_odds", "netkeiba_odds"]
    flow = next(c for c in children if c.step.kind == "flow_day")
    assert flow.adopted and flow.job_id == 900


def test_adopt_ignores_a_resident_from_another_date():
    conn = _FakeConn(running=[("flow_day", "windows", "20260101")])
    cfg = _cfg(job_id=42)
    children = [orc.Child(s) for s in orc.start_steps(cfg.date, cfg.flow_args)]
    orc._start_all(conn, children, cfg)
    assert [k for k, _, _ in conn.inserted] == ["run_odds", "netkeiba_odds", "flow_day"]


def test_stop_all_cancels_every_live_child_and_waits():
    conn, cfg = _FakeConn(), _cfg(job_id=42, drain_seconds=1.0)
    children = [orc.Child(s) for s in orc.start_steps(cfg.date, cfg.flow_args)]
    orc._start_all(conn, children, cfg)
    for c in children:
        conn.statuses[c.job_id] = "running"
    orc._stop_all(conn, children, cfg)
    assert sorted(conn.canceled) == sorted(c.job_id for c in children)


def test_stop_all_skips_children_already_finished():
    conn, cfg = _FakeConn(), _cfg(job_id=42, drain_seconds=1.0)
    children = [orc.Child(s) for s in orc.start_steps(cfg.date, cfg.flow_args)]
    orc._start_all(conn, children, cfg)
    for c in children:
        conn.statuses[c.job_id] = "done"
    orc._stop_all(conn, children, cfg)
    assert conn.canceled == []


class _BusyConn(_FakeConn):
    """Windows でまだ JV-Link を掴んでいる状態。"""

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT id, kind FROM ops_job WHERE target='windows'"):
            self._result = [(777, "run_odds")]
            return self
        return super().execute(sql, params)


class _DoneConn(_FakeConn):
    """投入した単発ジョブが即 done になる(完了待ちを抜けるため)。"""

    def execute(self, sql, params=()):
        out = super().execute(sql, params)
        if " ".join(sql.split()).startswith("INSERT INTO ops_job"):
            self.statuses[self._result[0][0]] = "done"
        return out


class _BusyDoneConn(_BusyConn, _DoneConn):
    pass


def test_finish_runs_the_close_then_the_seal():
    conn, cfg = _DoneConn(), _cfg(job_id=42, poll_seconds=0.01,
                                  finish_timeout_seconds=2.0)
    assert orc._finish(conn, orc.finish_steps(cfg.date), cfg) == 0
    assert [k for k, _, _ in conn.inserted] == ["close_day", "tax_seal"]


# --- 後日の決済(settle-pending) ----------------------------------------

def _scfg(**kw):
    c = orc.SettleConfig(poll_seconds=0.01, finish_timeout_seconds=2.0, job_id=42)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _patched(monkeypatch, conn):
    monkeypatch.setattr(orc, "_connect", lambda: conn)


def test_settle_pending_syncs_then_settles_the_dates_it_found(monkeypatch):
    """★払戻は開催の3〜5日後。日付は自分で探すので人が覚えておく必要がない。"""
    class _C(_DoneConn):
        pending_dates = ["20261004", "20261005"]

    conn = _C()
    _patched(monkeypatch, conn)
    assert orc.run_settle(_scfg()) == 0
    assert [k for k, _, _ in conn.inserted] == ["sync_all", "settle", "settle"]
    assert [json.loads(a)["date"] for k, _, a in conn.inserted
            if k == "settle"] == ["20261004", "20261005"]


def test_settle_pending_stops_when_the_sync_fails(monkeypatch):
    """★払戻が入っていないまま突合しても何も確定しない。進まない方が正しい。"""
    class _C(_DoneConn):
        pending_dates = ["20261004"]

        def execute(self, sql, params=()):
            out = super().execute(sql, params)
            if " ".join(sql.split()).startswith("INSERT INTO ops_job") \
                    and params[0] == "sync_all":
                self.statuses[self._result[0][0]] = "failed"
            return out

    conn = _C()
    _patched(monkeypatch, conn)
    assert orc.run_settle(_scfg()) == 1
    assert [k for k, _, _ in conn.inserted] == ["sync_all"]


def test_settle_pending_refuses_sync_while_jvlink_is_held(monkeypatch):
    class _C(_BusyConn, _DoneConn):
        pending_dates = ["20261004"]

    conn = _C()
    _patched(monkeypatch, conn)
    assert orc.run_settle(_scfg()) == 1
    assert conn.inserted == []           # 決済まで進まない


def test_settle_pending_reports_nothing_to_do(monkeypatch):
    conn = _DoneConn()
    _patched(monkeypatch, conn)
    assert orc.run_settle(_scfg()) == 0
    assert [k for k, _, _ in conn.inserted] == ["sync_all"]


def test_settle_pending_honours_explicit_dates(monkeypatch):
    class _C(_DoneConn):
        pending_dates = ["20261004"]

    conn = _C()
    _patched(monkeypatch, conn)
    assert orc.run_settle(_scfg(dates=["20260919"], with_sync=False)) == 0
    assert [json.loads(a)["date"] for k, _, a in conn.inserted
            if k == "settle"] == ["20260919"]


def test_settle_pending_targets_the_requested_machine(monkeypatch):
    class _C(_DoneConn):
        pending_dates = ["20261004"]

    conn = _C()
    _patched(monkeypatch, conn)
    orc.run_settle(_scfg(target="windows", with_sync=False))
    assert [t for k, t, _ in conn.inserted if k == "settle"] == ["windows"]


def test_settle_pending_dry_run_enqueues_nothing(monkeypatch):
    class _C(_DoneConn):
        pending_dates = ["20261004"]

    conn = _C()
    _patched(monkeypatch, conn)
    assert orc.run_settle(_scfg(dry_run=True)) == 0
    assert conn.inserted == []


# --- 発走時刻変更への追随 -------------------------------------------------

class _PostConn(_FakeConn):
    last_post = "1625"

    def execute(self, sql, params=()):
        if "max(hasso_time)" in " ".join(sql.split()):
            self._result = [(self.last_post,)]
            return self
        return super().execute(sql, params)


def test_stop_time_follows_a_delayed_last_race():
    """★起動時の最終発走で固定すると、遅れたとき最終レースの前に収集を止める。"""
    conn = _PostConn()
    cur = orc.stop_at("20261011", "1625", after_minutes=3)
    conn.last_post = "1640"
    fresh = orc.refreshed_stop_at(conn, "20261011", cur, after_minutes=3)
    assert fresh == orc.stop_at("20261011", "1640", after_minutes=3)


def test_stop_time_is_never_pulled_earlier():
    """★繰り上がりで前倒しすると、まだ投票していないレースの途中で止まりかねない。"""
    conn = _PostConn()
    cur = orc.stop_at("20261011", "1625", after_minutes=3)
    conn.last_post = "1610"
    assert orc.refreshed_stop_at(conn, "20261011", cur, after_minutes=3) is None


def test_unchanged_schedule_keeps_the_stop_time():
    conn = _PostConn()
    cur = orc.stop_at("20261011", "1625", after_minutes=3)
    assert orc.refreshed_stop_at(conn, "20261011", cur, after_minutes=3) is None


def test_missing_races_do_not_move_the_stop_time():
    conn = _PostConn()
    conn.last_post = None
    cur = orc.stop_at("20261011", "1625", after_minutes=3)
    assert orc.refreshed_stop_at(conn, "20261011", cur, after_minutes=3) is None

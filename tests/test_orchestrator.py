"""開催日オーケストレータ。

★ここで守りたいのは2点。(1) JV-Link を2本起こす計画を作らない、
  (2) 既に動いている常駐を二重起動しない(= 同じレースを2回買わない)。
"""

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


def test_finish_steps_sync_before_close():
    fin = orc.finish_steps("20261011")
    assert [s.kind for s in fin] == ["sync_all", "close_day"]
    assert fin[0].jvlink and not fin[1].jvlink
    assert not any(s.resident for s in fin)


def test_same_day_close_never_settles():
    """★払戻(HR)は開催の3〜5日後にしか配信されない。当日の決済は空振りするだけ。

    当日に出せる確定値は IPAT 自身の記録(受付明細の払戻)の方で、close_day は
    それを取り込んで損益まで出す。
    """
    close = next(s for s in orc.finish_steps("20261011") if s.kind == "close_day")
    assert close.args["no_settle"] is True


def test_finish_sync_carries_no_date_so_smart_sync_stays_on():
    """★date を渡すと SYNC_SMART が切れて「その日以降のみ」になる。

    取りに行きたいのは**過去の開催日の払戻**なので、日付を渡してはいけない。
    """
    sync = next(s for s in orc.finish_steps("20261011") if s.kind == "sync_all")
    assert "date" not in sync.args


def test_finish_steps_appends_settlement_for_arrived_payouts():
    fin = orc.finish_steps("20261011", settle_dates=["20261004", "20261005"])
    assert [s.kind for s in fin] == ["sync_all", "close_day", "settle", "settle"]
    assert [s.args["date"] for s in fin if s.kind == "settle"] == ["20261004", "20261005"]
    assert all(s.args["from_db"] for s in fin if s.kind == "settle")


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


def test_finish_runs_sync_then_close_in_order():
    conn, cfg = _DoneConn(), _cfg(job_id=42, poll_seconds=0.01,
                                  finish_timeout_seconds=2.0)
    assert orc._finish(conn, orc.finish_steps(cfg.date), cfg) == 0
    assert [k for k, _, _ in conn.inserted] == ["sync_all", "close_day"]


def test_finish_refuses_sync_while_jvlink_is_still_held():
    """★run_odds が残っているのに sync_all を投げると COM エラーで収集が死ぬ。"""
    conn, cfg = _BusyDoneConn(), _cfg(job_id=42, poll_seconds=0.01,
                                      finish_timeout_seconds=2.0)
    rc = orc._finish(conn, orc.finish_steps(cfg.date), cfg)
    assert [k for k, _, _ in conn.inserted] == ["close_day"]   # sync は見送り
    assert rc == 1


def test_failed_sync_skips_the_deferred_settlement():
    """★同期に失敗したら、払戻が入っていないかもしれないので決済へ進まない。

    当日の締め(IPAT の記録)はそのまま続ける。そちらは JV-Data に依らない。
    """
    import json

    class _FailSync(_DoneConn):
        pending_dates = ["20261004"]

        def execute(self, sql, params=()):
            out = super().execute(sql, params)
            if " ".join(sql.split()).startswith("INSERT INTO ops_job"):
                if params[0] == "sync_all":
                    self.statuses[self._result[0][0]] = "failed"
            return out

    conn, cfg = _FailSync(), _cfg(job_id=42, poll_seconds=0.01,
                                  finish_timeout_seconds=2.0)
    assert orc._finish(conn, orc.finish_steps(cfg.date), cfg) == 1
    kinds = [k for k, _, _ in conn.inserted]
    assert kinds == ["sync_all", "close_day"]            # settle へ進まない
    close = next(a for k, _, a in conn.inserted if k == "close_day")
    assert json.loads(close)["no_settle"] is True


def test_arrived_payouts_are_settled_after_the_sync():
    """★「今日の分を今日締める」のではなく「届いた分をその日に片付ける」。

    払戻は開催の3〜5日後なので、開催日ごとに人が思い出して押す必要をなくす。
    """
    class _WithPending(_DoneConn):
        pending_dates = ["20261004", "20261005"]

    conn, cfg = _WithPending(), _cfg(job_id=42, poll_seconds=0.01,
                                     finish_timeout_seconds=2.0)
    assert orc._finish(conn, orc.finish_steps(cfg.date), cfg) == 0
    assert [k for k, _, _ in conn.inserted] == ["sync_all", "close_day",
                                                "settle", "settle"]


def test_no_settle_disables_the_deferred_settlement():
    class _WithPending(_DoneConn):
        pending_dates = ["20261004"]

    conn, cfg = _WithPending(), _cfg(job_id=42, poll_seconds=0.01,
                                     finish_timeout_seconds=2.0,
                                     settle_pending=False)
    assert orc._finish(conn, orc.finish_steps(cfg.date), cfg) == 0
    assert [k for k, _, _ in conn.inserted] == ["sync_all", "close_day"]


def test_settlement_targets_the_close_machine():
    class _WithPending(_DoneConn):
        pending_dates = ["20261004"]

    conn, cfg = _WithPending(), _cfg(job_id=42, poll_seconds=0.01,
                                     finish_timeout_seconds=2.0,
                                     close_target="windows")
    orc._finish(conn, orc.finish_steps(cfg.date, close_target="windows"), cfg)
    assert [t for k, t, _ in conn.inserted if k == "settle"] == ["windows"]

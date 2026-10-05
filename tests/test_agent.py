

def test_flow_day_params_keep_netkeiba_source():
    """★以前は `"ts" if src=="ts" else "sokuho"` と書いており、netkeiba を指定しても
    黙って sokuho で走った。別ソースの結果を netkeiba の成績として記録してしまう。"""
    from hro_operations.agent import _flow_day_params

    assert _flow_day_params({"source": "netkeiba"})["source"] == "netkeiba"
    assert _flow_day_params({"source": "ts"})["source"] == "ts"
    assert _flow_day_params({"source": "sokuho"})["source"] == "sokuho"
    assert _flow_day_params({"source": "でたらめ"})["source"] == "sokuho"
    assert _flow_day_params({})["source"] == "sokuho"


def test_netkeiba_odds_job_is_vm_only_and_guards_the_window():
    """★netkeiba は JV-Link を使わないので VM で動かし、Windows の1プロセス制約とは
    競合しない。★--within-minutes を 8 より下げると起点(発走6分前)が取れなくなり、
    そのレースが丸ごと見送りになるので下限で守る。"""
    from hro_operations.agent import _COMMANDS, _b_netkeiba_odds

    assert "netkeiba_odds" in _COMMANDS["vm"]
    assert "netkeiba_odds" not in _COMMANDS["windows"]

    cmd, cwd, env = _b_netkeiba_odds({"date": "20260927", "within_minutes": 3})
    assert "netkeiba-odds" in cmd
    assert cmd[cmd.index("--within-minutes") + 1] == "8"      # 下限で守る
    assert cmd[cmd.index("--date") + 1] == "20260927"
    assert cwd.endswith("hro-synchronizer")

    _c, _w, env2 = _b_netkeiba_odds({"state": "/home/azureuser/.netkeiba_state.json"})
    assert env2["NETKEIBA_STATE"].endswith(".netkeiba_state.json")


def test_child_env_does_not_leak_the_agents_virtualenv(monkeypatch):
    """★agent は hro-operations の venv で動く。os.environ をそのまま子へ渡すと
    VIRTUAL_ENV が残り、`poetry run` が「既に仮想環境が有効」と判断して
    **別パッケージを hro-operations の venv で実行**する。
    2026-09-27 に netkeiba_odds(hro-synchronizer)で ModuleNotFoundError: yaml として露見。"""
    import os

    from hro_operations.agent import _child_env

    monkeypatch.setenv("VIRTUAL_ENV", "/home/u/.venvs/ops")
    monkeypatch.setenv("POETRY_ACTIVE", "1")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setenv("PATH", os.pathsep.join(
        ["/home/u/.venvs/ops/bin", "/usr/local/bin", "/usr/bin"]))

    env = _child_env({"ODDS_SPEC": "0B30"})
    assert "VIRTUAL_ENV" not in env and "POETRY_ACTIVE" not in env
    assert "PYTHONPATH" not in env
    assert "/home/u/.venvs/ops/bin" not in env["PATH"].split(os.pathsep)
    assert "/usr/bin" in env["PATH"].split(os.pathsep)   # 他は残す
    assert env["ODDS_SPEC"] == "0B30"                    # ジョブ固有の env は通す


def test_threshold_grid_check_is_source_aware():
    """★60秒格子は JV-Link(発表時刻が分刻み)の制約。netkeiba は実時刻なので 75 が正しい。
    信号源を見ずに弾いていたため、正しい netkeiba 設定でランナーが起動できなかった
    (2026-09-27 に実害。フロントだけ直して agent を直し忘れた)。"""
    import pytest

    from hro_operations.agent import _flow_day_params, _thresholds

    assert _thresholds({"75": 0.1533}, "netkeiba") == {75: 0.1533}
    assert _thresholds({"120": 0.1583}, "sokuho") == {120: 0.1583}
    with pytest.raises(ValueError, match="60秒の倍数"):
        _thresholds({"75": 0.1533}, "sokuho")

    p = _flow_day_params({"source": "netkeiba", "thresholds": '{"75": 0.1533}',
                          "lead_seconds": 75})
    assert p["source"] == "netkeiba" and p["thresholds"] == {75: 0.1533}
    assert p["flow_lead"] == 75


def test_cancel_is_checked_outside_the_output_loop():
    """★キャンセルは**心拍スレッド**で見る必要がある。出力ループ
    (`for line in proc.stdout`)は readline でブロックするので、無言で待機するジョブ
    (flow ランナーはレース間で数分沈黙する)では**次に何か出力されるまで効かない**。
    2026-09-27 に「中止を押しても消えない」として実害。"""
    import inspect

    from hro_operations import agent

    src = inspect.getsource(agent._run_job)
    hb = src[src.index("def _hb_loop"):src.index("hb_thread = threading.Thread")]
    assert "_canceled(" in hb, "心拍スレッドでキャンセルを見ていない"
    assert "_kill(" in hb, "心拍スレッドからプロセスを止めていない"
    # 出力ループ側では DB を見に行かない(ブロック中は到達しないので意味が無い)
    # ★コメント中にも同じ文言があるので**最後の出現**を使う
    loop = src[src.rindex("for line in proc.stdout"):src.index("code = proc.wait()")]
    assert "_canceled(" not in loop
    assert "cancel_flag.is_set()" in loop


def test_kill_falls_back_when_killpg_is_unavailable():
    """POSIX で killpg が使えない場合のフォールバック(Windows は別テスト)。"""
    from hro_operations.agent import _kill

    class P:
        pid = -1                      # os.getpgid が失敗する
        terminated = False

        def terminate(self):
            P.terminated = True

    _kill([P()])
    assert P.terminated
    _kill([])                         # 起動前でも落ちないこと


def test_max_per_race_is_passed_to_run_day():
    """★いまの executor は注文ごとに「投票→確認→送信→受付」を1周するので、
    同一レースで複数選ばれると2件目以降が締切を超えて捨てられる
    (2026-10-03 に実害: 3件中1件のみ成立)。上限を渡せること。"""
    from hro_operations.agent import _b_flow_day_windows, _flow_day_params

    assert _flow_day_params({"max_per_race": 1})["max_per_race"] == 1
    assert _flow_day_params({})["max_per_race"] == 0        # 既定は無制限

    cmd, _cwd, _env = _b_flow_day_windows(
        {"source": "netkeiba", "thresholds": '{"90": 0.15}', "lead_seconds": 90,
         "max_per_race": 1, "mode": "paper"})
    i = cmd.index("--flow-max-per-race")
    assert cmd[i + 1] == "1"


def test_kill_on_windows_takes_the_whole_process_tree(monkeypatch):
    """★Windows で terminate() だけでは**直下の子しか死なない**。

    起動は `poetry run hro-ops run-day` なので、プロセス木は
    poetry.exe → python.exe → node.exe(Playwright) → ブラウザ。
    terminate() で死ぬのは poetry.exe だけで、ブラウザが残り続ける。
    start_new_session=True も Windows では無視されるので、
    「プロセスグループにしてある」という前提は成り立っていなかった。
    """
    import hro_operations.agent as agent

    calls = []
    monkeypatch.setattr(agent.os, "name", "nt")
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))

    class P:
        pid = 4242
        terminated = False

        def terminate(self):
            P.terminated = True

    agent._kill([P()])
    assert calls == [["taskkill", "/T", "/F", "/PID", "4242"]], calls
    assert not P.terminated, "Windows で terminate() に落ちている(子孫が残る)"


class _FakeConn:
    """execute した SQL とパラメータを記録するだけの接続。"""

    def __init__(self, returning=()):
        self.sql = []
        self._returning = list(returning)

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        self_ = self

        class _Cur:
            def fetchall(self):
                return self_._returning

            def fetchone(self):
                return self_._returning[0] if self_._returning else None

        return _Cur()

    def commit(self):
        pass


def test_reclaim_at_startup_ignores_heartbeat_age():
    """★エージェントが今起動した以上、自分名義の running は存在し得ない。

    2026-10-03 に Windows Update が 13:42 と 13:51 の2回 VM を再起動し、当日の
    runner が消えたのに ops_job の行は running のまま残った。heartbeat_at は
    書いていたのに**読む側が無かった**のが原因。
    """
    from hro_operations.agent import _reclaim

    c = _FakeConn(returning=[(1,), (2,)])
    n = _reclaim(c, "win1", stale_sec=180, startup=True)
    sql, params = c.sql[0]
    assert n == 2
    assert "status='failed'" in sql and "status='running'" in sql
    assert "heartbeat_at" not in sql, "起動時の一掃で心拍の新しさを見てはいけない"
    assert params[1:] == ("win1",)


def test_reclaim_periodic_only_takes_stale_rows():
    """走行中のジョブを巻き込まないこと(心拍が古い行だけ)。"""
    from hro_operations.agent import _reclaim

    c = _FakeConn(returning=[])
    _reclaim(c, "win1", stale_sec=180)
    sql, params = c.sql[0]
    assert "heartbeat_at < now() - make_interval(secs => %s)" in sql
    assert params[1:] == ("win1", 180)


def test_agent_loop_actually_calls_reclaim():
    """★heartbeat_at を書くだけで誰も読まない、を二度とやらない。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "agent.py").read_text(encoding="utf-8")
    loop = src[src.index("    while True:"):]
    assert "_reclaim(" in loop, "エージェントのループが回収を呼んでいない"
    assert "startup=True" in loop, "起動時の一掃が無い"


# -- 券種と選別条件が実機まで届くこと ------------------------------------------- #
# ★2026-10-03 発覚: agent はこれらを run-day に渡しておらず、live は常に
#   run-day の既定(複勝・人気絞り無し・頭数下限無し)で走っていた。
#   単勝×人気7+ は OOS 1.6690 で複勝(1.0768)を大きく上回るのに使えておらず、
#   「複勝は8頭以上に限る」という運用上の指示も実機に届いていなかった。
_SEL = {"date": "20261004", "source": "netkeiba", "lead_seconds": 90,
        "flow_minutes": 6, "act_before_deadline_seconds": 25,
        "thresholds": {90: 0.1368}, "flat_amount": 1000,
        "bet_type": "tan", "min_ninki": 7, "min_horses": 0}


def test_windows_cmd_carries_bet_type_and_selection():
    from hro_operations.agent import _b_flow_day_windows

    cmd, _cwd, _env = _b_flow_day_windows(dict(_SEL))
    pairs = dict(zip(cmd, cmd[1:]))
    assert pairs["--flow-bet-type"] == "tan"
    assert pairs["--flow-min-ninki"] == "7"
    assert pairs["--flow-min-horses"] == "0"


def test_vm_env_carries_bet_type_and_selection():
    from hro_operations.agent import _b_flow_day

    _cmd, _cwd, env = _b_flow_day(dict(_SEL))
    assert env["BET_TYPE"] == "tan"
    assert env["MIN_NINKI"] == "7"
    assert env["MIN_HORSES"] == "0"


def test_bet_type_defaults_to_fuku_and_rejects_junk():
    """知らない値で黙って単勝にならないこと(お金が動く側の既定は保守的に)。"""
    from hro_operations.agent import _b_flow_day_windows

    for given in (None, "", "win", "たんしょう"):
        a = dict(_SEL)
        a["bet_type"] = given
        cmd, _c, _e = _b_flow_day_windows(a)
        assert dict(zip(cmd, cmd[1:]))["--flow-bet-type"] == "fuku", given


def test_flow_day_sh_forwards_the_same_knobs():
    """VM 側のシェルも同じ引数を渡すこと(片方だけ直すと信号源と同じ事故になる)。"""
    from pathlib import Path

    sh = (Path(__file__).resolve().parents[1]
          / "scripts" / "flow_day.sh").read_text(encoding="utf-8")
    for flag in ("--flow-bet-type", "--flow-min-ninki", "--flow-max-ninki",
                 "--flow-min-horses"):
        assert flag in sh, flag


def test_import_results_is_available_where_run_day_runs():
    """★results_<date>.jsonl は **run-day を回した機** にできる。

    flow_day を Windows へ移した時点で、VM だけに登録されていた import_results は
    成功しようがなくなっていた(2026-10-04 発覚: ops_job 117/137 が
    「ファイルが見つかりません」で failed のまま放置されていた)。
    flow_day がある機には import_results も無ければならない。
    """
    from hro_operations.agent import _COMMANDS

    for host, kinds in _COMMANDS.items():
        if "flow_day" in kinds:
            assert "import_results" in kinds, f"{host} に import_results が無い"


def test_umatan_and_partners_reach_the_runner():
    """★馬単も券種として実機まで届くこと。知らない値は fuku に落とす。"""
    from hro_operations.agent import _b_flow_day, _b_flow_day_windows

    a = dict(_SEL); a["bet_type"] = "umatan"; a["partners"] = 3
    cmd, _c, _e = _b_flow_day_windows(a)
    pairs = dict(zip(cmd, cmd[1:]))
    assert pairs["--flow-bet-type"] == "umatan"
    assert pairs["--flow-partners"] == "3"

    _c2, _cw, env = _b_flow_day(a)
    assert env["BET_TYPE"] == "umatan" and env["PARTNERS"] == "3"

    a["bet_type"] = "馬単"          # 知らない値
    cmd, _c, _e = _b_flow_day_windows(a)
    assert dict(zip(cmd, cmd[1:]))["--flow-bet-type"] == "fuku"


def test_run_day_accepts_umatan():
    """CLI と DayConfig が馬単を受けること(ここが抜けると信号だけ出て買えない)。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "hro_operations"
    cli = (root / "__main__.py").read_text(encoding="utf-8")
    assert '"fuku", "tan", "umatan"' in cli
    assert "--flow-partners" in cli
    assert "flow_partners=getattr(args" in cli

    rd = (root / "race_day.py").read_text(encoding="utf-8")
    assert "flow_partners: int = 3" in rd
    assert "partners=cfg.flow_partners" in rd

    sh = (root.parent / "scripts" / "flow_day.sh").read_text(encoding="utf-8")
    assert "--flow-partners" in sh

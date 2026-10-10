"""agent の kind→コマンド写像(flow 戦略まわり)のテスト。DB/JV-Link は使わない。"""

from __future__ import annotations

import pytest

from hro_operations import agent


def test_flow_day_paper_defaults(monkeypatch):
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    cmd, cwd, env = agent._b_flow_day({"date": "20260919"})
    assert cmd == ["bash", "scripts/flow_day.sh"] and cwd.endswith("hro-operations")
    assert env["MODE"] == "paper" and env["FLOW_THRESHOLD"] == "0.2802"
    # 既定は「締切10秒前に投票開始」= 発走-70s(preselect で直前の作業を削ったため)。
    assert env["FLOW_SOURCE"] == "sokuho" and env["LEAD_SECONDS"] == "70"
    assert env["FLOW_LEAD"] == "120"


def test_flow_day_live_requires_confirm_and_limits(monkeypatch):
    """live は UI からでも多重ゲート: confirm_live と 1件/1日上限が無ければ組み立てない。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    with pytest.raises(ValueError):
        agent._b_flow_day({"mode": "live"})
    with pytest.raises(ValueError):
        agent._b_flow_day({"mode": "live", "confirm_live": True, "max_amount_per_order": 1000})
    # 時刻が成立する組でのみ通る(決定 T-180s > 実行 T-90s > 締切 T-60s)。
    _, _, env = agent._b_flow_day({"mode": "live", "confirm_live": True,
                                   "max_amount_per_order": 1000, "max_amount_per_day": 20000,
                                   "lead_seconds": 180, "act_lead_seconds": 90})
    assert env["MODE"] == "live" and env["CONFIRM_LIVE"] == "1"
    assert env["MAX_PER_ORDER"] == "1000" and env["MAX_PER_DAY"] == "20000"


_LIVE = {"mode": "live", "confirm_live": True,
         "max_amount_per_order": 1000, "max_amount_per_day": 20000}


def test_flow_day_live_rejects_order_time_after_deadline(monkeypatch):
    """★実行時刻が締切より後だと live を組み立てない。

    この設定は「1日走りきって0件」という最悪の壊れ方をする(run-day は T-act まで待って
    発注するが、ガードは T-60s を過ぎた発注を捨てる)。開催日は取り返しがつかない。
    """
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    with pytest.raises(ValueError, match="締切"):
        agent._b_flow_day({**_LIVE, "act_lead_seconds": 30, "lead_seconds": 60})


def test_flow_day_live_rejects_snapshot_not_yet_available(monkeypatch):
    """決定に使うスナップショットが実行時刻にまだ無い組み合わせも弾く。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    with pytest.raises(ValueError, match="まだ存在しません"):
        agent._b_flow_day({**_LIVE, "act_lead_seconds": 90, "lead_seconds": 60})


def test_flow_day_paper_keeps_running_but_reports_timing_problem(monkeypatch):
    """paper は計測走行なので、時刻が破綻していても止めず警告だけ返す。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    _, _, env = agent._b_flow_day({"date": "20260926", "act_lead_seconds": 30})
    assert env["MODE"] == "paper" and env["LEAD_SECONDS"] == "30"
    p = agent._flow_day_params({"date": "20260926", "act_lead_seconds": 30})
    assert "締切" in p["timing_problem"]


def test_act_time_is_relative_to_deadline(monkeypatch):
    """運用の基準は「締切の何秒前に投票を開始するか」。締切=発走-60s から逆算する。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    p15 = agent._flow_day_params({"date": "20260926", "act_before_deadline_seconds": 15})
    p30 = agent._flow_day_params({"date": "20260926", "act_before_deadline_seconds": 30})
    assert p15["act_lead"] == 75 and p30["act_lead"] == 90     # 発走基準へ変換
    assert p15["timing_problem"] is None and p30["timing_problem"] is None


def test_threshold_not_transferable_across_source_or_lead(monkeypatch):
    """0.2802 は (ts / 発走-60s) の絶対値。設定が違えば取り直しを促す。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    p = agent._flow_day_params({"date": "20260926"})          # 既定 sokuho/120
    assert "flow-threshold" in p["threshold_note"]
    same = agent._flow_day_params({"date": "20260926", "source": "ts", "lead_seconds": 60})
    assert "threshold_note" not in same                        # 検証と同条件なら黙る
    other = agent._flow_day_params({"date": "20260926", "threshold": 0.31})
    assert "threshold_note" not in other                       # 取り直し済みの値には言わない


def test_flow_day_windows_builds_run_day_without_bash(monkeypatch):
    """Windows 経路は bash に依存せず run-day を直接起動する。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    cmd, cwd, _ = agent._b_flow_day_windows({**_LIVE, "date": "20260926",
                                             "lead_seconds": 180, "act_lead_seconds": 90})
    assert cmd[:4] == ["poetry", "run", "hro-ops", "run-day"] and "bash" not in cmd
    assert "--confirm-live" in cmd and "--ipat-recipe" in cmd
    assert cmd[cmd.index("--lead-seconds") + 1] == "90"
    assert cmd[cmd.index("--deadline-lead-seconds") + 1] == "60"
    assert cwd.endswith("hro-operations")


def test_fetch_ts_odds_resident_limits_scope():
    cmd, cwd, _ = agent._b_fetch_ts_odds({"date": "20260919", "repeat_seconds": 20})
    assert "--repeat-seconds" in cmd and "--within-minutes" in cmd and cwd.endswith("hro-synchronizer")
    cmd1, _, _ = agent._b_fetch_ts_odds({"date": "20260919"})
    assert "--repeat-seconds" not in cmd1


def test_new_kinds_are_registered():
    assert {"flow_day", "flow_check", "import_results", "jrdb_load"} <= set(agent._COMMANDS["vm"])
    assert {"fetch_ts_odds", "env_check"} <= set(agent._COMMANDS["windows"])


def test_run_odds_limits_scope_so_each_race_is_polled_within_a_minute():
    """★絞らないと当日全レースを毎周なめる。2026-09-21 の実測で24レース/1周82秒。
    締切30秒前に 発走-120秒 のスナップを使うには直前にそのレースを取れている必要がある。"""
    cmd, cwd, env = agent._b_run_odds({"date": "20260921"})
    assert cmd[cmd.index("--within-minutes") + 1] == "20"
    assert cmd[cmd.index("--past-minutes") + 1] == "5"
    assert env["ODDS_SPEC"] == "0B30"
    assert cwd.endswith("hro-synchronizer")
    # UI から広げられること
    wide, _, _ = agent._b_run_odds({"date": "20260921", "within_minutes": 45})
    assert wide[wide.index("--within-minutes") + 1] == "45"


def test_flow_day_passes_per_lead_thresholds(monkeypatch):
    """「T-120s が間に合ったレースだけ買う」= 120 だけ渡す。他のリードは run-day が見送る。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    _, _, env = agent._b_flow_day({"date": "20260926", "thresholds": {"120": 0.1631}})
    assert env["FLOW_THRESHOLDS"] == '{"120": 0.1631}'
    cmd, _, _ = agent._b_flow_day_windows({"date": "20260926", "thresholds": {"120": 0.1631}})
    assert cmd[cmd.index("--flow-thresholds") + 1] == '{"120": 0.1631}'


def test_flow_day_rejects_leads_off_the_announcement_grid(monkeypatch):
    """発表時刻は分格子なので、60の倍数以外のリードは実測され得ない=設定ミス。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    with pytest.raises(ValueError, match="60秒の倍数"):
        agent._b_flow_day({"date": "20260926", "thresholds": {"117": 0.1}})
    with pytest.raises(ValueError, match="JSON"):
        agent._b_flow_day({"date": "20260926", "thresholds": "not json"})


def test_run_odds_poll_interval_is_tunable():
    """★到着遅れの一部は自分のサンプリング待ち(平均 周期/2)。対象を絞れば1周1秒未満
    なので、周期を詰めれば無料で数秒縮まる。締切直前の価格を掴めるかに直結する。"""
    _, _, env = agent._b_run_odds({"date": "20260922"})
    assert env["ODDS_POLL_INTERVAL_SEC"] == "3.0"
    _, _, env2 = agent._b_run_odds({"date": "20260922", "poll_interval_sec": 2})
    assert env2["ODDS_POLL_INTERVAL_SEC"] == "2.0"


def test_vote_starts_10s_before_deadline_by_default(monkeypatch):
    """★IPAT の事前準備(preselect)で締切直前に残る作業を馬番と金額だけにしたので、
    投票開始を締切10秒前まで引っ張れる。配信遅れ中央値51秒に対して10秒は大きい
    (T-120s の入手率が 41%→54% 相当)。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    p = agent._flow_day_params({"date": "20260922"})
    assert p["act_lead"] == 70 and p["deadline_lead"] == 60
    assert p["timing_problem"] is None


# --- 開催日オーケストレータ -------------------------------------------------

def _race_day_args(**kw):
    a = {"date": "20261011", "source": "netkeiba", "thresholds": {"90": 0.1368},
         "lead_seconds": 90, "act_before_deadline_seconds": 10, "mode": "paper"}
    a.update(kw)
    return a


def _opt(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_race_day_builds_cli_with_flow_args_as_json(monkeypatch):
    import json
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    cmd, cwd, env = agent._b_race_day(_race_day_args(min_ninki=7,
                                                     bet_type="tan:1000,umatan:100"))
    assert cmd[:4] == ["poetry", "run", "hro-ops", "race-day"]
    assert cwd.endswith("hro-operations")
    fa = json.loads(_opt(cmd, "--flow-args"))
    assert fa["min_ninki"] == 7 and fa["bet_type"] == "tan:1000,umatan:100"
    assert fa["date"] == "20261011"


def test_race_day_defaults_runner_to_windows_and_close_to_vm(monkeypatch):
    """★IPAT は Windows 実績機、締めは JV-Link 不要なので VM(落ちにくい方)。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    cmd, _, _ = agent._b_race_day(_race_day_args())
    assert _opt(cmd, "--flow-target") == "windows"
    assert _opt(cmd, "--close-target") == "vm"


def test_race_day_unknown_target_falls_back(monkeypatch):
    """知らない値で勝手な機に投げない。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    cmd, _, _ = agent._b_race_day(_race_day_args(flow_target="mars",
                                                 close_target="mars"))
    assert _opt(cmd, "--flow-target") == "windows"
    assert _opt(cmd, "--close-target") == "vm"


def test_race_day_validates_flow_settings_at_enqueue(monkeypatch):
    """★1日走って0件を避けるため、リードの矛盾は投入時点で落とす。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    with pytest.raises(ValueError):
        agent._b_race_day(_race_day_args(mode="live", lead_seconds=30,
                                         act_before_deadline_seconds=10,
                                         confirm_live=True,
                                         max_amount_per_order=5000,
                                         max_amount_per_day=20000))


def test_race_day_live_requires_confirm_and_limits(monkeypatch):
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    with pytest.raises(ValueError):
        agent._b_race_day(_race_day_args(mode="live"))


def test_race_day_passes_own_job_id_as_parent(monkeypatch):
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    cmd, _, _ = agent._b_race_day(_race_day_args(job_id=421))
    assert _opt(cmd, "--job-id") == "421"


def test_race_day_flags(monkeypatch):
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    cmd, _, _ = agent._b_race_day(_race_day_args(no_netkeiba=True, dry_run=True))
    assert "--no-netkeiba" in cmd and "--dry-run" in cmd
    assert "--no-run-odds" not in cmd


def test_race_day_does_not_carry_sync_or_settlement(monkeypatch):
    """★同期と決済は開催日のジョブに入れない(払戻は開催の3〜5日後)。"""
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    cmd, _, _ = agent._b_race_day(_race_day_args())
    assert "--no-sync" not in cmd and "--no-settle" not in cmd


def test_settle_pending_finds_its_own_dates():
    """日付を渡さなければ自分で探す(開催日ごとに人が覚えておかなくて済む)。"""
    cmd, cwd, _ = agent._b_settle_pending({})
    assert cmd[:4] == ["poetry", "run", "hro-ops", "settle-pending"]
    assert "--dates" not in cmd
    assert _opt(cmd, "--target") == "vm"
    assert cwd.endswith("hro-operations")


def test_settle_pending_validates_explicit_dates():
    cmd, _, _ = agent._b_settle_pending({"dates": "20261004,20261005"})
    assert _opt(cmd, "--dates") == "20261004,20261005"
    with pytest.raises(ValueError):
        agent._b_settle_pending({"dates": "2026-10-04"})


def test_settle_pending_registered_on_both_servers():
    assert agent._COMMANDS["vm"]["settle_pending"] is agent._b_settle_pending
    assert agent._COMMANDS["windows"]["settle_pending"] is agent._b_settle_pending


def test_race_day_registered_on_both_servers():
    assert agent._COMMANDS["vm"]["race_day"] is agent._b_race_day
    assert agent._COMMANDS["windows"]["race_day"] is agent._b_race_day


def test_settle_from_db_for_a_past_day(monkeypatch):
    """★後日の決済。JSONL は run-day を回した機にしか無いので DB から読む。"""
    cmd, cwd, _ = agent._b_settle({"date": "20261004", "from_db": True, "modes": "live"})
    assert cmd == ["poetry", "run", "hro-buyer", "settle", "--from-db",
                   "--budget-key", "20261004", "--write", "--modes", "live"]
    assert cwd.endswith("hro-operations")


def test_settle_defaults_to_the_jsonl_path(monkeypatch):
    cmd, _, _ = agent._b_settle({"date": "20261004"})
    assert "--results" in cmd and "--from-db" not in cmd


def test_settle_rejects_a_crafted_modes_value():
    with pytest.raises(ValueError):
        agent._b_settle({"date": "20261004", "from_db": True, "modes": "live; rm -rf /"})


def test_settle_runs_on_both_servers():
    """おまかせを Windows で回しても「未対応の kind」で止まらないこと。"""
    assert agent._COMMANDS["vm"]["settle"] is agent._b_settle
    assert agent._COMMANDS["windows"]["settle"] is agent._b_settle


def test_bet_type_amounts_must_be_ticket_units(monkeypatch):
    """★100円単位を外すと、その券種だけが黙って全件 skipped になる。

    券種名の間違い(=fuku へ落とす)と違い、黙って別の金額にするわけにいかない。
    投入時点で落として、気付ける形にする。
    """
    monkeypatch.setattr(agent, "_daily_budget", lambda d: None)
    assert agent._bet_type_arg("tan:3000,umatan:500") == "tan:3000,umatan:500"
    assert agent._bet_type_arg("tan") == "tan"          # 金額なしは flat_amount
    for bad in ("tan:150", "tan:1000,umatan:50", "tan:0"):
        with pytest.raises(ValueError):
            agent._bet_type_arg(bad)
    with pytest.raises(ValueError):
        agent._b_race_day(_race_day_args(bet_type="tan:1000,umatan:50"))


def test_bet_type_unknown_still_falls_back_to_fuku():
    assert agent._bet_type_arg("tan:1000,sanrentan:100") == "fuku"


def test_fetch_race_is_windows_only_and_light():
    """★当週 RACE だけの軽い取り直し。sync-all とは別物。

    開催中は run_odds が JV-Link を握るので投入しないこと(衝突する)。
    開催中の追随は run_odds の周回の中(race_refresh_interval_sec)でやる。
    """
    cmd, cwd, _ = agent._b_fetch_race({})
    assert cmd == ["poetry", "run", "hro-synchronizer", "fetch-race"]
    assert cwd.endswith("hro-synchronizer")
    assert agent._COMMANDS["windows"]["fetch_race"] is agent._b_fetch_race
    assert "fetch_race" not in agent._COMMANDS["vm"]     # JV-Link が要る


def test_tax_seal_runs_on_either_machine():
    """★DB しか触らない。締めの後に走らせる。"""
    assert agent._COMMANDS["vm"]["tax_seal"] is agent._b_tax_seal
    assert agent._COMMANDS["windows"]["tax_seal"] is agent._b_tax_seal
    cmd, cwd, _ = agent._b_tax_seal({"date": "20261011"})
    assert cmd[:4] == ["poetry", "run", "hro-ops", "tax-seal"]
    assert _opt(cmd, "--date") == "20261011" and cwd.endswith("hro-operations")


def test_tax_seal_verify_mode_takes_no_date():
    cmd, _, _ = agent._b_tax_seal({"verify": True, "date": "20261011"})
    assert "--verify" in cmd and "--date" not in cmd

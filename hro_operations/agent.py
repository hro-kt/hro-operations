"""オペレーション・エージェント（各サーバ常駐）。

ops_job テーブルから自分の target(vm/windows) の queued ジョブを1件ずつ claim し、
kind をホワイトリストのコマンドに写像して subprocess 実行、標準出力を log 列へ逐次追記、
完了で status/exit_code を書き戻す。admin(Functions API)が queued を投入する。

★安全: kind は固定写像のみ実行。args は各ビルダで検証(YYYYMMDD/整数など)し、shell 補間せず
  env で渡す(shell=False)。任意コマンドは実行しない。

起動:  poetry run hro-ops agent --server vm       # VM(特徴/day-runner/決済)
       poetry run hro-ops agent --server windows  # Windows(JV-Link: sync/odds)
接続は POSTGRES_* env。作業ディレクトリの基点は HRO_HOME(既定 ~/hro)。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

_JST = timezone(timedelta(hours=9))


def _today_jst() -> str:
    """JRA暦は JST。既定日付は UTC でなく JST の当日にする(open前の未明でも正しい開催日)。"""
    return datetime.now(_JST).strftime("%Y%m%d")

_YMD = re.compile(r"^\d{8}$")


def _conninfo() -> str:
    g = os.environ.get
    s = (f"host={g('POSTGRES_HOST','127.0.0.1')} port={g('POSTGRES_PORT','5432')} "
         f"dbname={g('POSTGRES_DATABASE','hro')} user={g('POSTGRES_USER','postgres')} "
         f"password={g('POSTGRES_PASSWORD','')}")
    if g("POSTGRES_SSLMODE"):
        s += f" sslmode={g('POSTGRES_SSLMODE')}"
    return s


def _home() -> str:
    # 区切り混在を避けるため os.path.join で組む(Windowsは \ に揃う)。
    return os.environ.get("HRO_HOME") or os.path.join(os.path.expanduser("~"), "hro")


def _ymd(v, default: str = "") -> str:
    s = str(v or default)
    if s and not _YMD.match(s):
        raise ValueError(f"日付は YYYYMMDD: {s!r}")
    return s


def _int(v, default: int) -> int:
    return int(v if v is not None else default)


# --- kind -> コマンド写像(ホワイトリスト)。各ビルダは (cmd_list, cwd, extra_env) を返す。 ---
def _b_productionize(a: dict):
    env = {}
    for k in ("VALID_FROM", "TEST_FROM", "CAL_FROM", "CAL_TO"):
        if a.get(k.lower()):
            env[k] = _ymd(a[k.lower()])
    return (["bash", "scripts/productionize.sh"], os.path.join(_home(), "hro-operations"), env)


def _daily_budget(ymd: str) -> int | None:
    """admin UI で設定した日別予算(ops_daily_budget)。無ければ None(=run-day 既定)。"""
    try:
        import psycopg
        with psycopg.connect(_conninfo()) as conn, conn.cursor() as cur:
            cur.execute("SELECT amount FROM ops_daily_budget WHERE budget_key=%s", (ymd,))
            row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def _b_trio_day(a: dict):
    d = _ymd(a.get("date"), _today_jst())
    env = {"DATE": d, "BANKROLL": str(_int(a.get("bankroll"), 100000))}
    budget = _daily_budget(d)
    if budget is not None:
        env["DAILY_BUDGET"] = str(budget)   # UI設定の日別予算を実購入に反映
    if a.get("dry_run"):                     # 発注指示だけ即時プレビュー(待たない/ガード最小)
        env["MODE"] = "dry_run"
        env["NOWAIT"] = "1"
    src = a.get("source")
    if src in ("live", "confirmed", "replay"):  # replay=保存済ライブで過去日検証
        env["SOURCE"] = src
        if src in ("confirmed", "replay"):
            env["SKIP_REFRESH"] = "1"  # 検証は既存MVで十分=全MV再構築を省いて高速化
    if a.get("preset") in ("trio", "trio_wide"):
        env["PRESET"] = a["preset"]          # trio_wide=trio帯 ＋ wide(er>=1.7&prob>=0.10)・flat 併用
    return (["bash", "scripts/trio_day.sh"], os.path.join(_home(), "hro-operations"), env)


def _b_refresh(a: dict):
    return (["poetry", "run", "hro-features", "refresh"], os.path.join(_home(), "hro-features"), {})


def _b_settle(a: dict):
    d = _ymd(a.get("date"), _today_jst())
    results = f"results_{d}.jsonl"   # 安全な固定パターン(任意パス不可)
    # --write で bet_settlements に記録(admin の損益/実績に反映)。
    cmd = ["poetry", "run", "hro-buyer", "settle", "--results", results, "--write"]
    if a.get("watch"):               # 常駐: 開催中は定期的に再決済(段階確定, 冪等)
        cmd += ["--watch"]
    return (cmd, os.path.join(_home(), "hro-operations"), {})


def _b_sync_all(a: dict):
    # UIの「日付」欄を sync の開始日(fromtime)として渡せる。日付を指定すると SYNC_SMART を切り
    # 「その日以降のみ」を通常(option=1, サーバDL)で取得＝JVLinkのセットアップDVDダイアログを回避。
    # 空欄なら SYNC_SMART(既定ON): 種別ごとに DB frontier−lookback から自動差分。
    env = {}
    d = a.get("date")
    if d:
        env["SYNC_FROM"] = _ymd(d) + "000000"
        env["SYNC_SMART"] = "0"
    return (["poetry", "run", "hro-synchronizer", "sync-all"],
            os.path.join(_home(), "hro-synchronizer"), env)


def _b_run_odds(a: dict):
    d = _ymd(a.get("date"), _today_jst())
    return (["poetry", "run", "hro-synchronizer", "--date", d, "run"],
            os.path.join(_home(), "hro-synchronizer"), {"ODDS_SPEC": "0B30"})


def _b_tyb_poll(a: dict):
    # 直前情報(TYB)を JRDB からHTTP取得して nl_jrdb_tyb へ。synchronizer が居る Windows で常駐。
    d = _ymd(a.get("date"), _today_jst())
    return (["poetry", "run", "python", "-m", "hro_synchronizer.jrdb_tyb_loader",
             "poll", "--date", d, "--interval", "180"],
            os.path.join(_home(), "hro-synchronizer"), {})


def _b_reparse(a: dict):
    # jv_raw_records の配列レコード(確定オッズO1-O6/払戻HR)を構造化テーブルへ再展開(JVLink不要)。
    # 引数なし=全期間・全種別。types/from/to で絞れる(make_date基準)。冪等。
    types = str(a.get("types") or "O1,O2,O3,O4,O5,O6,HR")
    cmd = ["poetry", "run", "hro-synchronizer", "reparse", "--types", types]
    if a.get("from"):
        cmd += ["--from", _ymd(a["from"])]
    if a.get("to"):
        cmd += ["--to", _ymd(a["to"])]
    return (cmd, os.path.join(_home(), "hro-synchronizer"), {})


def _b_jrdb_load(a: dict):
    # JRDB(KYI/CYB/KAB/SED)を [from,to] で取込。開催毎に回す(TYB以外は取得経路が無く停止していた)。
    # 日付指定=その日/明示レンジ、無指定=直近3日(取りこぼし救済で広めに再取得。冪等)。
    if a.get("from") and a.get("to"):
        f, t = _ymd(a["from"]), _ymd(a["to"])
    elif a.get("date"):
        f = t = _ymd(a["date"])
    else:
        t = _today_jst()
        f = (datetime.strptime(t, "%Y%m%d") - timedelta(days=3)).strftime("%Y%m%d")
    cmd = ["poetry", "run", "python", "-m", "hro_synchronizer.jrdb_load_all",
           "--from", f, "--to", t]
    return (cmd, os.path.join(_home(), "hro-synchronizer"), {})


def _b_backfill(a: dict):
    # 過去JRAをモデル採点し prediction_log へ(MLOps監視の土台)。学習と同一ablation envで実行。
    env = {"FROM": _ymd(a.get("from")), "TO": _ymd(a.get("to"))}
    if a.get("limit"):
        env["LIMIT"] = str(_int(a.get("limit"), 0))
    if a.get("source"):
        env["SOURCE"] = str(a["source"])
    return (["bash", "scripts/backfill_predictions.sh"],
            os.path.join(_home(), "hro-operations"), env)


def _float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _flow_day_params(a: dict) -> dict:
    """UI の flow 設定を解決して1箇所に畳む(VM/Windows のビルダで共用)。

    ★タイミングの拘束: 決定時点 > 実行時刻 > 締切(既定60秒)。
    判断に使うスナップショット(T-flow_lead)は実行時刻の時点で存在していなければならず、
    発注(T-act)は締切より前でなければガードに捨てられる。ここで弾かないと
    「1日走りきって0件」という最悪の壊れ方をするので、投入時点で落とす。
    """
    d = _ymd(a.get("date"), _today_jst())
    p = {
        "date": d,
        "threshold": _float(a.get("threshold"), 0.2802),
        # 既定は「締切30秒前に投票開始」で実際に使える組。発表時刻は分刻みなので、
        # 発走-90s の時点で存在する最新スナップは 発走-120s のもの。ts(0B41)は
        # 発走近傍が 0/60/360秒しか無く 120 を指定しても 360 に落ちるため sokuho を既定にする。
        "source": "ts" if a.get("source") == "ts" else "sokuho",
        "flow_lead": _int(a.get("lead_seconds"), 120),
        "flow_min": _int(a.get("flow_minutes"), 6),
        "act_lead": 0,          # 下で締切基準から解決する
        "act_before_deadline": _int(a.get("act_before_deadline_seconds"), 0),
        "deadline_lead": _int(a.get("deadline_lead_seconds"), 60),
        "flat_amount": _int(a.get("flat_amount"), 100),
        "mode": "live" if a.get("mode") == "live" else "paper",
        "no_wait": bool(a.get("no_wait")),
    }
    # 運用上の基準は「投票締切の何秒前に投票を開始するか」。発走基準へ変換する。
    #   締切 = 発走 - deadline_lead(実測60秒)  →  実行 = 締切 - act_before_deadline
    # act_lead_seconds(発走基準)が明示されていればそちらを優先する(旧UI/CLI互換)。
    if a.get("act_lead_seconds") is not None:
        p["act_lead"] = _int(a.get("act_lead_seconds"), 30)
    else:
        p["act_lead"] = p["deadline_lead"] + (p["act_before_deadline"] or 30)

    # paper は「その設定なら何を選んだか」を記録する計測走行なので止めない(警告のみ)。
    # live は1件も通らないまま開催日を使い切るのが最悪なので、投入時点で落とす。
    p["timing_problem"] = _flow_timing_problem(p)
    if p["mode"] == "live":
        if p["timing_problem"]:
            raise ValueError(p["timing_problem"])
        if not a.get("confirm_live"):
            raise ValueError("live には confirm_live=true が必要です")
        p["max_per_order"] = _int(a.get("max_amount_per_order"), 0)
        p["max_per_day"] = _int(a.get("max_amount_per_day"), 0)
        if not p["max_per_order"] or not p["max_per_day"]:
            raise ValueError("live には 1件上限と1日上限(>0)が必要です")
    # 閾値 0.2802 は (ts / 発走-60s / 6分 / 分位0.95) で取った絶対値。信号源やリードを
    # 変えるとスケールが変わる(ts と sokuho は順位相関 0.993 だが傾き 1.245)。流用不可。
    if abs(p["threshold"] - 0.2802) < 1e-9 and (p["source"], p["flow_lead"]) != ("ts", 60):
        p["threshold_note"] = (
            f"閾値 0.2802 は (ts / 発走-60s) で取った値です。現在の設定 "
            f"({p['source']} / 発走-{p['flow_lead']}s) では取り直しが必要です: "
            f"hro-ops flow-threshold --flow-source {p['source']} "
            f"--flow-lead-seconds {p['flow_lead']} --flow-minutes {p['flow_min']}")
    budget = _daily_budget(d)
    if budget is not None:
        p["daily_budget"] = budget
    return p


def _flow_timing_problem(p: dict) -> str | None:
    """決定時点 > 実行時刻 > 締切 が崩れていれば理由を返す(成立していれば None)。"""
    if p["no_wait"]:
        return None                       # 締切を待たない即時プレビュー
    if p["act_lead"] <= p["deadline_lead"]:
        return (f"実行時刻 T-{p['act_lead']}s が締切 T-{p['deadline_lead']}s 以降です。"
                f"この設定では全件が締切超過で捨てられます"
                f"({p['deadline_lead']} より大きい値にしてください)")
    if p["flow_lead"] <= p["act_lead"]:
        return (f"決定時点 T-{p['flow_lead']}s のスナップショットは、実行時刻 "
                f"T-{p['act_lead']}s にはまだ存在しません(決定時点 > 実行時刻 が必要)")
    return None


def _b_flow_day(a: dict):
    """flow 戦略の day-runner(VM)。モデル不使用。paper が既定。

    live に必要なもの: confirm_live=true、上限(1件/1日)、実画面で検証済みレシピ
    (~/ipat_recipe.json)。無ければ flow_day.sh / run-day 側が起動を拒否する。
    """
    p = _flow_day_params(a)
    env = {
        "DATE": p["date"], "FLOW_THRESHOLD": str(p["threshold"]),
        "FLOW_SOURCE": p["source"], "FLOW_LEAD": str(p["flow_lead"]),
        "FLOW_MIN": str(p["flow_min"]), "LEAD_SECONDS": str(p["act_lead"]),
        "FLAT_AMOUNT": str(p["flat_amount"]), "MODE": p["mode"],
    }
    if p["no_wait"]:
        env["NOWAIT"] = "1"
    if p["mode"] == "live":
        env["CONFIRM_LIVE"] = "1"
        env["MAX_PER_ORDER"] = str(p["max_per_order"])
        env["MAX_PER_DAY"] = str(p["max_per_day"])
    if "daily_budget" in p:
        env["DAILY_BUDGET"] = str(p["daily_budget"])
    return (["bash", "scripts/flow_day.sh"], os.path.join(_home(), "hro-operations"), env)


def _b_flow_day_windows(a: dict):
    """flow 戦略の day-runner(Windows)。IPAT を実績のある機械から叩く経路。

    ★bash に依存しないよう run-day を直接起動する。JV-Link は使わないので、
    32ビット venv(hro-synchronizer)とは**別の64ビット venv**で動かすこと
    (run-day も hro-buyer も psycopg を直接 import する)。
    """
    p = _flow_day_params(a)
    cmd = ["poetry", "run", "hro-ops", "run-day", "--date", p["date"], "--strategy", "flow",
           "--flow-threshold", str(p["threshold"]), "--flow-source", p["source"],
           "--flow-lead-seconds", str(p["flow_lead"]), "--flow-minutes", str(p["flow_min"]),
           "--flat-amount", str(p["flat_amount"]), "--lead-seconds", str(p["act_lead"]),
           "--deadline-lead-seconds", str(p["deadline_lead"]), "--mode", p["mode"]]
    if p["no_wait"]:
        cmd.append("--no-wait")
    if "daily_budget" in p:
        cmd += ["--daily-budget", str(p["daily_budget"])]
    if p["mode"] == "live":
        recipe = os.environ.get("HRO_IPAT_RECIPE") or os.path.join(
            os.path.expanduser("~"), "ipat_recipe.json")
        cmd += ["--confirm-live", "--ipat-recipe", recipe,
                "--max-amount-per-order", str(p["max_per_order"]),
                "--max-amount-per-day", str(p["max_per_day"]),
                "--ipat-screenshot-dir", os.path.join(os.path.expanduser("~"), "ipat_shots")]
    return (cmd, os.path.join(_home(), "hro-operations"), {})


def _b_flow_check(a: dict):
    """締切前オッズの検査(flow-coverage + flow-usable)。発注はしない。翌日に回す。"""
    d = _ymd(a.get("date"), _today_jst())
    cmd = ["bash", "-c",
           "poetry run hro-ops flow-usable --date \"$D\" --margin-seconds 10 && "
           "poetry run hro-ops flow-coverage --date \"$D\""]
    return (cmd, os.path.join(_home(), "hro-operations"), {"D": d})


def _b_import_results(a: dict):
    """DB に届かない機で回した結果 JSONL(results_<date>.jsonl)を bet_results へ取り込む。"""
    d = _ymd(a.get("date"), _today_jst())
    cmd = ["poetry", "run", "hro-buyer", "import-results", "--results", f"results_{d}.jsonl",
           "--budget-key", d]
    return (cmd, os.path.join(_home(), "hro-operations"), {})


def _b_fetch_ts_odds(a: dict):
    """公式時系列オッズ(0B41)。1回(既定)か常駐(repeat_seconds>0)。

    ★JV-Link は1台1プロセス。run_odds(poll-odds)と同時に走らせると COM エラー/-202 になる。
    常駐は --within-minutes で対象を絞らないと1周が長くなり締切直前を取り逃す。
    """
    d = _ymd(a.get("date"), _today_jst())
    cmd = ["poetry", "run", "hro-synchronizer", "fetch-timeseries-odds",
           "--from", d, "--to", d, "--specs", str(a.get("specs") or "0B41")]
    rep = _int(a.get("repeat_seconds"), 0)
    if rep > 0:
        cmd += ["--repeat-seconds", str(rep),
                "--within-minutes", str(_int(a.get("within_minutes"), 15))]
    return (cmd, os.path.join(_home(), "hro-synchronizer"), {})


def _b_env_check(a: dict):
    """JV-Link を使える環境か(ビット数/登録)。COM エラーの切り分け。"""
    return (["poetry", "run", "hro-synchronizer", "env-check"],
            os.path.join(_home(), "hro-synchronizer"), {})


_COMMANDS = {
    "vm": {"productionize": _b_productionize, "trio_day": _b_trio_day,
           "refresh": _b_refresh, "settle": _b_settle, "backfill": _b_backfill,
           "flow_day": _b_flow_day, "flow_check": _b_flow_check,
           "import_results": _b_import_results, "jrdb_load": _b_jrdb_load},
    "windows": {"sync_all": _b_sync_all, "run_odds": _b_run_odds,
                "tyb_poll": _b_tyb_poll, "reparse": _b_reparse, "jrdb_load": _b_jrdb_load,
                "fetch_ts_odds": _b_fetch_ts_odds, "env_check": _b_env_check,
                # IPAT を実績のある Windows から叩く経路(VM と二者択一。同時に走らせない)
                "flow_day": _b_flow_day_windows},
}


def _now():
    return datetime.now(timezone.utc)


def _heartbeat(conn, server: str, job_id) -> None:
    conn.execute(
        "INSERT INTO ops_agent(server,last_seen,current_job_id) VALUES(%s,%s,%s) "
        "ON CONFLICT(server) DO UPDATE SET last_seen=EXCLUDED.last_seen, current_job_id=EXCLUDED.current_job_id",
        (server, _now(), job_id))
    conn.commit()


def _claim(conn, server: str):
    """自分の target の最古 queued を1件 running に。SKIP LOCKED で多重実行を防ぐ。"""
    row = conn.execute(
        "UPDATE ops_job SET status='running', started_at=now(), heartbeat_at=now(), agent=%s "
        "WHERE id = (SELECT id FROM ops_job WHERE target=%s AND status='queued' "
        "            ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
        "RETURNING id, kind, args", (server, server)).fetchone()
    conn.commit()
    return row


def _canceled(conn, job_id) -> bool:
    r = conn.execute("SELECT cancel_requested FROM ops_job WHERE id=%s", (job_id,)).fetchone()
    conn.commit()
    return bool(r and r[0])


def _append_log(conn, job_id, text: str) -> None:
    conn.execute("UPDATE ops_job SET log = log || %s, heartbeat_at=now() WHERE id=%s", (text, job_id))
    conn.commit()


def _finish(conn, job_id, status: str, code) -> None:
    conn.execute("UPDATE ops_job SET status=%s, exit_code=%s, finished_at=now() WHERE id=%s",
                 (status, code, job_id))
    conn.commit()


def _run_job(conn, server: str, job_id, kind: str, args: dict, interval: float = 5.0) -> None:
    builder = _COMMANDS.get(server, {}).get(kind)
    if builder is None:
        _append_log(conn, job_id, f"[agent] 未対応の kind={kind!r} (server={server})\n")
        _finish(conn, job_id, "failed", -1)
        return
    try:
        cmd, cwd, extra_env = builder(args or {})
    except Exception as e:
        _append_log(conn, job_id, f"[agent] args検証エラー: {e}\n")
        _finish(conn, job_id, "failed", -1)
        return

    if cwd and not os.path.isdir(cwd):
        hh = os.environ.get("HRO_HOME") or "(未設定→~/hro)"
        _append_log(conn, job_id,
                    f"[agent] 作業ディレクトリが存在しません: {cwd}\n"
                    f"[agent] HRO_HOME={hh}。リポジトリ親(例 C:\\hro)を指すよう設定してください。\n")
        _finish(conn, job_id, "failed", -1)
        return

    env = {**os.environ, **extra_env}
    _append_log(conn, job_id, f"[agent] $ {' '.join(cmd)}  (cwd={cwd})\n")

    # 長時間ジョブ(trio_day/productionize 等)中も ops_agent.last_seen を別接続で更新し続ける。
    # メイン conn はログのストリーミングで占有されるため、これが無いと実行中ずっと offline に誤表示。
    import psycopg
    stop_hb = threading.Event()

    def _hb_loop() -> None:
        hb = None
        while not stop_hb.wait(interval):
            try:
                if hb is None or hb.closed:
                    hb = psycopg.connect(_conninfo(), autocommit=True)
                _heartbeat(hb, server, job_id)   # agent(ops_agent.last_seen)
                # ジョブ自体の生存も更新(無出力の長時間ジョブでも heartbeat_at が新しく保たれる)。
                hb.execute("UPDATE ops_job SET heartbeat_at=now() WHERE id=%s AND status='running'",
                           (job_id,))
            except Exception:  # 接続断等は張り直して継続
                try:
                    if hb is not None:
                        hb.close()
                except Exception:
                    pass
                hb = None
        if hb is not None:
            try:
                hb.close()
            except Exception:
                pass

    hb_thread = threading.Thread(target=_hb_loop, daemon=True)
    hb_thread.start()
    try:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     start_new_session=True)  # プロセスグループ化(cancelで一括停止)
        except Exception as e:
            _append_log(conn, job_id, f"[agent] 起動失敗: {e}\n")
            _finish(conn, job_id, "failed", -1)
            return

        buf, last_flush = [], time.monotonic()
        canceled = False
        assert proc.stdout is not None
        for line in proc.stdout:
            buf.append(line)
            if time.monotonic() - last_flush > 2.0 or len(buf) >= 40:
                _append_log(conn, job_id, "".join(buf)); buf.clear(); last_flush = time.monotonic()
                if _canceled(conn, job_id):
                    canceled = True
                    _append_log(conn, job_id, "[agent] cancel要求 → プロセスグループ停止\n")
                    try:
                        import signal
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except Exception:
                        proc.terminate()
                    break
        if buf:
            _append_log(conn, job_id, "".join(buf))
        code = proc.wait()
        _finish(conn, job_id, "canceled" if canceled else ("done" if code == 0 else "failed"), code)
    finally:
        stop_hb.set()


def run_agent(server: str, interval: float = 5.0, concurrency: int = 3) -> int:
    """上限つき並行実行。長時間ジョブ(trio_day)の裏で settle/refresh 等を回せるよう、
    claim したジョブを専用接続のワーカースレッドで実行する(1サーバ最大 concurrency 本)。"""
    import psycopg
    if server not in _COMMANDS:
        sys.exit(f"--server は {list(_COMMANDS)} のいずれか")
    concurrency = max(1, concurrency)
    print(f"agent 起動 server={server} 並行数={concurrency} 対応kind={list(_COMMANDS[server])} (Ctrl-Cで停止)", flush=True)

    slots = threading.Semaphore(concurrency)

    def _worker(job_id, kind, args) -> None:
        wconn = None
        try:
            wconn = psycopg.connect(_conninfo(), autocommit=False)  # ジョブごとに専用接続(スレッド安全)
            print(f"  job#{job_id} kind={kind} 実行", flush=True)
            _run_job(wconn, server, job_id, kind, args, interval)
            print(f"  job#{job_id} 完了", flush=True)
        except Exception as e:
            print(f"  job#{job_id} 実行エラー: {type(e).__name__}: {e}", flush=True)
            try:
                if wconn is not None and not wconn.closed:
                    _finish(wconn, job_id, "failed", -1)
            except Exception:
                pass
        finally:
            if wconn is not None:
                try:
                    wconn.close()
                except Exception:
                    pass
            slots.release()

    conn = None  # claim + agent heartbeat 用(メインスレッド専用)
    try:
        while True:
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(_conninfo(), autocommit=False)
                _heartbeat(conn, server, None)
                if not slots.acquire(blocking=False):  # 空きスロット無し → 待つ
                    time.sleep(interval); continue
                row = _claim(conn, server)
                if row is None:
                    slots.release()
                    time.sleep(interval); continue
                job_id, kind, args = row
                threading.Thread(target=_worker, args=(job_id, kind, args), daemon=True).start()
                # 空きがあれば次周回で即 claim(queueを詰めて捌く)。heartbeat も毎周回。
            except KeyboardInterrupt:
                raise
            except Exception as e:  # ジョブ失敗/DB切断でagentを落とさず、再接続して継続
                print(f"agent loop error: {type(e).__name__}: {e}", flush=True)
                try:
                    if conn is not None and not conn.closed:
                        conn.close()
                except Exception:
                    pass
                conn = None
                time.sleep(interval)
    except KeyboardInterrupt:
        print("agent 停止")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return 0

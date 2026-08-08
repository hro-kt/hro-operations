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
from datetime import datetime, timezone

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
    d = _ymd(a.get("date"), datetime.now(timezone.utc).strftime("%Y%m%d"))
    env = {"DATE": d, "BANKROLL": str(_int(a.get("bankroll"), 100000))}
    budget = _daily_budget(d)
    if budget is not None:
        env["DAILY_BUDGET"] = str(budget)   # UI設定の日別予算を実購入に反映
    return (["bash", "scripts/trio_day.sh"], os.path.join(_home(), "hro-operations"), env)


def _b_refresh(a: dict):
    return (["poetry", "run", "hro-features", "refresh"], os.path.join(_home(), "hro-features"), {})


def _b_settle(a: dict):
    d = _ymd(a.get("date"), datetime.now(timezone.utc).strftime("%Y%m%d"))
    results = f"results_{d}.jsonl"   # 安全な固定パターン(任意パス不可)
    return (["poetry", "run", "hro-buyer", "settle", "--results", results],
            os.path.join(_home(), "hro-operations"), {})


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
    d = _ymd(a.get("date"), datetime.now(timezone.utc).strftime("%Y%m%d"))
    return (["poetry", "run", "hro-synchronizer", "--date", d, "run"],
            os.path.join(_home(), "hro-synchronizer"), {"ODDS_SPEC": "0B30"})


_COMMANDS = {
    "vm": {"productionize": _b_productionize, "trio_day": _b_trio_day,
           "refresh": _b_refresh, "settle": _b_settle},
    "windows": {"sync_all": _b_sync_all, "run_odds": _b_run_odds},
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
                _heartbeat(hb, server, job_id)
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


def run_agent(server: str, interval: float = 5.0) -> int:
    import psycopg
    if server not in _COMMANDS:
        sys.exit(f"--server は {list(_COMMANDS)} のいずれか")
    print(f"agent 起動 server={server} 対応kind={list(_COMMANDS[server])} (Ctrl-Cで停止)", flush=True)
    conn = None
    try:
        while True:
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(_conninfo(), autocommit=False)
                _heartbeat(conn, server, None)
                row = _claim(conn, server)
                if row is None:
                    time.sleep(interval); continue
                job_id, kind, args = row
                _heartbeat(conn, server, job_id)
                print(f"  job#{job_id} kind={kind} 実行", flush=True)
                _run_job(conn, server, job_id, kind, args, interval)
                print(f"  job#{job_id} 完了", flush=True)
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

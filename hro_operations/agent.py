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
    """速報オッズ(0B30)の常駐取得。

    ★within_minutes を絞らないと当日全レースを毎周なめる。1レース約6秒なので
    24レースで1周82秒(2026-09-21 実測)になり、同じレースを1分に1回も取れない。
    締切(発走-60秒)の30秒前に 発走-120秒 のスナップを使うには、その直前に
    そのレースを取れている必要があるため、発走が近いものだけに絞る。
    """
    d = _ymd(a.get("date"), _today_jst())
    cmd = ["poetry", "run", "hro-synchronizer", "--date", d, "run",
           "--within-minutes", str(_int(a.get("within_minutes"), 20)),
           "--past-minutes", str(_int(a.get("past_minutes"), 5))]
    # ★取得周期。発表の到着が遅れるほど締切直前の価格を掴めなくなるが、遅れの一部は
    #   **自分のサンプリング待ち**(平均 周期/2)。対象を20分以内に絞れば1周1秒未満なので
    #   周期を詰めれば無料で数秒縮まる(2026-09-21 実測: 1周0.7秒/2レース)。
    env = {"ODDS_SPEC": "0B30",
           "ODDS_POLL_INTERVAL_SEC": str(_float(a.get("poll_interval_sec"), 3.0))}
    return (cmd, os.path.join(_home(), "hro-synchronizer"), env)


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


# 実時刻で動く信号源(発表時刻の分格子に縛られない)
GRID_FREE_SOURCES = {"netkeiba"}


def _thresholds(raw, source: str = "sokuho") -> dict[int, float] | None:
    """{リード秒: 閾値} を正規化する。UI からは文字列キーで来る。"""
    if not raw:
        return None
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"リード別閾値が JSON ではありません: {e}") from e
    if not isinstance(raw, dict) or not raw:
        raise ValueError('リード別閾値は {"120": 0.1631} の形で指定してください')
    out: dict[int, float] = {}
    for k, v in raw.items():
        lead = int(k)
        # ★60秒格子の制約は JV-Link(発表時刻が分刻み)の話。netkeiba は実時刻なので
        #   75 のような値が正しい。信号源を見ずに弾いていたため、正しい netkeiba 設定で
        #   ランナーが起動できなかった(2026-09-27 に実害。フロントだけ直して
        #   ここを直し忘れていた)。
        if lead % 60 and source not in GRID_FREE_SOURCES:
            raise ValueError(
                f"リードは60秒の倍数で指定してください(発表時刻が分格子): {lead}"
                f" ※信号源={source}。netkeiba なら秒単位で指定できます")
        out[lead] = float(v)
    return out


_KNOWN_BET_TYPES = ("fuku", "tan", "umatan")


def _bet_type_arg(raw) -> str:
    """"tan:1000,umatan:100" を検証して返す。1つでも知らない券種なら fuku。"""
    parts = [p.strip() for p in str(raw or "").split(",") if p.strip()]
    if not parts:
        return "fuku"
    for p in parts:
        bt, _, amt = p.partition(":")
        if bt.strip() not in _KNOWN_BET_TYPES:
            return "fuku"
        if amt and not amt.strip().isdigit():
            return "fuku"
    return ",".join(parts)


def _flow_day_params(a: dict) -> dict:
    """UI の flow 設定を解決して1箇所に畳む(VM/Windows のビルダで共用)。

    ★タイミングの拘束: 決定時点 > 実行時刻 > 締切(既定60秒)。
    判断に使うスナップショット(T-flow_lead)は実行時刻の時点で存在していなければならず、
    発注(T-act)は締切より前でなければガードに捨てられる。ここで弾かないと
    「1日走りきって0件」という最悪の壊れ方をするので、投入時点で落とす。
    """
    d = _ymd(a.get("date"), _today_jst())
    # ★信号源を先に解決する。閾値の格子検査が信号源に依存するため(netkeiba は実時刻)。
    src = (a.get("source") if a.get("source") in ("ts", "sokuho", "netkeiba")
           else "sokuho")
    p = {
        "date": d,
        "threshold": _float(a.get("threshold"), 0.2802),
        # リード別の閾値 {リード秒: 閾値}。これを渡すと、実測リードに対応する値が
        # 無いレースは**見送る**。「T-120s が間に合ったレースだけ買う」運用はこれで実現する。
        "thresholds": _thresholds(a.get("thresholds"), src),
        # 既定は「締切30秒前に投票開始」で実際に使える組。発表時刻は分刻みなので、
        # 発走-90s の時点で存在する最新スナップは 発走-120s のもの。ts(0B41)は
        # 発走近傍が 0/60/360秒しか無く 120 を指定しても 360 に落ちるため sokuho を既定にする。
        # ★許可された値だけ通し、知らない値は sokuho に落とす。以前は
        #   `"ts" if ... else "sokuho"` と書いており、netkeiba を指定しても**黙って
        #   sokuho で走っていた**(別ソースの結果を netkeiba の成績として記録する事故)。
        "source": src,
        "flow_lead": _int(a.get("lead_seconds"), 120),
        "flow_min": _int(a.get("flow_minutes"), 6),
        "act_lead": 0,          # 下で締切基準から解決する
        "act_before_deadline": _int(a.get("act_before_deadline_seconds"), 0),
        "deadline_lead": _int(a.get("deadline_lead_seconds"), 60),
        "flat_amount": _int(a.get("flat_amount"), 100),
        # ★1レースで買う上限。バッチ化(hro-buyer 151ae38)で同一レースは1回の送信に
        #   まとめるようになったので、既定は 0(無制限)で構わない。
        "max_per_race": _int(a.get("max_per_race"), 0),
        # ★券種と選別条件。**以前はここに無く、run-day の既定(複勝・絞り無し)で
        #   走っていた**。単勝×人気7+ が OOS 1.6690 と複勝(1.0768)を大きく上回る
        #   のに live では一度も使えておらず、「複勝は8頭以上に限る」という指示も
        #   実機に届いていなかった(2026-10-03 に発覚)。
        # ★知らない値は fuku に落とす(お金が動く側の既定は保守的に)
        # ★カンマ区切りで複数指定できる("tan:1000,umatan:100")。知らない券種が
        #   混ざっていたら**丸ごと fuku に落とす**(お金が動く側の既定は保守的に)。
        "bet_type": _bet_type_arg(a.get("bet_type")),
        "partners": _int(a.get("partners"), 3),
        "min_ninki": _int(a.get("min_ninki"), 0),
        "max_ninki": _int(a.get("max_ninki"), 0),
        "min_horses": _int(a.get("min_horses"), 0),
        "mode": "live" if a.get("mode") == "live" else "paper",
        "no_wait": bool(a.get("no_wait")),
    }
    # 運用上の基準は「投票締切の何秒前に投票を開始するか」。発走基準へ変換する。
    #   締切 = 発走 - deadline_lead(実測60秒)  →  実行 = 締切 - act_before_deadline
    # act_lead_seconds(発走基準)が明示されていればそちらを優先する(旧UI/CLI互換)。
    if a.get("act_lead_seconds") is not None:
        p["act_lead"] = _int(a.get("act_lead_seconds"), 30)
    else:
        p["act_lead"] = p["deadline_lead"] + (p["act_before_deadline"] or 10)

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
    if p["thresholds"]:
        pass                      # リード別に取り直した値を渡しているので警告不要
    elif abs(p["threshold"] - 0.2802) < 1e-9 and (p["source"], p["flow_lead"]) != ("ts", 60):
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
        "MAX_PER_RACE": str(p["max_per_race"]),
        "BET_TYPE": p["bet_type"], "PARTNERS": str(p["partners"]),
        "MIN_NINKI": str(p["min_ninki"]),
        "MAX_NINKI": str(p["max_ninki"]), "MIN_HORSES": str(p["min_horses"]),
    }
    if p["thresholds"]:
        import json
        env["FLOW_THRESHOLDS"] = json.dumps({str(k): v for k, v in p["thresholds"].items()})
    if p["no_wait"]:
        env["NOWAIT"] = "1"
    if p["mode"] == "live":
        env["CONFIRM_LIVE"] = "1"
        env["MAX_PER_ORDER"] = str(p["max_per_order"])
        env["MAX_PER_DAY"] = str(p["max_per_day"])
    if "daily_budget" in p:
        env["DAILY_BUDGET"] = str(p["daily_budget"])
    return (["bash", "scripts/flow_day.sh"], os.path.join(_home(), "hro-operations"), env)


def _b_close_day(a: dict):
    """開催日の締め。IPAT の記録を取り込み、突合・損益・決済まで一度に。

    ★**VM でも Windows でも動く**。IPAT にログインするのは最初の取り込みだけで、
      残り(突合・収益・決済)は DB しか触らない。JV-Link も使わない。
      必要なのは Playwright・IPAT のレシピ・IPAT の認証情報(環境変数)の3つ。
    ★レース後に走るので flow ランナーとは競合しない。Windows は JV-Link 機で
      2 vCPU、かつ Windows Update で勝手に再起動した実績がある(2026-10-04)ので、
      **VM で回す方が安定する**。
    ★CLI を3回叩くと3回ログインする。1本にまとめてログインを1回で済ませる。
    ★払戻(nl_hr)が未取込なら決済は飛ばせる(no_settle)。IPAT 側の損益は
      nl_hr に依らず出るので、締め自体は成立する。
    """
    d = _ymd(a.get("date"), _today_jst())
    cmd = ["poetry", "run", "hro-buyer", "ipat", "close-day", "--budget-key", d]
    if a.get("no_settle"):
        cmd.append("--no-settle")
    recipe = os.environ.get("HRO_IPAT_RECIPE") or os.path.join(
        os.path.expanduser("~"), "ipat_recipe.json")
    cmd += ["--ipat-recipe", recipe]
    return (cmd, os.path.join(_home(), "hro-buyer"), {})


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
           "--flow-max-per-race", str(p["max_per_race"]),
           "--flow-bet-type", p["bet_type"],
           "--flow-partners", str(p["partners"]),
           "--flow-min-ninki", str(p["min_ninki"]),
           "--flow-max-ninki", str(p["max_ninki"]),
           "--flow-min-horses", str(p["min_horses"]),
           "--deadline-lead-seconds", str(p["deadline_lead"]), "--mode", p["mode"]]
    if p["thresholds"]:
        import json
        cmd += ["--flow-thresholds",
                json.dumps({str(k): v for k, v in p["thresholds"].items()})]
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


def _b_netkeiba_odds(a: dict):
    """netkeiba の単勝オッズ(秒単位)を常駐取得。VM で動かす。

    ★JV-Link を使わないので Windows の1プロセス制約と**競合しない**。
      信号(単勝)は netkeiba、発注する複勝のオッズは JV-Link(ts_sokuho_o1)という分担。
      片方でも止まると発注は0件になる(netkeiba は単勝しか出さないため)。
    ★--within-minutes を 8 より下げないこと。起点(発走6分前)が取れなくなり、
      そのレースは丸ごと見送りになる。
    """
    d = _ymd(a.get("date"), _today_jst())
    within = max(8, _int(a.get("within_minutes"), 10))
    cmd = ["poetry", "run", "hro-synchronizer", "netkeiba-odds",
           "--date", d,
           "--within-minutes", str(within),
           "--interval", str(_float(a.get("interval"), 2.0))]
    env = {}
    if a.get("state"):
        env["NETKEIBA_STATE"] = str(a["state"])
    return (cmd, os.path.join(_home(), "hro-synchronizer"), env)


_COMMANDS = {
    "vm": {"productionize": _b_productionize, "trio_day": _b_trio_day,
           "refresh": _b_refresh, "settle": _b_settle, "backfill": _b_backfill,
           "flow_day": _b_flow_day, "flow_check": _b_flow_check,
           "import_results": _b_import_results, "jrdb_load": _b_jrdb_load,
           # ★締めは JV-Link を使わないので VM でも動く(むしろこちらが安定)
           "close_day": _b_close_day,
           # netkeiba は JV-Link を使わないので Windows の1プロセス制約と競合しない
           "netkeiba_odds": _b_netkeiba_odds},
    "windows": {"sync_all": _b_sync_all, "run_odds": _b_run_odds,
                # ★results_<date>.jsonl は run-day を回した機にできる。flow_day を
                #   Windows へ移した時点で、VM 側の import_results は成功しようが
                #   なくなっていた(2026-10-04 に発覚。117/137 が failed のまま)。
                "import_results": _b_import_results,
                "tyb_poll": _b_tyb_poll, "reparse": _b_reparse, "jrdb_load": _b_jrdb_load,
                "fetch_ts_odds": _b_fetch_ts_odds, "env_check": _b_env_check,
                # IPAT を実績のある Windows から叩く経路(VM と二者択一。同時に走らせない)
                "flow_day": _b_flow_day_windows,
                # ★Windows でも動く(レシピと認証情報がこちらに在るため)。
                #   既定は VM を勧める
                "close_day": _b_close_day},
}



# agent 自身の仮想環境を子プロセスへ漏らさないための変数
_VENV_VARS = ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "POETRY_ACTIVE",
              "PYTHONHOME", "PYTHONPATH")



def _kill(proc_ref: list) -> None:
    """子プロセスを**木ごと**止める。

    ★POSIX は start_new_session=True でプロセスグループにしてあるので killpg で一括。
    ★Windows には killpg が無い。以前はここで proc.terminate() に落としていたが、
      これは**直下の子しか殺さない**。Windows の起動は `poetry run hro-ops run-day`
      なので、死ぬのは poetry.exe だけで、python → node(Playwright) → ブラウザが
      そのまま残る。start_new_session も Windows では無視されるので、
      「プロセスグループにしてある」という前提自体が成り立っていなかった。
      taskkill /T(子孫ごと) /F で確実に落とす。
    """
    if not proc_ref:
        return
    proc = proc_ref[0]
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        except Exception:   # noqa: BLE001 - taskkill が無い/失敗 → せめて直下を落とす
            try:
                proc.kill()
            except Exception:
                pass
        return
    try:
        import signal
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:       # noqa: BLE001 - killpg が使えない環境
        try:
            proc.terminate()
        except Exception:
            pass


def _child_env(extra: dict) -> dict:
    """子プロセスの環境。★agent 自身の venv を引き継がせない。

    agent は hro-operations の venv で動く。os.environ をそのまま渡すと VIRTUAL_ENV が
    残り、`poetry run` が「既に仮想環境が有効」と判断して**別パッケージを
    hro-operations の venv で実行**する。2026-09-27 に netkeiba_odds(hro-synchronizer)で
    `ModuleNotFoundError: yaml` として露見した。それまで VM のジョブはほぼ
    hro-operations 自身を呼ぶものだったので表面化していなかった。
    PATH からも venv の bin を落とす(そこに別パッケージの実行ファイルは無い)。
    """
    env = {k: v for k, v in os.environ.items() if k not in _VENV_VARS}
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        bindir = os.path.join(venv, "bin")
        parts = [p for p in env.get("PATH", "").split(os.pathsep)
                 if p and os.path.normpath(p) != os.path.normpath(bindir)]
        env["PATH"] = os.pathsep.join(parts)
    env.update(extra)
    return env


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


def _reclaim(conn, server: str, *, stale_sec: float, startup: bool = False) -> int:
    """死んだジョブの `running` を回収して failed にする。

    ★heartbeat_at は書いていたのに**誰も読んでいなかった**。そのため VM が再起動したり
      プロセスが消えたりすると ops_job の行が running のまま永久に残り、UI 上も
      「中止を押しても消えない」ように見えていた(2026-10-03 に Windows Update の
      自動再起動で実害: 13:42 と 13:51 の2回再起動し、当日の runner が消えたまま)。
    ★startup=True では心拍の新しさに関係なく自分の running を全部回収する。
      エージェントが今起動したのだから、自分名義で走っているジョブは存在し得ない。
    """
    if startup:
        where, params = "", (server,)
    else:
        where = " AND heartbeat_at < now() - make_interval(secs => %s)"
        params = (server, stale_sec)
    rows = conn.execute(
        "UPDATE ops_job SET status='failed', exit_code=-1, finished_at=now(), "
        "  log = log || %s "
        "WHERE target=%s AND status='running'" + where + " RETURNING id",
        ("\n[agent] 心拍が途絶えたため回収しました(プロセス消滅/再起動の可能性)\n",)
        + params).fetchall()
    conn.commit()
    return len(rows)


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

    env = _child_env(extra_env)
    _append_log(conn, job_id, f"[agent] $ {' '.join(cmd)}  (cwd={cwd})\n")

    # 長時間ジョブ(trio_day/productionize 等)中も ops_agent.last_seen を別接続で更新し続ける。
    # メイン conn はログのストリーミングで占有されるため、これが無いと実行中ずっと offline に誤表示。
    import psycopg
    stop_hb = threading.Event()
    # ★キャンセルは**この別スレッド**で見る。出力ループ(`for line in proc.stdout`)は
    #   readline でブロックするので、無言で待機するジョブ(flow ランナーはレース間で
    #   数分沈黙する)では**次に何か出力されるまでキャンセルが効かない**。
    #   2026-09-27 に「中止を押しても消えない」として実害。
    cancel_flag = threading.Event()

    def _hb_loop() -> None:
        hb = None
        while not stop_hb.wait(interval):
            try:
                if hb is None or hb.closed:
                    hb = psycopg.connect(_conninfo(), autocommit=True)
                _heartbeat(hb, server, job_id)   # agent(ops_agent.last_seen)
                if not cancel_flag.is_set() and _canceled(hb, job_id):
                    cancel_flag.set()
                    _kill(proc_ref)
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

    proc_ref: list = []          # 起動前にキャンセルが来ても壊れないよう list で渡す
    hb_thread = threading.Thread(target=_hb_loop, daemon=True)
    hb_thread.start()
    try:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     start_new_session=True)  # プロセスグループ化(cancelで一括停止)
            proc_ref.append(proc)
        except Exception as e:
            _append_log(conn, job_id, f"[agent] 起動失敗: {e}\n")
            _finish(conn, job_id, "failed", -1)
            return

        buf, last_flush = [], time.monotonic()
        assert proc.stdout is not None
        for line in proc.stdout:
            buf.append(line)
            if time.monotonic() - last_flush > 2.0 or len(buf) >= 40:
                _append_log(conn, job_id, "".join(buf)); buf.clear(); last_flush = time.monotonic()
                if cancel_flag.is_set():
                    break
        if buf:
            _append_log(conn, job_id, "".join(buf))
        code = proc.wait()
        if cancel_flag.is_set():
            _append_log(conn, job_id, "[agent] cancel要求 → プロセスグループ停止\n")
        _finish(conn, job_id,
                "canceled" if cancel_flag.is_set() else ("done" if code == 0 else "failed"),
                code)
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
        stale_sec = max(interval * 6, 180.0)   # 心拍は interval 毎。余裕を持って判定する
        swept = False
        while True:
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(_conninfo(), autocommit=False)
                if not swept:
                    # ★起動直後の一掃。再起動で消えたジョブはここで落ちる。
                    n = _reclaim(conn, server, stale_sec=stale_sec, startup=True)
                    if n:
                        print(f"agent: 起動時に running のジョブ {n} 件を回収しました",
                              flush=True)
                    swept = True
                elif _now().second < interval:     # 毎分あたり1回程度に抑える
                    n = _reclaim(conn, server, stale_sec=stale_sec)
                    if n:
                        print(f"agent: 心拍の途絶えたジョブ {n} 件を回収しました", flush=True)
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

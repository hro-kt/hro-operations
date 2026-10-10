"""開催日のオーケストレーション(1ジョブで当日を回しきる)。

★開催日は手でボタンを順に押す作業になっていて、押し忘れ・止め忘れがそのまま
  取りこぼしになっていた。2026-10-04 は Windows Update の再起動でランナーが消え、
  誰も再投入しないまま5レースを失った。手順と後始末を機械に任せる。

当日の依存関係:

    朝  preflight ─┬─ run_odds      (Windows / JV-Link / 常駐)
                   ├─ netkeiba_odds (VM             / 常駐)
                   └─ flow_day      (Win か VM 二択 / 常駐)
    最終レース後   上の常駐を**全部止めてから** sync_all (Windows / JV-Link)
                   そのあと close_day (IPAT 取り込み・突合・損益・決済)

★**JV-Link は1台1プロセス**。run_odds と sync_all/fetch_ts_odds を重ねると COM
  エラー(-202)になり、その日の収集が丸ごと死ぬ。「止めてから同期」はここが理由で、
  人が順番を守る限り問題にならなかったが、自動化する以上は機械が守る。
★netkeiba は JV-Link を使わないので VM で並走できる。
★flow ランナーは IPAT セッションが1つなので Windows と VM の二者択一。両方起こさない。

子ジョブは args に `orchestrated_by: <親のjob id>` を入れて出す。これで
  - 親が再起動しても自分の子を見つけ直せる(引き継ぎ)
  - 親が死んだ子を回収できる(agent 側の掃除)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_JST = timezone(timedelta(hours=9))

# 常駐ジョブ(止めるまで動き続ける)と単発ジョブで扱いが違う
_RESIDENT = True
_ONESHOT = False


@dataclass(frozen=True)
class Step:
    """1手順。kind/target は ops_job のものをそのまま使う。"""

    name: str
    kind: str
    target: str
    args: dict = field(default_factory=dict)
    resident: bool = _ONESHOT
    jvlink: bool = False        # JV-Link を占有するか
    required: bool = True       # 落ちたら致命的か(常駐のみ意味がある)
    # 単発の完了待ち上限。重い同期は既定より長くないと、終わる前に見限ってしまう
    timeout_seconds: float | None = None


def start_steps(date: str, flow_args: dict, *, flow_target: str = "windows",
                with_netkeiba: bool = True, with_run_odds: bool = True) -> list[Step]:
    """開催前に起こす常駐ジョブ。

    ★run_odds と netkeiba_odds は役割が違う。信号(単勝の資金移動)は netkeiba、
      発注の可否を決める複勝オッズは JV-Link 由来。**片方でも止まると0件**になる。
    """
    steps: list[Step] = []
    if with_run_odds:
        steps.append(Step("速報オッズ(JV-Link)", "run_odds", "windows", {"date": date},
                          resident=_RESIDENT, jvlink=True))
    if with_netkeiba:
        steps.append(Step("netkeiba オッズ", "netkeiba_odds", "vm", {"date": date},
                          resident=_RESIDENT))
    steps.append(Step("flow ランナー", "flow_day", flow_target,
                      {**flow_args, "date": date}, resident=_RESIDENT))
    return steps


def finish_steps(date: str, *, close_target: str = "vm") -> list[Step]:
    """最終レース後。**常駐を止めてから**、当日にしかできないことだけをやる。

    ★当日やる価値があるのは締めだけ。close_day は IPAT の**投票履歴**を読むが、
      IPAT で遡れるのは当日と前日だけで、夜間メンテの時間帯もある。**期限が
      あるのはここ**。
    ★当日の締めは必ず no_settle。**払戻(HR)は開催の3〜5日後にしか配信されない**
      (2026-09-22 実測: 9/12・9/13 開催分の make_date が 9/14、受信は 9/17)。
      当日に決済を回しても nl_hr が空で settled=False になるだけで、何も確定しない。
      当日に出せる確定値は **IPAT 自身の記録**(受付明細の払戻)の方で、close_day は
      それを取り込んで損益まで出す。
    ★JV-Data 同期と決済は**当日のジョブに入れない**。どちらも期限が無く、同期は
      全種別の差分を取る重いジョブで時間が読めない。当日の経路に置くと、長引いた
      ぶんだけ締めが危うくなるだけで、得るものが無い。別日に settle_steps で回す。
    """
    return [Step("開催日の締め(IPAT の記録=当日の確定値)", "close_day", close_target,
                 {"date": date, "no_settle": True})]


def settle_steps(dates: list[str], *, target: str = "vm",
                 with_sync: bool = True) -> list[Step]:
    """後日(払戻が届いてから)の手順。開催日とは別に回す。

    ★sync_all に date を**渡さない**。date を渡すと SYNC_SMART が切れて
      「その日以降のみ」になり、取りに行きたい**過去の開催日の払戻**が入らない
      (_b_sync_all 参照)。空にすると種別ごとに DB の frontier から自動差分で取る。
    ★同期は時間が読めないので待ち時間を長めに。待ち切れなくてもジョブは止めない。
    """
    steps: list[Step] = []
    if with_sync:
        steps.append(Step("JV-Data 同期(払戻を取り込む)", "sync_all", "windows",
                          {}, jvlink=True, timeout_seconds=4 * 3600.0))
    for d in dates:
        steps.append(Step(f"決済 {d}(払戻との突合)", "settle", target,
                          {"date": d, "from_db": True, "modes": "live"}))
    return steps


def jvlink_conflict(steps: list[Step]) -> str | None:
    """JV-Link を占有する常駐が2本以上ある計画なら理由を返す。

    ★計画の時点で弾く。走らせてから COM エラーで気付くと、その日はもう取り返せない。
    """
    names = [s.name for s in steps if s.jvlink and s.resident]
    if len(names) > 1:
        return ("JV-Link を使う常駐が複数あります(1台1プロセス): " + " / ".join(names))
    return None


def describe(steps: list[Step]) -> str:
    """計画を人が読める形に(--dry-run / ログ冒頭)。"""
    out = []
    for i, s in enumerate(steps, 1):
        tag = "常駐" if s.resident else "単発"
        jv = " [JV-Link]" if s.jvlink else ""
        args = json.dumps(s.args, ensure_ascii=False, sort_keys=True) if s.args else "{}"
        out.append(f"  {i}. {s.name}  ({tag}{jv})  kind={s.kind} target={s.target}\n"
                   f"       args={args}")
    return "\n".join(out)


def parse_hhmm(date: str, hhmm: str) -> datetime:
    """YYYYMMDD + HHMM を JST の時刻に。"""
    return datetime.strptime(date + hhmm, "%Y%m%d%H%M").replace(tzinfo=_JST)


def stop_at(date: str, last_post: str, *, after_minutes: int = 3) -> datetime:
    """常駐を止める時刻。最終レースの発走 + マージン。

    ★発走の**後**に止める。締切(発走-60秒)で止めると、投票のリトライや
      結果の書き戻しが途中で切れる。数分遅らせても損はしない。
    """
    return parse_hhmm(date, last_post) + timedelta(minutes=max(0, after_minutes))


def should_stop(now: datetime, stop_time: datetime) -> bool:
    return now >= stop_time


@dataclass
class Child:
    """投入した子ジョブの追跡状態。"""

    step: Step
    job_id: int | None = None
    restarts: int = 0
    adopted: bool = False       # 既に走っていたものを引き継いだ


def needs_restart(step: Step, status: str | None, child: Child, *,
                  max_restarts: int, stopping: bool) -> bool:
    """常駐が落ちた → 再投入すべきか。

    ★2026-10-04 は Windows Update の自動再起動でランナーが消え、誰も気付かないまま
      5レースを失った。常駐が死んだら黙って立て直す。
    ★停止フェーズに入ってからは再投入しない(自分で止めたものを起こし直さない)。
    ★回数を区切る。起動直後に必ず落ちる設定(閾値のキー違い等)を無限に投げ続けると、
      ログが溢れるだけで何も直らない。
    """
    if stopping or not step.resident:
        return False
    if status not in ("failed", "canceled", "done"):
        return False
    return child.restarts < max_restarts


# --------------------------------------------------------------------------
# 実行時(DB を介して子ジョブを出し入れする)
# --------------------------------------------------------------------------

_TERMINAL = ("done", "failed", "canceled")


def _log(msg: str) -> None:
    """agent が標準出力を ops_job.log へ流し込むので、print がそのまま運用ログになる。"""
    print(f"[{datetime.now(_JST):%H:%M:%S}] {msg}", flush=True)


def _connect():
    import psycopg
    from hro_operations.agent import _conninfo
    return psycopg.connect(_conninfo(), autocommit=True)


def _last_post(conn, date: str) -> str | None:
    """当日 JRA の最終発走 HHMM。nl_ra に無ければ None。"""
    row = conn.execute(
        "SELECT max(hasso_time) FROM nl_ra "
        "WHERE year=%s AND month_day=%s AND jyo_cd BETWEEN '01' AND '10' "
        "  AND hasso_time ~ '^[0-9]{4}$'", (date[:4], date[4:8])).fetchone()
    return row[0] if row and row[0] else None


_SQL_PENDING_SETTLEMENT = """
-- live で賭けたのに決済が1件も入っていない開催日のうち、**払戻がもう届いている**もの。
-- ★nl_hr は的中組合せの行しか持たないので「そのレースの行が在るか」で確定判定する
--   (settlement.build_payout_index と同じ見方)。
SELECT r.budget_key
FROM (SELECT DISTINCT budget_key FROM bet_results
      WHERE mode = 'live' AND status = 'submitted'
        AND budget_key ~ '^[0-9]{8}$'
        AND budget_key >= to_char(now() AT TIME ZONE 'Asia/Tokyo' - %(days)s * interval '1 day',
                                  'YYYYMMDD')
        AND budget_key < %(today)s) r
WHERE NOT EXISTS (SELECT 1 FROM bet_settlements s
                  WHERE s.budget_key = r.budget_key AND s.settled_at IS NOT NULL)
  AND EXISTS (SELECT 1 FROM nl_hr h
              WHERE h.year = left(r.budget_key, 4) AND h.month_day = right(r.budget_key, 4))
ORDER BY r.budget_key
"""


def pending_settlement_dates(conn, today: str, *, days: int = 28,
                             limit: int = 5) -> list[str]:
    """決済待ちで、かつ払戻がもう届いている過去の開催日。

    ★**払戻(HR)は開催の3〜5日後**にしか配信されない。当日に決済を回しても空振りする
      だけなので、「今日の分を今日締める」のではなく「届いた分をその日に片付ける」。
      開催日ごとに人が思い出して押す必要をなくすのが目的。
    ★sync_all の**後**に呼ぶこと。同期で初めて nl_hr に入る日があるため。
    """
    try:
        rows = conn.execute(_SQL_PENDING_SETTLEMENT,
                            {"days": days, "today": today}).fetchall()
    except Exception as e:      # noqa: BLE001 - 締め本体は落とさない
        _log(f"⚠ 決済待ちの開催日を調べられませんでした: {e}")
        return []
    return [str(r[0]) for r in rows][:limit]


def _adopt(conn, step: Step, date: str) -> int | None:
    """同じ kind/target/date で既に queued|running の子がいれば、その id を返す。

    ★二重起動を防ぐのが目的。netkeiba を2本起こせば片方は無駄にブラウザを掴み、
      flow ランナーを2本起こせば**同じレースを2回買う**。ボタンの二度押しや
      オーケストレータの再投入で簡単に起きるので、投入前に必ず見る。
    """
    row = conn.execute(
        "SELECT id FROM ops_job WHERE kind=%s AND target=%s AND status IN ('queued','running') "
        "  AND coalesce(args->>'date','') = %s AND NOT cancel_requested "
        "ORDER BY requested_at DESC LIMIT 1", (step.kind, step.target, date)).fetchone()
    return int(row[0]) if row else None


def _enqueue(conn, step: Step, parent_id: int | None) -> int:
    args = dict(step.args)
    if parent_id is not None:
        args["orchestrated_by"] = parent_id
        # ★掃除の対象は**常駐だけ**。単発(同期や締め)は放っておいても自分で終わる。
        #   親が先に降りただけで走っている同期を殺すと、重い処理をやり直しになる。
        if step.resident:
            args["resident"] = True
    row = conn.execute(
        "INSERT INTO ops_job(kind,args,target,requested_by) VALUES(%s,%s::jsonb,%s,%s) "
        "RETURNING id",
        (step.kind, json.dumps(args, ensure_ascii=False), step.target,
         f"orchestrator#{parent_id}" if parent_id else "orchestrator")).fetchone()
    return int(row[0])


def _status(conn, job_id: int) -> str | None:
    row = conn.execute("SELECT status FROM ops_job WHERE id=%s", (job_id,)).fetchone()
    return row[0] if row else None


def _tail(conn, job_id: int, n: int = 400) -> str:
    row = conn.execute("SELECT right(coalesce(log,''), %s) FROM ops_job WHERE id=%s",
                       (n, job_id)).fetchone()
    return (row[0] or "").strip() if row else ""


def _cancel(conn, job_id: int) -> None:
    conn.execute("UPDATE ops_job SET cancel_requested=true "
                 "WHERE id=%s AND status IN ('queued','running')", (job_id,))


def _self_canceled(conn, job_id: int | None) -> bool:
    """自分に中止が来ていないか。

    ★agent はプロセスを殺しにくるが、Windows は taskkill /F なので後始末の猶予が無い。
      自分で先に気付いて、子を止めてから降りる。
    """
    if job_id is None:
        return False
    row = conn.execute("SELECT cancel_requested FROM ops_job WHERE id=%s", (job_id,)).fetchone()
    return bool(row and row[0])


def _sleep_until(deadline: datetime, *, poll: float) -> float:
    import time
    time.sleep(poll)
    return poll


def _preflight(date: str, flow_args: dict) -> list[str]:
    """走り出す前に致命的な欠落を見る。問題の一覧(空なら OK)を返す。

    flow_args は **UI の生の設定**(flow_day ジョブに渡すものと同一)。

    ★ここで落とせるのは「設定が噛み合っていない」類(閾値のキー違い・リードの大小)。
      朝の時点ではオッズがまだ届いていないのが正常なので、preflight 側が
      too_early を見て note に落としてくれる。
    """
    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .preflight import check
    from .race_day import DayConfig

    # ★解決は agent と**同じ関数**でやる。ここで独自に既定値を置くと、preflight が
    #   見る設定と実際にランナーへ渡る設定がずれる(それが 2026-09-26 の0件の形)。
    from .agent import _flow_day_params
    p = _flow_day_params(flow_args)
    cfg = DayConfig(date=date, win_model="", place_model="", results_path="",
                    strategy="flow",
                    flow_source=p["source"],
                    flow_lead_seconds=p["flow_lead"],
                    flow_minutes=p["flow_min"],
                    flow_threshold=p["threshold"],
                    flow_thresholds=p["thresholds"],
                    deadline_lead_seconds=p["deadline_lead"],
                    lead_seconds=p["act_lead"])
    if p.get("threshold_note"):
        _log(f"  note: {p['threshold_note']}")
    db = FeatureDB(load_features_config())
    try:
        res = check(db, date, cfg)
    finally:
        db.close()
    for n in res.get("notes") or []:
        _log(f"  note: {n}")
    return list(res.get("problems") or [])


@dataclass
class OrchestratorConfig:
    date: str
    flow_args: dict = field(default_factory=dict)
    flow_target: str = "windows"       # IPAT を叩く機(Windows と VM の二択)
    close_target: str = "vm"           # 締めは JV-Link を使わないので VM が安定
    with_run_odds: bool = True
    with_netkeiba: bool = True
    stop_after_minutes: int = 3        # 最終発走 + これ分 で常駐を止める
    max_restarts: int = 3              # 常駐1本あたりの再投入上限
    poll_seconds: float = 20.0
    drain_seconds: float = 180.0       # 常駐の停止を待つ上限(JV-Link の解放待ち)
    finish_timeout_seconds: float = 3600.0
    dry_run: bool = False
    job_id: int | None = None          # 自分の ops_job.id(agent が渡す)


def run(cfg: OrchestratorConfig) -> int:
    """開催日を1本で回す。戻り値は終了コード。"""
    steps = start_steps(cfg.date, cfg.flow_args, flow_target=cfg.flow_target,
                        with_netkeiba=cfg.with_netkeiba, with_run_odds=cfg.with_run_odds)
    fin = finish_steps(cfg.date, close_target=cfg.close_target)

    conflict = jvlink_conflict(steps)
    if conflict:
        _log(f"✗ 計画が不正: {conflict}")
        return 2
    if cfg.flow_target not in ("vm", "windows"):
        _log(f"✗ flow の実行先は vm か windows: {cfg.flow_target!r}")
        return 2

    _log(f"=== 開催日オーケストレーション {cfg.date} ===")
    _log("起動する常駐:")
    print(describe(steps), flush=True)
    _log("最終レース後:")
    print(describe(fin), flush=True)
    _log("※ 当日の締めは決済まで行きません。**払戻(HR)は開催の3〜5日後**にしか"
         "配信されないためです。当日の確定値は IPAT 自身の記録(受付明細)から出ます。")
    _log("※ JV-Data 同期と決済は当日やりません。別ジョブ『払戻の取込と決済』"
         "(hro-ops settle-pending)で、数日後に回してください。")

    try:
        conn = _connect()
    except Exception as e:      # noqa: BLE001 - dry-run は計画だけ見たい場面がある
        _log(f"✗ DB に接続できません: {e}")
        return 0 if cfg.dry_run else 2

    last_post = _last_post(conn, cfg.date)
    if not last_post:
        _log(f"✗ nl_ra に {cfg.date} のレースがありません。RACE の同期が先です")
        conn.close()
        return 0 if cfg.dry_run else 2
    stop_time = stop_at(cfg.date, last_post, after_minutes=cfg.stop_after_minutes)
    _log(f"最終発走 {last_post[:2]}:{last_post[2:]} → 常駐の停止予定 {stop_time:%H:%M}")

    if cfg.dry_run:
        _log("dry-run のためここまで(ジョブは投入していません)")
        conn.close()
        return 0

    problems = _preflight(cfg.date, cfg.flow_args)
    if problems:
        for p in problems:
            _log(f"✗ preflight: {p}")
        _log("走り出す前に落とします(1日走って0件を避けるため)")
        return 2
    _log("preflight ✓")

    children = [Child(step=s) for s in steps]
    try:
        _start_all(conn, children, cfg)
        stopping = _monitor(conn, children, cfg, stop_time)
        _stop_all(conn, children, cfg)
        if stopping == "canceled":
            _log("中止要求のため締めは行いません")
            return 1
        return _finish(conn, fin, cfg)
    finally:
        try:
            conn.close()
        except Exception:   # noqa: BLE001
            pass


def _start_all(conn, children: list[Child], cfg: OrchestratorConfig) -> None:
    for c in children:
        existing = _adopt(conn, c.step, cfg.date)
        if existing is not None:
            c.job_id, c.adopted = existing, True
            _log(f"↻ {c.step.name}: 既に job#{existing} が動いているので引き継ぎます")
            continue
        c.job_id = _enqueue(conn, c.step, cfg.job_id)
        _log(f"▶ {c.step.name}: job#{c.job_id} を投入({c.step.target})")


def _monitor(conn, children: list[Child], cfg: OrchestratorConfig,
             stop_time: datetime) -> str:
    """停止時刻まで常駐を見張る。落ちていたら再投入する。

    戻り値: "stop_time"(予定どおり) / "canceled"(自分に中止が来た)
    """
    import time
    _log(f"監視を開始します(停止予定 {stop_time:%H:%M}、{cfg.poll_seconds:.0f}秒ごと)")
    last_note = 0.0
    while True:
        if _self_canceled(conn, cfg.job_id):
            _log("自分に中止要求。常駐を止めて降ります")
            return "canceled"
        now = datetime.now(_JST)
        if should_stop(now, stop_time):
            _log(f"停止時刻 {stop_time:%H:%M} に到達")
            return "stop_time"
        for c in children:
            st = _status(conn, c.job_id) if c.job_id else None
            if not needs_restart(c.step, st, c, max_restarts=cfg.max_restarts,
                                 stopping=False):
                continue
            tail = _tail(conn, c.job_id or 0)
            _log(f"⚠ {c.step.name}(job#{c.job_id})が {st} で終了していました。"
                 f"再投入します({c.restarts + 1}/{cfg.max_restarts})")
            if tail:
                _log(f"   直前のログ: …{tail[-200:]}")
            c.job_id = _enqueue(conn, c.step, cfg.job_id)
            c.restarts += 1
            _log(f"▶ {c.step.name}: job#{c.job_id} を再投入")
        if time.monotonic() - last_note > 600:   # 10分おきに生存を1行
            alive = [f"{c.step.name}#{c.job_id}={_status(conn, c.job_id)}"
                     for c in children if c.job_id]
            _log("稼働中: " + " / ".join(alive))
            last_note = time.monotonic()
        time.sleep(cfg.poll_seconds)


def _stop_all(conn, children: list[Child], cfg: OrchestratorConfig) -> None:
    """常駐に中止を出し、**本当に終わるまで待つ**。

    ★ここを待たずに sync_all を投げると JV-Link が二重に開く。run_odds の停止確認は
      飛ばせない。待ちきれなかった場合は sync をやめる判断を呼び出し側でする。
    """
    import time
    for c in children:
        if c.job_id and _status(conn, c.job_id) not in _TERMINAL:
            _cancel(conn, c.job_id)
            _log(f"■ {c.step.name}: job#{c.job_id} に停止を要求")
    deadline = time.monotonic() + cfg.drain_seconds
    while time.monotonic() < deadline:
        pending = [c for c in children
                   if c.job_id and _status(conn, c.job_id) not in _TERMINAL]
        if not pending:
            _log("常駐はすべて停止しました")
            return
        time.sleep(5.0)
    stuck = [f"{c.step.name}#{c.job_id}" for c in children
             if c.job_id and _status(conn, c.job_id) not in _TERMINAL]
    _log(f"⚠ {cfg.drain_seconds:.0f}秒待っても止まらない常駐があります: {', '.join(stuck)}")


def _jvlink_busy(conn, cfg: OrchestratorConfig) -> list[str]:
    """Windows で JV-Link を掴んだままのジョブ(自分の子以外も含む)。"""
    rows = conn.execute(
        "SELECT id, kind FROM ops_job WHERE target='windows' AND status='running' "
        "  AND kind IN ('run_odds','fetch_ts_odds','sync_all','reparse')").fetchall()
    return [f"{k}#{i}" for i, k in rows]


def _run_step(conn, step: Step, cfg: OrchestratorConfig) -> str:
    """1手順を投入して終わるまで待つ。戻り値は最終 status("skipped"/"canceled" を含む)。"""
    import time
    if step.jvlink:
        busy = _jvlink_busy(conn, cfg)
        if busy:
            _log(f"✗ JV-Link を掴んだままのジョブがあるため {step.name} は見送ります: "
                 f"{', '.join(busy)}(1台1プロセス)")
            return "skipped"
    job_id = _enqueue(conn, step, cfg.job_id)
    _log(f"▶ {step.name}: job#{job_id} を投入({step.target})。完了を待ちます")
    deadline = time.monotonic() + (step.timeout_seconds or cfg.finish_timeout_seconds)
    st = None
    while time.monotonic() < deadline:
        if _self_canceled(conn, cfg.job_id):
            _cancel(conn, job_id)
            _log("自分に中止要求。残りの手順はやめます")
            return "canceled"
        st = _status(conn, job_id)
        if st in _TERMINAL:
            break
        time.sleep(cfg.poll_seconds)
    if st == "done":
        _log(f"✓ {step.name}: job#{job_id} 完了")
        return "done"
    if st not in _TERMINAL:
        _log(f"⚠ {step.name}: job#{job_id} が待ち時間内に終わりませんでした"
             f"(ジョブ自体は走り続けます。中止したいときは手で止めてください)")
        return "timeout"
    _log(f"✗ {step.name}: job#{job_id} が {st} で終わりました")
    tail = _tail(conn, job_id)
    if tail:
        _log(f"   ログ末尾: …{tail[-300:]}")
    return st or "timeout"


def _finish(conn, fin: list[Step], cfg: OrchestratorConfig) -> int:
    """最終レース後の手順を**順番に**流す。前が終わるまで次を出さない。

    ★当日やるのは締めだけ。JV-Data 同期と決済は別日(settle-pending)に回す。
    """
    rc = 0
    for step in fin:
        st = _run_step(conn, step, cfg)
        if st == "canceled":
            return 1
        if st != "done":
            rc = 1
    return rc


# --------------------------------------------------------------------------
# 後日(払戻が届いてから)の決済。開催日のジョブとは別に回す。
# --------------------------------------------------------------------------

@dataclass
class SettleConfig:
    """`hro-ops settle-pending` の設定。日付を指定しなければ自分で探す。"""

    dates: list[str] = field(default_factory=list)
    target: str = "vm"                 # 決済を走らせる機(DB しか触らない)
    with_sync: bool = True
    within_days: int = 28
    max_dates: int = 10
    poll_seconds: float = 30.0
    finish_timeout_seconds: float = 3600.0
    dry_run: bool = False
    job_id: int | None = None


def run_settle(cfg: SettleConfig) -> int:
    """払戻を取り込んで、決済待ちの開催日をまとめて片付ける。

    ★**払戻(HR)は開催の3〜5日後にしか配信されない**。開催日ごとに人が思い出して
      押す必要をなくすのが目的なので、日付は自分で探す(live で賭けたのに未決済で、
      かつ nl_hr に払戻がもう入っている日)。
    ★同期を**先に**やる。同期で初めて nl_hr に入る日があるため。
    """
    _log("=== 払戻の取込と決済 ===")
    if cfg.target not in ("vm", "windows"):
        _log(f"✗ 決済の実行先は vm か windows: {cfg.target!r}")
        return 2
    try:
        conn = _connect()
    except Exception as e:      # noqa: BLE001
        _log(f"✗ DB に接続できません: {e}")
        return 0 if cfg.dry_run else 2

    scfg = OrchestratorConfig(date="", close_target=cfg.target,
                              poll_seconds=cfg.poll_seconds,
                              finish_timeout_seconds=cfg.finish_timeout_seconds,
                              job_id=cfg.job_id)
    try:
        if cfg.with_sync:
            sync = settle_steps([], target=cfg.target)[0]
            if cfg.dry_run:
                _log("先に JV-Data を同期します(日付なし=種別ごとの自動差分):")
                print(describe([sync]), flush=True)
            else:
                st = _run_step(conn, sync, scfg)
                if st == "canceled":
                    return 1
                if st != "done":
                    _log("✗ 同期が終わっていないので決済へ進みません"
                         "(払戻が入っていないまま突合しても何も確定しない)")
                    return 1

        today = datetime.now(_JST).strftime("%Y%m%d")
        dates = cfg.dates or pending_settlement_dates(
            conn, today, days=cfg.within_days, limit=cfg.max_dates)
        if not dates:
            _log("決済待ちで払戻が届いている開催日はありません"
                 "(払戻は開催の3〜5日後に配信されます)")
            return 0
        _log(f"決済する開催日 {len(dates)} 件: {', '.join(dates)}")
        steps = settle_steps(dates, target=cfg.target, with_sync=False)
        if cfg.dry_run:
            print(describe(steps), flush=True)
            _log("dry-run のためここまで(ジョブは投入していません)")
            return 0
        rc = 0
        for step in steps:
            st = _run_step(conn, step, scfg)
            if st == "canceled":
                return 1
            if st != "done":
                rc = 1
        return rc
    finally:
        try:
            conn.close()
        except Exception:   # noqa: BLE001
            pass

"""網羅性の記録。年内の全 JRA レースに規則を当てた結果を1行ずつ残す。

★雑所得(ハズレ馬券も経費)の主張で中核になるのは「個々の馬券の的中に着目しない
  網羅的な購入」であること。これは「全部買う」ことではなく、**レースごとの恣意的な
  選択をしていない**こと。同じ規則を全レースに当て、その結果として買った/買わな
  かったが決まっている、という記録がそれを示す。
★だから「買ったレース」だけでなく「当てたが買わなかったレース」と「当てられな
  かったレース」を区別して残す。後者は穴であり、**穴を隠さず理由つきで残すこと**が
  記録全体の信用になる。都合の悪い行を落とした記録は、落としていない行まで疑われる。
★日次でしか作れない。収集の状況もランナーの稼働も後からは分からない。
"""

from __future__ import annotations

BOUGHT = "bought"
NO_CANDIDATE = "no_candidate"
RACE_SKIPPED = "race_skipped"
NO_DATA = "no_data"
NOT_RUNNING = "not_running"

# 「規則を当てた」と言えるもの。網羅率の分子はこれ。
EVALUATED = frozenset({BOUGHT, NO_CANDIDATE, RACE_SKIPPED})

STATUS_JP = {
    BOUGHT: "買った",
    NO_CANDIDATE: "条件を満たす馬がいなかった",
    RACE_SKIPPED: "レース単位で見送り",
    NO_DATA: "スナップショット不足",
    NOT_RUNNING: "ランナーが動いていなかった",
}


def classify(*, n_bought: int, dlogs: list[dict], runner_up: bool) -> tuple[str, str]:
    """1レースの結果を分類する。(status, reason) を返す。

    dlogs は bet_decision_logs の行(selection_id と reason を持つ dict)。
    runner_up は「判断すべき時刻にランナーが動いていたか」。

    ★判定の順番に意味がある。買っていれば他を見る必要はない。判断記録があれば
      規則は当たっている。無いときだけ、ランナーの稼働で「取りに行けなかった」か
      「そもそも動いていなかった」かを分ける。
    """
    if n_bought > 0:
        return BOUGHT, ""
    if dlogs:
        # "*" はレース単位の見送り(窓ずれ/頭数下限/リードの閾値なし/スナップ不足)
        race_level = [d for d in dlogs if d.get("selection_id") == "*"]
        if race_level:
            reason = str(race_level[0].get("reason") or "")
            # スナップ不足はレース単位の見送りではなく「当てられなかった」側
            if "スナップショット" in reason:
                return NO_DATA, reason
            return RACE_SKIPPED, reason
        return NO_CANDIDATE, f"評価 {len(dlogs)} 頭、いずれも規則を満たさず"
    if runner_up:
        return NO_DATA, "ランナーは動いていたが判断の記録が無い"
    return NOT_RUNNING, "この時刻にランナーが動いていなかった"


def summarize(rows: list[dict]) -> dict:
    """網羅率と内訳。**分母は対象レース全部**で、評価できなかったぶんも含める。

    ★分母から穴を外すと網羅率が自動的に100%になり、指標として無意味になる。
    """
    total = len(rows)
    by: dict[str, int] = {}
    n_bought = amount = 0
    for r in rows:
        st = str(r.get("status"))
        by[st] = by.get(st, 0) + 1
        n_bought += int(r.get("n_bought") or 0)
        amount += int(r.get("amount") or 0)
    evaluated = sum(by.get(s, 0) for s in EVALUATED)
    return {
        "races": total,
        "evaluated": evaluated,
        "evaluated_ratio": (evaluated / total) if total else None,
        "bought_races": by.get(BOUGHT, 0),
        "bought_ratio": (by.get(BOUGHT, 0) / total) if total else None,
        "tickets": n_bought,
        "amount": amount,
        "by_status": by,
    }


# --- DB から組む ---------------------------------------------------------

_SQL_RACES = """
SELECT DISTINCT ON (year, month_day, jyo_cd, kaiji, nichiji, race_num)
       year||month_day||jyo_cd||kaiji||nichiji||race_num AS race_id,
       jyo_cd, race_num, hasso_time
FROM nl_ra
WHERE year = %(y)s AND month_day = %(m)s
  AND jyo_cd BETWEEN '01' AND '10'
  AND hasso_time ~ '^[0-9]{4}$'
ORDER BY year, month_day, jyo_cd, kaiji, nichiji, race_num
"""

_SQL_DLOGS = """
SELECT race_id, selection_id, reason FROM bet_decision_logs WHERE budget_key = %s
"""

_SQL_ORDERS = """
SELECT race_id, count(*)::int AS n, sum(amount)::int AS amount,
       min(strategy_version_id) AS sv
FROM bet_orders WHERE budget_key = %s GROUP BY race_id
"""

# ★ランナーの稼働窓。ops_job は started_at / finished_at を持つので、
#   「そのレースの判断時刻に flow ランナーが動いていたか」を**記録から**言える。
#   これが無いと、買っていないレースが「規則で落ちた」のか「取りに行けなかった」のか
#   区別できず、網羅性の主張が弱くなる。
_SQL_RUN_WINDOWS = """
SELECT started_at, coalesce(finished_at, now())
FROM ops_job
WHERE kind = 'flow_day' AND started_at IS NOT NULL
  AND coalesce(args->>'date', '') = %s
ORDER BY started_at
"""


def _post_ts(conn, budget_key: str, hhmm: str):
    """発走時刻(JST HHMM)を timestamptz に。SQL 側で変換させる(TZ を跨がない)。"""
    row = conn.execute(
        "SELECT (to_timestamp(%s,'YYYYMMDDHH24MI')::timestamp AT TIME ZONE 'Asia/Tokyo')",
        (budget_key + hhmm,)).fetchone()
    return row[0] if row else None


def build_coverage(conn, budget_key: str) -> dict:
    """その日の全 JRA レースを分類して race_coverage に書き、集計を返す。

    ★冪等。同じ日を2度走らせても上書きするだけ(封印は別に鎖で守る)。
    """
    y, m = budget_key[:4], budget_key[4:8]
    races = conn.execute(_SQL_RACES, {"y": y, "m": m}).fetchall()
    if not races:
        return {"races": 0, "note": "nl_ra に当日のレースがありません"}

    dlogs: dict[str, list[dict]] = {}
    for rid, sel, reason in conn.execute(_SQL_DLOGS, (budget_key,)).fetchall():
        dlogs.setdefault(rid, []).append({"selection_id": sel, "reason": reason})
    orders = {r[0]: {"n": r[1], "amount": r[2], "sv": r[3]}
              for r in conn.execute(_SQL_ORDERS, (budget_key,)).fetchall()}
    windows = conn.execute(_SQL_RUN_WINDOWS, (budget_key,)).fetchall()
    any_sv = next((o["sv"] for o in orders.values() if o["sv"]), None)

    rows = []
    for race_id, jyo, rno, hhmm in races:
        post = _post_ts(conn, budget_key, hhmm)
        runner_up = any(s <= post <= e for s, e in windows) if post else bool(windows)
        o = orders.get(race_id) or {}
        status, reason = classify(n_bought=int(o.get("n") or 0),
                                  dlogs=dlogs.get(race_id, []),
                                  runner_up=runner_up)
        horses = [d for d in dlogs.get(race_id, []) if d["selection_id"] != "*"]
        rows.append({
            "budget_key": budget_key, "race_id": race_id, "jyo_cd": jyo,
            "race_num": rno, "hasso_time": hhmm, "status": status, "reason": reason,
            "n_horses": len(horses) or None, "n_eligible": None,
            "n_bought": int(o.get("n") or 0), "amount": int(o.get("amount") or 0),
            "strategy_version_id": o.get("sv") or any_sv,
        })

    conn.execute("DELETE FROM race_coverage WHERE budget_key = %s", (budget_key,))
    conn.executemany(
        "INSERT INTO race_coverage(budget_key,race_id,jyo_cd,race_num,hasso_time,"
        " status,reason,n_horses,n_eligible,n_bought,amount,strategy_version_id)"
        " VALUES(%(budget_key)s,%(race_id)s,%(jyo_cd)s,%(race_num)s,%(hasso_time)s,"
        " %(status)s,%(reason)s,%(n_horses)s,%(n_eligible)s,%(n_bought)s,%(amount)s,"
        " %(strategy_version_id)s)", rows)
    return summarize(rows)

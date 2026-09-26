"""開催日の朝に、発注に必要な条件が揃っているかを1発で確認する。

★3プロセスのどれかが死んでいても**静かに0件**になる。2026-09-26 は設定が反映されて
  いないことに1日気付かず、全レースを空振りした。走り出す前に見える形にする。

見るのは4点:
  1. nl_ra に当日のレースがあるか(無いと収集も判断も動かない)
  2. netkeiba が届いているか(信号)
  3. JV-Link の速報が届いているか(**複勝オッズ。無いと全頭落ちる**)
  4. 設定の整合(閾値のキー / リードの大小関係)
"""

from __future__ import annotations

_SQL_RACES = """
SELECT count(*) AS n,
       count(*) FILTER (
         WHERE (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
                AT TIME ZONE 'Asia/Tokyo') > clock_timestamp()) AS upcoming,
       min(hasso_time) AS first_post, max(hasso_time) AS last_post
FROM nl_ra
WHERE year = %(y)s AND month_day = %(m)s AND jyo_cd BETWEEN '01' AND '10'
  AND hasso_time ~ '^[0-9]{4}$'
"""

# ★clock_timestamp() を使う。now() はトランザクション開始時刻で、常駐プロセスだと凍る。
_SQL_FRESH = """
SELECT count(*) AS rows,
       count(DISTINCT year||month_day||jyo_cd||race_num) AS races,
       max({TS}) AS last_at,
       EXTRACT(EPOCH FROM (clock_timestamp() - max({TS})))::int AS age_sec
FROM {TABLE}
WHERE year = %(y)s AND month_day = %(m)s
"""


def check(db, date: str, cfg) -> dict:
    """cfg は DayConfig。lead_seconds は**発注時刻**(agent が締切-N秒から変換したもの)。"""
    key = {"y": date[:4], "m": date[4:8]}
    out: dict = {"date": date, "problems": [], "notes": []}

    r = (db.query(_SQL_RACES, key) or [{}])[0]
    out["races"] = r
    if not (r.get("n") or 0):
        out["problems"].append(
            f"nl_ra に {date} のレースがありません。RACE データの取り込みが要ります")

    for label, table, ts in (("netkeiba", "ts_netkeiba_o1", "observed_at"),
                             ("速報(JV-Link)", "ts_sokuho_o1", "observed_at")):
        try:
            q = _SQL_FRESH.replace("{TABLE}", table).replace("{TS}", ts)
            out[table] = (db.query(q, key) or [{}])[0]
        except Exception as e:   # noqa: BLE001 - テーブルが無い等でも他の確認は続ける
            out[table] = {"error": str(e)[:120]}
            out["problems"].append(f"{label}: {table} を読めません({e})")

    # 設定の整合。★ここが合っていないと「正常に1日走って0件」になる
    want = (int(cfg.flow_lead_seconds) if cfg.flow_source == "netkeiba"
            else int(round(cfg.flow_lead_seconds / 60.0)) * 60)
    keys = sorted((cfg.flow_thresholds or {}))
    out["threshold_key"] = {"want": want, "have": keys}
    if cfg.flow_thresholds and want not in cfg.flow_thresholds:
        out["problems"].append(
            f"閾値のキーに {want} がありません(設定: {keys})。全レース見送りになります")
    # ★DayConfig に act_before_deadline_seconds は無い(agent が lead_seconds へ変換済み)
    act = cfg.lead_seconds
    if not (cfg.flow_lead_seconds > act > cfg.deadline_lead_seconds):
        out["problems"].append(
            f"リードの大小が不正: flow_lead {cfg.flow_lead_seconds} > 投票開始 {act} "
            f"> 締切 {cfg.deadline_lead_seconds} である必要があります")
    out["act_lead"] = act

    # ★netkeiba を使うなら複勝オッズは JV 由来。速報が止まっていると全頭落ちる
    sok = out.get("ts_sokuho_o1") or {}
    if cfg.flow_source == "netkeiba" and not (sok.get("rows") or 0):
        out["problems"].append(
            "速報(ts_sokuho_o1)が空です。netkeiba は単勝しか出さないので、"
            "**複勝オッズが無いと全頭落ちて0件**になります。poll-odds を起動してください")
    nk = out.get("ts_netkeiba_o1") or {}
    if cfg.flow_source == "netkeiba" and not (nk.get("rows") or 0):
        out["problems"].append("netkeiba(ts_netkeiba_o1)が空です。netkeiba-odds を起動してください")
    return out

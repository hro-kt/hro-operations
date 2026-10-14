"""収集が「生きているが何も採れていない」状態を見る。

★いま入っている検知はすべて「プロセスが死んだか」。ところが 2026-10-10 の
  netkeiba はセッションが切れても**プロセスは生きたまま**で、1レース1スナップしか
  返さない状態が9分続いた。ジョブは running のままなのでおまかせの再投入も効かず、
  午前の取りこぼしに気づいたのは後になってからだった。
★沈黙そのものは異常ではない。収集は「発走N分以内」のレースしか見に行かないので、
  レースが遠い時間帯は**何も採れていないのが正常**。異常と言えるのは
  「発走が近いのに採れていない」ときだけ。ここを区別しないと誤報だらけになる。
★片方でも止まると発注は0件になる(信号=netkeiba / 発注する複勝のオッズ=JV-Link)。
  どちらが黙っているかを名指しする。
"""

from __future__ import annotations

SOURCES = (
    ("netkeiba", "ts_netkeiba_o1", "信号(単勝)"),
    ("sokuho", "ts_sokuho_o1", "複勝オッズ(JV-Link)"),
)

# 発走までこの分数以内のレースが在るときだけ「黙っている」を異常とみなす。
# ★収集側の既定(netkeiba --within-minutes 10 / run_odds --within-minutes 20)より
#   内側にする。外側で測ると、まだ取りに行かない時間帯を異常と呼んでしまう。
WINDOW_MIN = 8
# この秒数より長く何も入っていなければ沈黙とみなす。
# ★netkeiba は2秒周期だが**変化したときだけ保存する**ので、無風なら数十秒空く。
#   短すぎると誤報になる。
SILENT_SEC = 180

_SQL_NEXT_POST = """
SELECT min(EXTRACT(EPOCH FROM (
         (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') - clock_timestamp())) / 60.0)
FROM nl_ra
WHERE year=%s AND month_day=%s AND jyo_cd BETWEEN '01' AND '10'
  AND hasso_time ~ '^[0-9]{4}$'
  AND (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
       AT TIME ZONE 'Asia/Tokyo') > clock_timestamp()
"""

_SQL_LAST = """
SELECT EXTRACT(EPOCH FROM (clock_timestamp() - max(observed_at)))::int,
       max(observed_at)::text
FROM {table} WHERE year=%s AND month_day=%s
"""


def check(conn, date: str, *, window_min: int = WINDOW_MIN,
          silent_sec: int = SILENT_SEC) -> dict:
    """収集の健全性。{"mins_to_post":…, "sources":[…], "silent":[…]}。

    ★clock_timestamp() を使う。now() はトランザクション開始時刻で、常駐プロセス
      から呼ぶと凍る。
    """
    y, m = date[:4], date[4:8]
    row = conn.execute(_SQL_NEXT_POST, (y, m)).fetchone()
    mins = float(row[0]) if row and row[0] is not None else None
    out: dict = {"date": date, "mins_to_post": mins, "sources": [], "silent": []}
    # ★発走が遠い/もう無いなら、黙っていて当たり前。異常とは呼ばない。
    due = mins is not None and mins <= window_min
    out["in_window"] = due
    for name, table, label in SOURCES:
        try:
            r = conn.execute(_SQL_LAST.format(table=table), (y, m)).fetchone()
            age = int(r[0]) if r and r[0] is not None else None
            last = r[1] if r else None
        except Exception as e:      # noqa: BLE001 - 表が無い等でも他は見る
            out["sources"].append({"name": name, "label": label, "error": str(e)[:120]})
            continue
        silent = due and (age is None or age > silent_sec)
        out["sources"].append({"name": name, "label": label, "age_sec": age,
                               "last_at": last, "silent": silent})
        if silent:
            out["silent"].append(name)
    return out


def job_kind(source: str) -> str | None:
    """沈黙している信号源を立て直すジョブ。"""
    return {"netkeiba": "netkeiba_odds", "sokuho": "run_odds"}.get(source)


def describe(rep: dict) -> str:
    mins = rep.get("mins_to_post")
    head = (f"次の発走まで {mins:.0f} 分" if mins is not None else "これからの発走なし")
    if not rep.get("in_window"):
        return f"{head}(収集の対象時間外なので、空でも正常)"
    bad = [s for s in rep["sources"] if s.get("silent")]
    if not bad:
        return f"{head} / 収集は動いています"
    return (f"{head} / ★沈黙: "
            + "、".join(f"{s['label']}"
                        + (f"({s['age_sec']}秒前が最後)" if s.get("age_sec") is not None
                           else "(データなし)") for s in bad))

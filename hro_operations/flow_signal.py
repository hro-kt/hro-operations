"""締切直前の単勝プール資金移動シグナル(flow_tan)で複勝の発注候補を作る。

検証結果(2026-09, `hro-backtest/docs/2026-09_flow_signal.md`):
  現象は TYB直前オッズ→確定オッズ で **11年中11年**に再現(+0.144, 対照+0.004)。
  運用形(決定時点で見える情報のみ・払戻は実績)で **複勝 ROI 1.1741 [1.058,1.298]**、
  P(ROI<=1)=0.001、8ヶ月中8ヶ月、的中34.2%、平均odds3.61、月約185本。
  リーク監査済(締切後 0/48,329、実際の決定時点は T−60s)。

スコア: レース内の単勝プール占有率 share_i = (1/tan_odds_i) / Σ_j (1/tan_odds_j) の
        logit 差 = logit(share_i at T−lead) − logit(share_i at T−flow)
        1/tan_odds は単勝プールの占有率そのもの(複勝下限は他馬の組合せに依存し粗い)。
選別: **fit 期間で決めた絶対閾値**を超えたものを全部買う。1レース固定N点だと信号の弱い
      レースでも無理に買って薄まる(実測 1.17→1.006)。効果の大半は「どのレースで賭けるか」。
★モデルを一切使わない。フィットするパラメータも無い。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import timedelta

log = logging.getLogger(__name__)

# ★時刻の扱い: nl_ra.hasso_time(発走 HHMM)も ts_o1.hasso_time(スナップ MMDDHHMI)も **JST**。
#   to_timestamp はセッションのタイムゾーンで解釈するので、セッションが UTC だと 9 時間ずれる。
#   スナップ同士の比較なら同じだけずれて相殺されるが、observed_at(実時刻)と比べると破綻する
#   (実際に「取得の余裕 32,325秒」のような値が出た)。必ず AT TIME ZONE 'Asia/Tokyo' で固定する。

# 学習不要。ts_o1 / ts_sokuho_o1 のどちらからでも同じ量が作れる。
_SRC = {
    "ts": ("ts_o1", "hasso_time"),            # 公式時系列(0B41)。発表時刻の格子
    "sokuho": ("ts_sokuho_o1", "hasso_time"),   # 自前10秒ポーリング(0B30)。基準は発表時刻
}


@dataclass
class FlowConfig:
    lead_seconds: int = 60      # 決定時点 = 発走 − これ秒(0B41 の格子は T−60s)
    flow_minutes: int = 6       # フローの起点 = 発走 − これ分
    threshold: float = 0.0      # スコアの絶対閾値(fit 期間の分位から決めた値)
    source: str = "ts"          # ts | sokuho
    max_odds: float = 0.0       # >0 で複勝オッズ上限(荒れすぎを弾く)


def _logit(x: float, lo: float = 1e-6) -> float:
    x = min(max(x, lo), 1.0 - lo)
    return math.log(x / (1.0 - x))


_SQL_TS = """
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
snap AS (
  SELECT t.umaban, t.tan_odds, t.fuku_odds_low, (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts
  FROM ts_o1 t
  WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
),
-- PostgreSQL は UNION の**前**に ORDER BY を書けない(構文エラー)。DISTINCT ON は
-- ORDER BY と組で意味を持つので、枝ごとに CTE へ切り出す。
late AS (
  SELECT DISTINCT ON (umaban) umaban, tan_odds, fuku_odds_low, ts
  FROM snap, ra WHERE ts <= ra.post - make_interval(secs => %(lead)s)
  ORDER BY umaban, ts DESC
),
early AS (
  SELECT DISTINCT ON (umaban) umaban, tan_odds, fuku_odds_low, ts
  FROM snap, ra WHERE ts <= ra.post - make_interval(mins => %(flow)s)
  ORDER BY umaban, ts DESC
)
SELECT umaban, tan_odds, fuku_odds_low, ts, 'late' AS which FROM late
UNION ALL
SELECT umaban, tan_odds, fuku_odds_low, ts, 'early' AS which FROM early
"""

_SQL_SOKUHO = """
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
snap AS (
  SELECT t.umaban, t.tan_odds, t.fuku_odds_low,
         (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts
  FROM ts_sokuho_o1 t
  WHERE t.hasso_time ~ '^[0-9]{8}$'
    AND (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
),
-- PostgreSQL は UNION の**前**に ORDER BY を書けない(構文エラー)。DISTINCT ON は
-- ORDER BY と組で意味を持つので、枝ごとに CTE へ切り出す。
late AS (
  SELECT DISTINCT ON (umaban) umaban, tan_odds, fuku_odds_low, ts
  FROM snap, ra WHERE ts <= ra.post - make_interval(secs => %(lead)s)
  ORDER BY umaban, ts DESC
),
early AS (
  SELECT DISTINCT ON (umaban) umaban, tan_odds, fuku_odds_low, ts
  FROM snap, ra WHERE ts <= ra.post - make_interval(mins => %(flow)s)
  ORDER BY umaban, ts DESC
)
SELECT umaban, tan_odds, fuku_odds_low, ts, 'late' AS which FROM late
UNION ALL
SELECT umaban, tan_odds, fuku_odds_low, ts, 'early' AS which FROM early
"""


def _hhmmss(v) -> str:
    """理由文用の時刻表記。ts が NULL や文字列でも落とさない(発注を止めないため)。"""
    try:
        return v.strftime("%H:%M:%S")
    except Exception:
        return str(v)


def _num(v) -> float | None:
    s = (str(v) or "").strip()
    if not s.isdigit() or int(s) <= 0:
        return None
    return int(s) / 10.0


def flow_scores(db, race: tuple[str, ...], cfg: FlowConfig) -> dict[str, dict]:
    """{馬番: {'score','fuku_odds','tan_odds','ts_late','ts_early'}}。取れない馬は含めない。"""
    y, m, j, k, n, r = race
    sql = _SQL_TS if cfg.source == "ts" else _SQL_SOKUHO
    rows = db.query(sql, {"y": y, "m": m, "j": j, "k": k, "n": n, "r": r,
                          "lead": cfg.lead_seconds, "flow": cfg.flow_minutes})
    late = {x["umaban"]: x for x in rows if x["which"] == "late"}
    early = {x["umaban"]: x for x in rows if x["which"] == "early"}
    if not late or not early:
        return {}
    s_late = sum(1.0 / o for x in late.values() if (o := _num(x["tan_odds"])))
    s_early = sum(1.0 / o for x in early.values() if (o := _num(x["tan_odds"])))
    if s_late <= 0 or s_early <= 0:
        return {}
    out: dict[str, dict] = {}
    for um, xl in late.items():
        xe = early.get(um)
        tl, te = _num(xl["tan_odds"]), _num(xe["tan_odds"]) if xe else None
        fk = _num(xl["fuku_odds_low"])
        if tl is None or te is None or fk is None:
            continue
        out[um] = {
            "score": _logit((1.0 / tl) / s_late) - _logit((1.0 / te) / s_early),
            "fuku_odds": fk, "tan_odds": tl,
            "ts_late": xl["ts"], "ts_early": xe["ts"],
        }
    return out


_SQL_DIAG_TS = """
SELECT count(*) AS rows,
       count(DISTINCT t.hasso_time) AS snaps,
       count(DISTINCT t.umaban) AS horses,
       min((to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo')) AS first_ts,
       max((to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo')) AS last_ts,
       min(t.hasso_time) AS raw_min, max(t.hasso_time) AS raw_max
FROM ts_o1 t
WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
    = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
"""

_SQL_DIAG_SOKUHO = """
SELECT count(*) AS rows,
       count(DISTINCT t.hasso_time) AS snaps,
       count(DISTINCT t.umaban) AS horses,
       min((to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
            AT TIME ZONE 'Asia/Tokyo')) AS first_ts,
       max((to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
            AT TIME ZONE 'Asia/Tokyo')) AS last_ts,
       min(t.hasso_time) AS raw_min, max(t.hasso_time) AS raw_max
FROM ts_sokuho_o1 t
WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
    = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
"""

_SQL_GRID = """
-- 発走前のスナップショットが「発走の何秒前」に在るか(格子の粗さを見る)
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
sn AS (
  SELECT DISTINCT t.hasso_time,
         (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts
  FROM ts_o1 t
  WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
)
SELECT sn.hasso_time,
       EXTRACT(EPOCH FROM (ra.post - sn.ts)) AS lead_sec
FROM sn, ra
WHERE sn.ts <= ra.post
ORDER BY sn.ts DESC
LIMIT %(lim)s
"""

_SQL_GRID_SOKUHO = """
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
sn AS (
  SELECT DISTINCT t.hasso_time,
         (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts
  FROM ts_sokuho_o1 t
  WHERE t.hasso_time ~ '^[0-9]{8}$'
    AND (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
)
SELECT sn.hasso_time,
       EXTRACT(EPOCH FROM (ra.post - sn.ts)) AS lead_sec
FROM sn, ra
WHERE sn.ts <= ra.post
ORDER BY sn.ts DESC
LIMIT %(lim)s
"""


def snapshot_grid(db, race: tuple[str, ...], cfg: FlowConfig, limit: int = 15) -> list[dict]:
    """発走直前のスナップショットが「何秒前」に在るかを新しい順に返す。

    格子が粗いと、決定時点を早めたときに起点と同じスナップを引いてスコアが 0 になる
    (実際に lead 90/120/180 秒で全部同じ結果=スコアほぼ全ゼロになった)。
    """
    y, m, j, k, n, r = race
    sql = _SQL_GRID if cfg.source == "ts" else _SQL_GRID_SOKUHO
    return db.query(sql, {"y": y, "m": m, "j": j, "k": k, "n": n, "r": r, "lim": limit})


_SQL_POST = """
SELECT hasso_time,
       (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
FROM nl_ra
WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
"""


_SQL_COVERAGE = """
WITH ra AS (
  SELECT year, month_day, jyo_cd, kaiji, nichiji, race_num, hasso_time,
         (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE year=%(y)s AND month_day=%(m)s AND hasso_time ~ '^[0-9]{4}$'
    AND jyo_cd IN ('01','02','03','04','05','06','07','08','09','10')
),
sn AS (
  SELECT t.year, t.month_day, t.jyo_cd, t.kaiji, t.nichiji, t.race_num,
         (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts,
         t.observed_at
  FROM ts_o1 t
  WHERE t.year=%(y)s AND t.month_day=%(m)s
)
SELECT ra.jyo_cd, ra.race_num, ra.hasso_time, ra.post,
       count(sn.ts) AS snaps,
       max(sn.ts) FILTER (WHERE sn.ts <= ra.post - make_interval(secs => %(lead)s)) AS late_ts,
       max(sn.ts) FILTER (WHERE sn.ts <= ra.post - make_interval(mins => %(flow)s)) AS early_ts,
       max(sn.observed_at) FILTER (WHERE sn.ts <= ra.post - make_interval(secs => %(lead)s))
         AS late_fetched_at
FROM ra
LEFT JOIN sn USING (year, month_day, jyo_cd, kaiji, nichiji, race_num)
GROUP BY ra.jyo_cd, ra.race_num, ra.hasso_time, ra.post
ORDER BY ra.jyo_cd, ra.race_num
"""


def flow_coverage(db, date: str, cfg: FlowConfig) -> list[dict]:
    """開催日の全レースについて、決定時点(T−lead)のオッズが**間に合って**取れているかを見る。

    2つは別物なので両方返す:
      - スナップショットの時刻(hasso_time)が T−lead 以前にあるか … 信号を作れるか
      - その行を**いつ取得したか**(observed_at)が T−lead より前か … 締切前に使えたか
        (後から取り込んだ場合、検証はできても当日の発注には間に合っていない)
    """
    rows = db.query(_SQL_COVERAGE, {"y": date[:4], "m": date[4:8],
                                    "lead": cfg.lead_seconds, "flow": cfg.flow_minutes})
    out = []
    for r in rows:
        post, late_ts, fetched = r["post"], r["late_ts"], r["late_fetched_at"]
        decide_at = post - timedelta(seconds=cfg.lead_seconds) if post else None
        out.append({
            "jyo_cd": r["jyo_cd"], "race_num": r["race_num"], "hasso_time": r["hasso_time"],
            "post": post, "snaps": r["snaps"],
            "late_lead_sec": (post - late_ts).total_seconds() if (post and late_ts) else None,
            "has_early": r["early_ts"] is not None,
            # 取得が決定時点に間に合っていたか(正の秒数なら余裕、負なら間に合っていない)
            "fetch_margin_sec": ((decide_at - fetched).total_seconds()
                                 if (decide_at and fetched) else None),
        })
    return out


def flow_diagnose(db, race: tuple[str, ...], cfg: FlowConfig) -> dict:
    """なぜ発注が出ないのかを切り分けるための材料を集める(発注はしない)。

    見るのは3点: (1) 発走時刻が nl_ra に在るか (2) オッズのスナップショットが
    何時から何時まで何本在るか (3) 各馬のスコアと閾値。
    """
    y, m, j, k, n, r = race
    key = {"y": y, "m": m, "j": j, "k": k, "n": n, "r": r}
    post = db.query(_SQL_POST, key)
    diag = db.query(_SQL_DIAG_TS if cfg.source == "ts" else _SQL_DIAG_SOKUHO, key)
    scores = flow_scores(db, race, cfg)
    return {
        "race_id": "".join(race),
        "post": post[0] if post else None,
        "snapshots": diag[0] if diag else None,
        "scores": scores,
        "cutoff_late_seconds": cfg.lead_seconds,
        "cutoff_early_minutes": cfg.flow_minutes,
        "threshold": cfg.threshold,
        "n_above": sum(1 for d in scores.values() if d["score"] >= cfg.threshold),
    }


def _spearman(a: list[float], b: list[float]) -> float | None:
    """順位相関。scipy を使わない(依存を増やさない)。"""
    n = len(a)
    if n < 3:
        return None

    def rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        for pos, i in enumerate(order):
            r[i] = float(pos)
        return r

    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    dbb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return num / (da * dbb) if da and dbb else None


def lead_scan(db, races, leads: list[int], cfg: FlowConfig,
              ref_source: str = "ts", ref_lead: int = 60) -> list[dict]:
    """決定時点を早めたときに信号がどれだけ保たれるかを測る。

    基準は「検証で使った時点」(既定: 公式時系列の発走60秒前)。各 lead について
      - 順位相関(基準との一致)
      - 回帰の傾き(尺度の違い。絶対閾値をそのまま使えるか)
      - 閾値で選ぶ馬の重なり(Jaccard)
    をレース横断で集計する。ROI は測れない(必要な履歴が無い)ので、信号の保存度で判断する。
    """
    ref_cfg = FlowConfig(lead_seconds=ref_lead, flow_minutes=cfg.flow_minutes,
                         threshold=cfg.threshold, source=ref_source)
    out = []
    for lead in leads:
        c = FlowConfig(lead_seconds=lead, flow_minutes=cfg.flow_minutes,
                       threshold=cfg.threshold, source=cfg.source)
        rhos: list[float] = []
        xs: list[float] = []
        ys: list[float] = []
        inter = union = n_ref = n_cur = n_races = 0
        for race in races:
            ref = flow_scores(db, race, ref_cfg)
            cur = flow_scores(db, race, c)
            common = sorted(set(ref) & set(cur))
            if len(common) < 3:
                continue
            n_races += 1
            a = [ref[u]["score"] for u in common]
            b = [cur[u]["score"] for u in common]
            rho = _spearman(a, b)
            if rho is not None:
                rhos.append(rho)
            xs += a
            ys += b
            sa = {u for u in ref if ref[u]["score"] >= cfg.threshold}
            sb = {u for u in cur if cur[u]["score"] >= cfg.threshold}
            n_ref += len(sa)
            n_cur += len(sb)
            inter += len(sa & sb)
            union += len(sa | sb)
        slope = None
        if len(xs) >= 3:
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            if sxx:
                slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        out.append({
            "lead": lead, "races": n_races,
            "rho": (sum(rhos) / len(rhos)) if rhos else None,
            "slope": slope,
            "n_ref": n_ref, "n_cur": n_cur,
            "jaccard": (inter / union) if union else None,
        })
    return out


def flow_orders(db, race: tuple[str, ...], cfg: FlowConfig, amount: int, model_version: str):
    """閾値を超えた馬の複勝 BetOrder を作る。モデルは使わない。"""
    from hro_moneymanager.models import BetOrder

    race_id = "".join(race)
    sc = flow_scores(db, race, cfg)
    if not sc:
        log.info("%s: flow スコア算出不可(スナップショット不足)", race_id)
        return []
    orders = []
    for um, d in sorted(sc.items(), key=lambda kv: -kv[1]["score"]):
        if d["score"] < cfg.threshold:
            continue
        if cfg.max_odds > 0 and d["fuku_odds"] > cfg.max_odds:
            continue
        orders.append(BetOrder(
            race_id=race_id, selection_id=um, bet_type="place", amount=amount,
            probability=0.0,                 # flow は確率を推定しない(順位/閾値で選ぶ)
            odds=d["fuku_odds"],
            expected_return=0.0, edge=0.0, kelly_fraction=0.0,
            model_version=model_version,
            reason=(f"flow_tan={d['score']:+.4f}>={cfg.threshold:+.4f} "
                    f"late={_hhmmss(d['ts_late'])} early={_hhmmss(d['ts_early'])} "
                    f"src={cfg.source}"),
        ))
    log.info("%s: flow 候補 %d/%d 頭 (閾値 %+.4f, src=%s)",
             race_id, len(orders), len(sc), cfg.threshold, cfg.source)
    return orders

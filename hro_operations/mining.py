"""JRA-VAN のデータマイニング予想(DM/TM)を評価する。

★狙い
  終盤に動く金が「何を見ているのか」を特定したい。DM/TM は JRA-VAN 自身が出す
  事前予想で、nl_dm は 2015年から40,564レース分ある(我々が扱ってきたどのデータより桁違い)。
  終盤のフローが DM/TM の方向に動いているなら、**同じ判断を数分早くできる**。
  動いていないなら、終盤の金は DM/TM に無い情報で動いていると確定する。

★先に潰すこと(順序を間違えない)
  1. **本当に事前予想か**。nl_dm の make_date はレース日の数日後(RACE蓄積版)。
     中身が事前予想なら研究に使えるが、結果が漏れていたら全部無意味になる。
     → 着順との順位相関が「市場より明確に高い」なら漏れを疑う。
  2. **ライブで同じものが使えるか**。蓄積版が前日予想か直前予想かは data_kubun からは
     区別できない。研究で直前版・実戦で前日版、という食い違いは静かに効く。
"""

from __future__ import annotations

from dataclasses import dataclass

from .flow_signal import _num, _spearman


@dataclass
class MiningConfig:
    lead_seconds: int = 60      # 市場の比較時点(発走-これ秒)
    flow_minutes: int = 6       # flow の起点
    source: str = "ts"


# 1レース1行にまとめず、馬ごとの行で返す。相関はレース単位で取る。
_SQL = """
WITH ra AS (
  SELECT year, month_day, jyo_cd, kaiji, nichiji, race_num,
         (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE year||month_day BETWEEN %(d0)s AND %(d1)s
    AND jyo_cd BETWEEN '01' AND '10'          -- ★JRA のみ(地方/海外を混ぜない)
    AND hasso_time ~ '^[0-9]{4}$'
),
late AS (
  SELECT DISTINCT ON (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban)
         t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
         t.tan_odds AS t1
  FROM {TABLE} t JOIN ra USING (year,month_day,jyo_cd,kaiji,nichiji,race_num)
  WHERE t.hasso_time ~ '^[0-9]{8}$'
    AND (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
         AT TIME ZONE 'Asia/Tokyo') <= ra.post - make_interval(secs => %(lead)s)
  ORDER BY t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
           t.hasso_time DESC
),
early AS (
  SELECT DISTINCT ON (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban)
         t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
         t.tan_odds AS t0
  FROM {TABLE} t JOIN ra USING (year,month_day,jyo_cd,kaiji,nichiji,race_num)
  WHERE t.hasso_time ~ '^[0-9]{8}$'
    AND (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
         AT TIME ZONE 'Asia/Tokyo') <= ra.post - make_interval(mins => %(flow)s)
  ORDER BY t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
           t.hasso_time DESC
)
SELECT l.year||l.month_day||l.jyo_cd||l.kaiji||l.nichiji||l.race_num AS rid,
       l.umaban, l.t1, e.t0,
       dm.yoso_soha_time AS dm_time, tm.yosoku_score AS tm_score,
       se.kakutei_jyuni AS finish, se.i_jyo_cd,
       h.pay AS fuku_pay
FROM late l
JOIN early e USING (year,month_day,jyo_cd,kaiji,nichiji,race_num,umaban)
LEFT JOIN nl_dm dm
  ON (dm.year,dm.month_day,dm.jyo_cd,dm.race_num,dm.umaban)
   = (l.year,l.month_day,l.jyo_cd,l.race_num,l.umaban)
LEFT JOIN nl_tm tm
  ON (tm.year,tm.month_day,tm.jyo_cd,tm.race_num,tm.umaban)
   = (l.year,l.month_day,l.jyo_cd,l.race_num,l.umaban)
LEFT JOIN nl_se se
  ON (se.year,se.month_day,se.jyo_cd,se.kaiji,se.nichiji,se.race_num,se.umaban)
   = (l.year,l.month_day,l.jyo_cd,l.kaiji,l.nichiji,l.race_num,l.umaban)
LEFT JOIN nl_hr h
  ON (h.year,h.month_day,h.jyo_cd,h.kaiji,h.nichiji,h.race_num)
   = (l.year,l.month_day,l.jyo_cd,l.kaiji,l.nichiji,l.race_num)
 AND h.bet_type = 'fuku'
 AND regexp_replace(h.kumi,'[^0-9]','','g') = l.umaban
"""



# --------------------------------------------------------------------------- #
# flow の分解: DM で説明できる部分 / できない残差
#
# ★決定的な問い: flow_tan の利益はどちらから来ているのか。
#   DM由来なら T-360s の時点で同じ判断ができ、締切直前の綱渡りをやめられる。
#   残差由来なら、終盤の金は DM に無い情報で動いており、追従を速くするしかない。
#
# ★レース内で最小二乗を当てる(レースごとに頭数も尺度も違うため、全体で1本の回帰を
#   当てると頭数の多いレースに引きずられる)。
# --------------------------------------------------------------------------- #
def _zscore(v: list[float]) -> list[float] | None:
    n = len(v)
    if n < 3:
        return None
    m = sum(v) / n
    sd = (sum((x - m) ** 2 for x in v) / n) ** 0.5
    return None if sd <= 0 else [(x - m) / sd for x in v]


def decompose_race(use: list[dict]) -> list[dict] | None:
    """各馬に flow / dm_part(DMで説明できる分) / resid(残差) を付ける。"""
    # DM は予想走破タイムで**小さいほど強い**。強さの向きに揃えるため符号反転。
    dmz = _zscore([-float(x["dm"]) for x in use])
    fz = [x["flow"] for x in use]
    if dmz is None:
        return None
    n = len(use)
    mf = sum(fz) / n
    cov = sum(d * (f - mf) for d, f in zip(dmz, fz)) / n
    var = sum(d * d for d in dmz) / n          # z なので ~1 だが厳密に取る
    b = cov / var if var > 0 else 0.0
    out = []
    for x, d, f in zip(use, dmz, fz):
        part = b * d
        out.append({**x, "dm_z": d, "dm_part": part, "resid": f - mf - part})
    return out


def _int(v) -> int | None:
    s = (str(v) or "").strip()
    return int(s) if s.isdigit() and int(s) > 0 else None


def evaluate(db, d_from: str, d_to: str, cfg: MiningConfig, *,
             amount: int = 100) -> dict:
    """DM/TM が (1) 着順を当てるか (2) 終盤のフローを説明するか を同一レースで測る。"""
    from .flow_signal import _logit, summarize_bets

    table = "ts_o1" if cfg.source == "ts" else "ts_sokuho_o1"
    rows = db.query(_SQL.replace("{TABLE}", table),
                    {"d0": d_from, "d1": d_to,
                     "lead": cfg.lead_seconds, "flow": cfg.flow_minutes})
    by_race: dict[str, list[dict]] = {}
    for r in rows:
        by_race.setdefault(r["rid"], []).append(r)

    # レースごとの順位相関を貯める
    rho = {"dm_finish": [], "tm_finish": [], "mkt_finish": [],
           "dm_flow": [], "tm_flow": [], "dm_mkt": []}
    dm_bets: list[tuple] = []
    n_races = n_used = 0
    for rid, rs in by_race.items():
        n_races += 1
        use = []
        for x in rs:
            t1, t0 = _num(x["t1"]), _num(x["t0"])
            dm, tm = _int(x["dm_time"]), _int(x["tm_score"])
            fin = _int(x["finish"])
            if None in (t1, t0, dm, tm, fin):
                continue
            use.append({**x, "t1": t1, "t0": t0, "dm": dm, "tm": tm, "fin": fin})
        if len(use) < 5:
            continue
        s1 = sum(1.0 / x["t1"] for x in use)
        s0 = sum(1.0 / x["t0"] for x in use)
        if s1 <= 0 or s0 <= 0:
            continue
        n_used += 1
        for x in use:
            x["flow"] = _logit((1.0 / x["t1"]) / s1) - _logit((1.0 / x["t0"]) / s0)

        fin = [x["fin"] for x in use]
        # ★DM は予想走破タイムなので**小さいほど強い**。着順も小さいほど強いので同符号。
        dm = [x["dm"] for x in use]
        # ★TM は予測スコアなので**大きいほど強い**。着順と向きを合わせるため符号反転。
        tm = [-x["tm"] for x in use]
        mkt = [x["t1"] for x in use]            # 単勝オッズ: 小さいほど強い
        flow = [-x["flow"] for x in use]        # flow: 大きいほど強い → 反転
        for k, a, b in (("dm_finish", dm, fin), ("tm_finish", tm, fin),
                        ("mkt_finish", mkt, fin), ("dm_flow", dm, flow),
                        ("tm_flow", tm, flow), ("dm_mkt", dm, mkt)):
            v = _spearman(a, b)
            if v is not None:
                rho[k].append(v)

        # DM 最上位(予想タイム最小)の複勝を買ったら、という粗い確認
        best = min(use, key=lambda x: x["dm"])
        if str(best.get("i_jyo_cd") or "").strip() in ("1", "2", "3"):
            dm_bets.append((rid, amount, amount, True))
        else:
            p = best["fuku_pay"]
            dm_bets.append((rid, amount, 0 if p in (None, "") else
                            int(round(int(p) * amount / 100)), False))

    def _avg(v):
        return sum(v) / len(v) if v else None

    rep = {"races_seen": n_races, "races_used": n_used,
           "rho": {k: _avg(v) for k, v in rho.items()},
           "dm_top1": summarize_bets(dm_bets, races=n_used, races_scored=n_used)}
    return rep


def decompose(db, d_from: str, d_to: str, cfg: MiningConfig, *,
              quantile: float = 0.95, amount: int = 100) -> dict:
    """flow / DMで説明できる部分 / 残差 を**同一レース**で比較する。"""
    from .flow_signal import _logit, summarize_bets

    table = "ts_o1" if cfg.source == "ts" else "ts_sokuho_o1"
    rows = db.query(_SQL.replace("{TABLE}", table),
                    {"d0": d_from, "d1": d_to,
                     "lead": cfg.lead_seconds, "flow": cfg.flow_minutes})
    by_race: dict[str, list[dict]] = {}
    for r in rows:
        by_race.setdefault(r["rid"], []).append(r)

    kept: list[tuple[str, list[dict]]] = []
    n_seen = n_skip = n_unsettled = 0
    for rid, rs in by_race.items():
        n_seen += 1
        use = []
        for x in rs:
            t1, t0 = _num(x["t1"]), _num(x["t0"])
            dm = _int(x["dm_time"])
            if None in (t1, t0, dm):
                continue
            use.append({**x, "t1": t1, "t0": t0, "dm": dm})
        if len(use) < 5:
            n_skip += 1
            continue
        s1 = sum(1.0 / x["t1"] for x in use)
        s0 = sum(1.0 / x["t0"] for x in use)
        if s1 <= 0 or s0 <= 0:
            n_skip += 1
            continue
        for x in use:
            x["flow"] = _logit((1.0 / x["t1"]) / s1) - _logit((1.0 / x["t0"]) / s0)
        # ★払戻が1行も無いレースは未確定。外れとして数えない。
        if all(x["fuku_pay"] in (None, "") for x in use):
            n_unsettled += 1
            continue
        d = decompose_race(use)
        if d is None:
            n_skip += 1
            continue
        kept.append((rid, d))

    def _run(key: str, label: str) -> dict:
        vals = sorted(x[key] for _rid, use in kept for x in use)
        if not vals:
            return {"label": label, "bets": 0, "roi": 0.0}
        thr = vals[min(len(vals) - 1, int(len(vals) * quantile))]
        bets = []
        for rid, use in kept:
            for x in use:
                if x[key] < thr:
                    continue
                if str(x.get("i_jyo_cd") or "").strip() in ("1", "2", "3"):
                    bets.append((rid, amount, amount, True))   # 返還
                    continue
                p = x["fuku_pay"]
                bets.append((rid, amount, 0 if p in (None, "") else
                             int(round(int(p) * amount / 100)), False))
        rep = summarize_bets(bets, races=len(kept), races_scored=len(kept))
        rep["label"] = label
        rep["threshold"] = thr
        return rep

    return {"races_seen": n_seen, "races_used": len(kept),
            "skipped": n_skip, "unsettled": n_unsettled,
            "flow": _run("flow", "flow(そのまま)"),
            "dm_part": _run("dm_part", "DMで説明できる分"),
            "resid": _run("resid", "残差(DMで説明できない分)"),
            "dm_z": _run("dm_z", "DM単体(強さ順)")}

"""複数ホライズンの flow を同一レースで比較する。

★いまの flow_tan は「起点 T-360s → 決定 T-75s」の**差分1本**しか見ていない。
  価格経路には他にも情報がある(直近1分の動き / 加速しているか / 長い目で見た動き)。
  どのホライズンが効くのか、組み合わせると1本を超えるのかを測る。

★比較は必ず**同一レース**で行う。別々に走らせた数字を並べると、信号の差と
  レース構成の差が混ざる(2026-09-23 に ts_o2 391レース と ts_o1 877レースを
  比べて読み違えた)。ここでは「全ホライズンがスコアを作れて決済済み」のレース
  だけを土俵にし、閾値も**その土俵の中で**同じ分位から取る(買う本数を揃える)。

★払戻は確定複勝(パリミュチュエル)。判断時のオッズでは払われない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .flow_signal import _logit, _num, _tan_reader, summarize_bets
from .money_signal import _SQL_SCRATCH, _SQL_SETTLE, _settlement

# 信号源 → (テーブル, 観測時刻の式)
_SRC = {
    "ts": ("ts_o1", "(to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp"
                    " AT TIME ZONE 'Asia/Tokyo')"),
    "sokuho": ("ts_sokuho_o1", "(to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp"
                               " AT TIME ZONE 'Asia/Tokyo')"),
    "netkeiba": ("ts_netkeiba_o1", "t.observed_at"),
}


@dataclass
class HorizonConfig:
    source: str = "ts"
    lead_seconds: int = 60                     # 決定時点(発走-これ秒)
    origins: list[int] = field(default_factory=lambda: [120, 180, 360, 600, 900])
    window_tolerance_sec: int = 60
    quantile: float = 0.95


_SQL_MULTI = """
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
cuts AS (SELECT unnest(%(cuts)s::int[]) AS cut),
snap AS (
  SELECT t.umaban, t.tan_odds, t.fuku_odds_low, {TS} AS ts
  FROM {TABLE} t
  WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    {EXTRA}
)
-- 各カットオフごとに、その時刻以前で最新のスナップを馬ごとに1本ずつ取る
SELECT DISTINCT ON (c.cut, s.umaban)
       c.cut, s.umaban, s.tan_odds, s.fuku_odds_low,
       EXTRACT(EPOCH FROM (ra.post - s.ts))::int AS lead_sec
FROM cuts c, snap s, ra
WHERE s.ts <= ra.post - make_interval(secs => c.cut)
ORDER BY c.cut, s.umaban, s.ts DESC
"""

_SQL_RACES = """
SELECT DISTINCT t.year, t.month_day, t.jyo_cd, t.kaiji, t.nichiji, t.race_num
FROM {TABLE} t
JOIN nl_ra ra USING (year, month_day, jyo_cd, kaiji, nichiji, race_num)
WHERE t.year||t.month_day BETWEEN %(d0)s AND %(d1)s
  AND t.jyo_cd BETWEEN '01' AND '10'          -- ★JRA のみ(地方/海外を混ぜない)
  AND ra.hasso_time ~ '^[0-9]{4}$'
ORDER BY 1,2,3,4,5,6
"""


def _sql(cfg: HorizonConfig, template: str) -> str:
    table, ts = _SRC[cfg.source]
    extra = ("" if cfg.source == "netkeiba"
             else "AND t.hasso_time ~ '^[0-9]{8}$'")
    return (template.replace("{TABLE}", table).replace("{TS}", ts)
            .replace("{EXTRA}", extra))


def multi_scores(db, race: tuple[str, ...], cfg: HorizonConfig) -> dict[int, dict[str, float]]:
    """{起点リード: {馬番: スコア}}。1つでも欠けたら {} を返す(比較を揃えるため)。"""
    y, m, j, k, n, r = race
    cuts = [cfg.lead_seconds] + list(cfg.origins)
    rows = db.query(_sql(cfg, _SQL_MULTI),
                    {"y": y, "m": m, "j": j, "k": k, "n": n, "r": r, "cuts": cuts})
    tan_of = _tan_reader(cfg.source)
    by_cut: dict[int, dict[str, dict]] = {}
    for x in rows:
        by_cut.setdefault(int(x["cut"]), {})[x["umaban"]] = x
    if set(by_cut) != set(cuts):
        return {}

    def _shares(cut: int):
        d = by_cut[cut]
        # ★実効リードが意図から外れたら使わない。格子に穴があると別の時点を見ることになり、
        #   尺度が変わって閾値が比較できなくなる。
        lead = next(iter(d.values())).get("lead_sec")
        if lead is not None and abs(lead - cut) > cfg.window_tolerance_sec:
            return None
        odds = {um: o for um, x in d.items() if (o := tan_of(x["tan_odds"]))}
        tot = sum(1.0 / o for o in odds.values())
        return {um: (1.0 / o) / tot for um, o in odds.items()} if tot > 0 else None

    late = _shares(cfg.lead_seconds)
    if late is None:
        return {}
    # ★複勝オッズ(発注する券種)が無い馬は落とす。netkeiba でも複勝は JV 由来。
    fuku = {um: f for um, x in by_cut[cfg.lead_seconds].items()
            if (f := _num(x["fuku_odds_low"]))}
    out: dict[int, dict[str, float]] = {}
    for cut in cfg.origins:
        early = _shares(cut)
        if early is None:
            return {}
        sc = {um: _logit(late[um]) - _logit(early[um])
              for um in late if um in early and um in fuku}
        if not sc:
            return {}
        out[cut] = sc
    return out


def evaluate(db, d_from: str, d_to: str, cfg: HorizonConfig, *,
             amount: int = 100, progress=None) -> dict:
    """ホライズン別 + 組み合わせの回収率を、同一レースで比較する。"""
    races = [(r["year"], r["month_day"], r["jyo_cd"], r["kaiji"], r["nichiji"], r["race_num"])
             for r in db.query(_sql(cfg, _SQL_RACES), {"d0": d_from, "d1": d_to})]
    pay, settled, refund = _settlement(db, d_from, d_to)

    kept: list[tuple[str, dict[int, dict[str, float]]]] = []
    n_seen = n_nosc = n_unsettled = 0
    for i, race in enumerate(races):
        if progress is not None and i % 100 == 0:
            progress(i, len(races))
        n_seen += 1
        sc = multi_scores(db, race, cfg)
        if not sc:
            n_nosc += 1
            continue
        rid = "".join(race)
        if rid not in settled:
            n_unsettled += 1
            continue
        kept.append((rid, sc))

    def _thr(cut: int) -> float:
        vals = sorted(v for _rid, sc in kept for v in sc[cut].values())
        return vals[min(len(vals) - 1, int(len(vals) * cfg.quantile))] if vals else 0.0

    thr = {cut: _thr(cut) for cut in cfg.origins}

    def _settle(picks: set, label: str) -> dict:
        bets = []
        for rid, um in picks:
            if um in refund.get(rid, ()):
                bets.append((rid, amount, amount, True))
                continue
            p = pay.get(rid, {}).get(um)
            bets.append((rid, amount, 0 if p in (None, "") else
                         int(round(int(p) * amount / 100)), False))
        rep = summarize_bets(bets, races=len(kept), races_scored=len(kept))
        rep["label"] = label
        return rep

    picks = {cut: {(rid, um) for rid, sc in kept for um, v in sc[cut].items() if v >= thr[cut]}
             for cut in cfg.origins}

    # 組み合わせ: 各ホライズンの閾値で正規化して平均し、その分位で切る。
    # ★単純な和だとホライズンごとに尺度が違う(窓が長いほど大きく出る)ため揃えてから足す。
    combo_vals: dict[tuple[str, str], float] = {}
    for rid, sc in kept:
        for um in sc[cfg.origins[0]]:
            zs = [sc[c][um] / thr[c] for c in cfg.origins if thr[c] > 0 and um in sc[c]]
            if len(zs) == len(cfg.origins):
                combo_vals[(rid, um)] = sum(zs) / len(zs)
    cv = sorted(combo_vals.values())
    combo_thr = cv[min(len(cv) - 1, int(len(cv) * cfg.quantile))] if cv else 0.0

    out = {"races_seen": n_seen, "races_used": len(kept),
           "no_score": n_nosc, "unsettled": n_unsettled, "thresholds": thr,
           "per_horizon": {cut: _settle(picks[cut], f"T-{cut}s → T-{cfg.lead_seconds}s")
                           for cut in cfg.origins},
           "all": _settle(set.intersection(*picks.values()) if picks else set(),
                          "全ホライズンで閾値超え(AND)"),
           "any": _settle(set.union(*picks.values()) if picks else set(),
                          "いずれかで閾値超え(OR)"),
           "combo": _settle({k for k, v in combo_vals.items() if v >= combo_thr},
                            "正規化して平均(combo)")}
    return out

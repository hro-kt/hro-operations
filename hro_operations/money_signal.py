"""late money(締切直前に入ってくる金)を**金額**で測る信号。

★flow_tan との違い
  flow_tan は単勝シェアの変化 logit(share_late) - logit(share_early) を見る。
  シェアは相対量なので「i に金が入った」と「j から金が抜けた」を区別できない。
  さらに**プールがどれだけ膨らんだか**が落ちる。同じシェア変化でも、プールが
  1.3倍になる中で起きたのか横ばいで起きたのかで意味がまるで違う。

★なぜ金額が復元できるか
  単一オッズ型(馬連/馬単/三連複/三連単)は 1組=1プールなので
      票数_c = プール総額 × (1 - 控除率) / オッズ_c
  が厳密に成立する。プール総額は JV のオッズレコード末尾の合計票数(vote 列)にあり、
  ts_o2 / ts_sokuho_o2..o6 に時系列で入っている(実測 2026-08-16 三連複:
  15:54 の 293,186 から 16:24 の 374,766 まで単調増加)。
  控除率は定数なので、**比を取る限り知らなくてよい**。

★馬ごとに落とす
  M_i(t) = Σ_{c ∋ i} プール(t)/オッズ_c(t)。組はその構成馬すべてに数えるので
  Σ_i M_i = (1組の頭数) × Σ_c m_c になるが、シェアを取るときに約分される。

  score_i = logit( ΔM_i / ΔM_総 ) - logit( M_i(early) / M_総(early) )

  = 「**新しく入った金**のうち i に向かった割合」が「**既にあった金**の割合」を
    どれだけ上回るか。late money がどこへ行ったかを直接測る。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .flow_signal import _logit, summarize_bets

# 賭式ごとの (テーブル, kumi の1馬あたり桁数, 1組の頭数)
POOLS = {
    "umaren": ("ts_o2", 2, 2),            # 馬連。0B42 で過去約1年を埋め戻せる
    "umaren_sokuho": ("ts_sokuho_o2", 2, 2),
    "sanrenpuku": ("ts_sokuho_o5", 2, 3),  # 三連複。自前ポーリング分のみ
}


@dataclass
class MoneyConfig:
    lead_seconds: int = 60
    flow_minutes: int = 6
    pool: str = "umaren"
    threshold: float = 0.0
    thresholds: dict[int, float] | None = None
    window_tolerance_sec: int = 60
    # ★窓の間にプールがほとんど増えていないと ΔM は雑音しか含まない。
    #   増分がプールの何%以上あれば使うか。0 で無効。
    min_pool_growth: float = 0.005


_SQL = """
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
snap AS (
  SELECT t.kumi, t.odds, t.vote, t.hasso_time,
         (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts
  FROM {TABLE} t
  WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND t.hasso_time ~ '^[0-9]{8}$'
),
pick AS (
  SELECT
    (SELECT max(hasso_time) FROM snap, ra
      WHERE snap.ts <= ra.post - make_interval(secs => %(lead)s)) AS ht_late,
    (SELECT max(hasso_time) FROM snap, ra
      WHERE snap.ts <= ra.post - make_interval(mins => %(flow)s)) AS ht_early
)
SELECT s.kumi, s.odds, s.vote, s.ts,
       EXTRACT(EPOCH FROM (ra.post - s.ts))::int AS lead_sec,
       CASE WHEN s.hasso_time = p.ht_late THEN 'late' ELSE 'early' END AS which
FROM snap s, pick p, ra
WHERE s.hasso_time IN (p.ht_late, p.ht_early)
  AND p.ht_late IS NOT NULL AND p.ht_early IS NOT NULL
  AND p.ht_late <> p.ht_early
"""


def _odds(v) -> float | None:
    """JV のオッズ(10倍の整数文字列)→ 倍率。"""
    s = (str(v) or "").strip()
    if not s.isdigit() or int(s) <= 0:
        return None
    return int(s) / 10.0


def _pool(v) -> float | None:
    s = (str(v) or "").strip()
    if not s.isdigit() or int(s) <= 0:
        return None
    return float(int(s))


def _horses(kumi: str, width: int, n: int) -> list[str] | None:
    """'0103' → ['01','03'] / '010308' → ['01','03','08']。"""
    s = (kumi or "").strip()
    if len(s) < width * n:
        return None
    out = [s[i * width:(i + 1) * width] for i in range(n)]
    return out if all(x.isdigit() and int(x) > 0 for x in out) else None


def _money_by_horse(rows, width: int, n: int) -> tuple[dict[str, float], float] | None:
    """{馬番: その馬を含む組への投入金額の合計} と プール総額。

    金額 ∝ プール/オッズ。控除率は定数なので比では消える。
    """
    pool = None
    acc: dict[str, float] = {}
    for x in rows:
        o = _odds(x["odds"])
        if o is None:
            continue
        if pool is None:
            pool = _pool(x["vote"])
        hs = _horses(x["kumi"], width, n)
        if hs is None:
            continue
        m = 1.0 / o                      # プールは後段で共通に掛ける(比に効かない)
        for h in hs:
            acc[h] = acc.get(h, 0.0) + m
    if pool is None or pool <= 0 or not acc:
        return None
    return {h: v * pool for h, v in acc.items()}, pool


def money_scores(db, race: tuple[str, ...], cfg: MoneyConfig) -> dict[str, dict]:
    """{馬番: {'score','money_late','money_early','d_share','pool_growth',...}}。"""
    y, m, j, k, n, r = race
    spec = POOLS.get(cfg.pool)
    if spec is None:
        raise ValueError(f"不明なプール: {cfg.pool!r} ({'|'.join(POOLS)})")
    table, width, per_combo = spec
    rows = db.query(_SQL.replace("{TABLE}", table),
                    {"y": y, "m": m, "j": j, "k": k, "n": n, "r": r,
                     "lead": cfg.lead_seconds, "flow": cfg.flow_minutes})
    late_rows = [x for x in rows if x["which"] == "late"]
    early_rows = [x for x in rows if x["which"] == "early"]
    if not late_rows or not early_rows:
        return {}
    a = _money_by_horse(late_rows, width, per_combo)
    b = _money_by_horse(early_rows, width, per_combo)
    if a is None or b is None:
        return {}
    m_late, pool_late = a
    m_early, pool_early = b
    # ★プールが増えていない窓は ΔM が雑音しか含まない。買う理由にならない。
    growth = (pool_late - pool_early) / pool_early if pool_early > 0 else 0.0
    if cfg.min_pool_growth > 0 and growth < cfg.min_pool_growth:
        return {}

    # ★金は引き出せないので ΔM は本来非負。負は取得の揺らぎなので 0 に丸める。
    d = {h: max(0.0, m_late.get(h, 0.0) - v) for h, v in m_early.items()}
    d_total = sum(d.values())
    e_total = sum(m_early.values())
    if d_total <= 0 or e_total <= 0:
        return {}

    lead_late = late_rows[0].get("lead_sec")
    lead_early = early_rows[0].get("lead_sec")
    out: dict[str, dict] = {}
    for h, me in m_early.items():
        out[h] = {
            "score": _logit(d[h] / d_total) - _logit(me / e_total),
            "d_share": d[h] / d_total, "base_share": me / e_total,
            "money_late": m_late.get(h, 0.0), "money_early": me,
            "pool_late": pool_late, "pool_early": pool_early, "pool_growth": growth,
            "lead_late": lead_late, "lead_early": lead_early,
            "window_sec": (lead_early - lead_late
                           if lead_late is not None and lead_early is not None else None),
            "ts_late": late_rows[0]["ts"], "ts_early": early_rows[0]["ts"],
        }
    return out


def threshold_from(db, races, cfg: MoneyConfig, quantile: float = 0.95) -> dict:
    """flow_signal.threshold_from と同じ規則(fit 期間の上側分位)。"""
    vals: list[float] = []
    n_races = n_skewed = 0
    want = cfg.flow_minutes * 60 - cfg.lead_seconds
    for race in races:
        sc = money_scores(db, race, cfg)
        if not sc:
            continue
        got = next(iter(sc.values())).get("window_sec")
        if got is not None and abs(got - want) > cfg.window_tolerance_sec:
            n_skewed += 1
            continue
        n_races += 1
        vals += [d["score"] for d in sc.values()]
    if not vals:
        return {"threshold": None, "n": 0, "races": 0, "n_above": 0,
                "races_skewed_window": n_skewed}
    vals.sort()
    thr = vals[min(len(vals) - 1, int(len(vals) * quantile))]
    return {"threshold": thr, "n": len(vals), "races": n_races,
            "n_above": sum(1 for v in vals if v >= thr), "quantile": quantile,
            "races_skewed_window": n_skewed}


__all__ = ["MoneyConfig", "POOLS", "money_scores", "threshold_from", "summarize_bets",
           "math"]

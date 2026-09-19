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

log = logging.getLogger(__name__)

# 学習不要。ts_o1 / ts_sokuho_o1 のどちらからでも同じ量が作れる。
_SRC = {
    "ts": ("ts_o1", "hasso_time"),            # 公式時系列(0B41)。発表時刻の格子
    "sokuho": ("ts_sokuho_o1", "observed_at"),  # 自前10秒ポーリング(0B30)
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
  SELECT to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
snap AS (
  SELECT t.umaban, t.tan_odds, t.fuku_odds_low, to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI') AS ts
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
  SELECT to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
snap AS (
  SELECT t.umaban, t.tan_odds, t.fuku_odds_low, t.observed_at AS ts
  FROM ts_sokuho_o1 t
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

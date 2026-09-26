"""flow が選んだ馬から組を作って、ワイド・馬連・三連複を評価する。

★なぜ試す価値があるか
  単勝×人気7+ が OOS 1.6690 だったのは、エッジが**中穴の「勝ち切る力」**にあるから。
  組み合わせ券はその馬が絡む配当がさらに大きい。ただし2頭・3頭が同時に要るので
  的中率は激減する。**本数と分散を必ず一緒に見ること**。

★組の作り方
  同一レースで閾値を超えた馬(flow 上位)から、スコアの高い順に組を作る。
  1レースで N 頭が候補なら C(N,2) 組できるが、点数が増えると1点あたりの期待値が
  薄まるので `--max-combos` で上位から打ち切る。

★決済
  nl_hr の確定払戻。組番は馬番2桁の連結(昇順)で、ワイドは1レースに3組当たる。
  ★当たり組が複数あるワイドで「1つでも一致したら当たり」を組ごとに判定すること。
    レース単位で判定すると、買っていない組で当たり扱いになる。
"""

from __future__ import annotations

from itertools import combinations

from .flow_signal import summarize_bets

# 券種 → (nl_hr の bet_type, 1組の頭数)
COMBO_TYPES = {
    "wide": ("wide", 2),
    "umaren": ("umaren", 2),
    "sanrenfuku": ("sanrenfuku", 3),
}

_SQL_PAY = """
SELECT h.year||h.month_day||h.jyo_cd||h.kaiji||h.nichiji||h.race_num AS rid,
       regexp_replace(h.kumi,'[^0-9]','','g') AS kumi, h.pay
FROM nl_hr h
WHERE h.year||h.month_day BETWEEN %(d0)s AND %(d1)s
  AND h.bet_type = %(bt)s
"""


def _norm(kumi: str, width: int, n: int) -> str | None:
    """'0102' → '0102'。桁が揃っていなければ None。"""
    s = (kumi or "").strip()
    if len(s) != width * n or not s.isdigit():
        return None
    return s


def evaluate(db, rows: list[dict], d_from: str, d_to: str, bet: str, *,
             threshold: float, min_ninki: int = 0, max_ninki: int = 0,
             max_combos: int = 3, amount: int = 100) -> dict:
    """flow の候補から組を作って買った場合の回収率。"""
    if bet not in COMBO_TYPES:
        raise ValueError(f"不明な券種: {bet} ({'|'.join(COMBO_TYPES)})")
    hr_type, per = COMBO_TYPES[bet]

    pay: dict[str, dict[str, str]] = {}
    for r in db.query(_SQL_PAY, {"d0": d_from, "d1": d_to, "bt": hr_type}):
        k = _norm(r["kumi"], 2, per)
        if k:
            pay.setdefault(r["rid"], {})[k] = r["pay"]

    by_race: dict[str, list[dict]] = {}
    for r in rows:
        if min_ninki and r["ninki"] < min_ninki:
            continue
        if max_ninki and r["ninki"] > max_ninki:
            continue
        if r["flow"] < threshold:
            continue
        by_race.setdefault(r["rid"], []).append(r)

    bets: list[tuple] = []
    n_races = n_used = 0
    for rid, picks in by_race.items():
        n_races += 1
        if len(picks) < per:
            continue
        if rid not in pay:                 # ★払戻が無い= 未確定。外れとして数えない
            continue
        n_used += 1
        picks.sort(key=lambda r: -r["flow"])
        for combo in list(combinations(picks, per))[:max_combos]:
            kumi = "".join(sorted(x["umaban"] for x in combo))
            p = pay[rid].get(kumi)
            payout = int(round(int(p) * amount / 100)) if str(p or "").isdigit() else 0
            bets.append((rid, amount, payout, False))

    rep = summarize_bets(bets, races=n_races, races_scored=n_used)
    rep["races_seen"] = n_races
    rep["races_with_combo"] = n_used
    rep["bet"] = bet
    return rep

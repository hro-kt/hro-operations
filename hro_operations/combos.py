"""flow が選んだ馬から組を作って、ワイド・馬連・三連複を評価する。

★なぜ試す価値があるか
  単勝×人気7+ が OOS 1.6690 だったのは、エッジが**中穴の「勝ち切る力」**にあるから。
  組み合わせ券はその馬が絡む配当がさらに大きい。ただし2頭・3頭が同時に要るので
  的中率は激減する。**本数と分散を必ず一緒に見ること**。

★組の作り方は2通り。**partners の方が本質的**。
  - all : 候補どうしで組む。**両方が候補である**ことを要求するので、組が作れるのは
          全帯で1,794中531レース(30%)、人気7+では989中255(26%)。分母が構造的に小さい。
  - partners : **軸＝flow が拾った馬 / 相手＝人気上位N頭**。エッジは1頭ごとに存在するので
          相手まで候補である必要はない。候補が1頭いれば組めるので対象レースが激減しない。
          軸がエッジを提供し、相手が的中確率を支える(実際の買い方と同じ形)。
  点数が増えると1点あたりの期待値が薄まるので `--max-combos` で上位から打ち切る。

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
             max_combos: int = 3, amount: int = 100,
             mode: str = "partners", partners: int = 3) -> dict:
    """flow の候補から組を作って買った場合の回収率。"""
    if bet not in COMBO_TYPES:
        raise ValueError(f"不明な券種: {bet} ({'|'.join(COMBO_TYPES)})")
    hr_type, per = COMBO_TYPES[bet]

    pay: dict[str, dict[str, str]] = {}
    for r in db.query(_SQL_PAY, {"d0": d_from, "d1": d_to, "bt": hr_type}):
        k = _norm(r["kumi"], 2, per)
        if k:
            pay.setdefault(r["rid"], {})[k] = r["pay"]

    all_by_race: dict[str, list[dict]] = {}
    for r in rows:
        all_by_race.setdefault(r["rid"], []).append(r)

    def _picks(rs):
        out = [r for r in rs if r["flow"] >= threshold
               and not (min_ninki and r["ninki"] < min_ninki)
               and not (max_ninki and r["ninki"] > max_ninki)]
        out.sort(key=lambda r: -r["flow"])
        return out

    bets: list[tuple] = []
    n_races = n_used = 0
    for rid, rs in all_by_race.items():
        picks = _picks(rs)
        if not picks:
            continue
        n_races += 1
        combos: list[tuple] = []
        if mode == "all":
            if len(picks) >= per:
                combos = list(combinations(picks, per))
        else:
            # ★軸=候補 / 相手=人気上位。相手から軸自身は除く。
            others = sorted((r for r in rs if r["ninki"] <= partners),
                            key=lambda r: r["ninki"])
            for axis in picks:
                pool = [o for o in others if o["umaban"] != axis["umaban"]]
                if len(pool) < per - 1:
                    continue
                combos += [(axis, *c) for c in combinations(pool, per - 1)]
        if not combos:
            continue
        if rid not in pay:                 # ★払戻が無い= 未確定。外れとして数えない
            continue
        n_used += 1
        seen: set[str] = set()
        for combo in combos:
            kumi = "".join(sorted(x["umaban"] for x in combo))
            if kumi in seen:               # ★軸が2頭いると同じ組が重複しうる
                continue
            seen.add(kumi)
            if len(seen) > max_combos:
                break
            p = pay[rid].get(kumi)
            payout = int(round(int(p) * amount / 100)) if str(p or "").isdigit() else 0
            bets.append((rid, amount, payout, False))

    rep = summarize_bets(bets, races=n_races, races_scored=n_used)
    rep["races_seen"] = n_races
    rep["races_with_combo"] = n_used
    rep["bet"] = bet
    rep["mode"] = mode
    return rep

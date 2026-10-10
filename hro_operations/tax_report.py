"""年次の税務集計。**live のみ・暦年単位**。

★金額の真実源は IPAT 側(ipat_receipts)。bet_orders は「出した指示」であって
  成立額ではない(2026-10-10 は指示 29件のうち 6件が bet_unit 違反で不成立だった)。
★一時所得か雑所得かの判定はしない。両方の計算を並べて出し、判断は税理士に委ねる。
  こちらが決めてしまうと、都合のよい方だけを計算した資料に見える。
★手動購入は**分離して明示**する。隠すと、機械的・網羅的という主張全体が疑われる。
"""

from __future__ import annotations

# 一時所得の特別控除(所得税法34条3項)
ICHIJI_DEDUCTION = 500_000

_SQL_MONTHLY = """
SELECT left(r.budget_key, 6) AS ym,
       count(DISTINCT r.budget_key)::int                AS days,
       count(*)::int                                    AS receipts,
       sum(coalesce(r.bought, 0))::bigint               AS bought,
       sum(coalesce(r.payout, 0))::bigint               AS payout
FROM ipat_receipts r
WHERE left(r.budget_key, 4) = %s
GROUP BY 1 ORDER BY 1
"""

_SQL_COVERAGE = """
SELECT left(budget_key, 6) AS ym, status, count(*)::int, sum(n_bought)::int
FROM race_coverage WHERE left(budget_key, 4) = %s
GROUP BY 1, 2 ORDER BY 1, 2
"""

# ★自動購入と手動購入の切り分け。v_vote_map の kind='manual' は
#   「投票指示が無いのに投票された」= 人が手で入れたもの。
_SQL_MANUAL = """
SELECT budget_key, race_id, bet_type, selection_id, voted_amount, receipt
FROM v_vote_map
WHERE left(budget_key, 4) = %s AND kind = 'manual'
ORDER BY budget_key, race_id
"""

_SQL_VERSIONS = """
SELECT id, version, mode, effective_from, effective_to, params_hash
FROM strategy_versions
WHERE mode = 'live' AND effective_from <= %s AND coalesce(effective_to, '99999999') >= %s
ORDER BY version
"""

# ★一時所得の経費は「**的中した馬券の購入額だけ**」。受付単位(ipat_receipts)では
#   1受付に当たりとハズレが混ざるので出せない。払戻と突合済みの行から取る。
#   = 決済(settle)が済んでいる必要がある(払戻は開催の3〜5日後に配信)。
_SQL_HIT_COST = """
SELECT count(*) FILTER (WHERE hit)::int        AS n_hit,
       count(*)::int                           AS n_settled,
       sum(amount) FILTER (WHERE hit)::bigint  AS hit_cost,
       sum(amount)::bigint                     AS settled_cost,
       sum(payout)::bigint                     AS settled_payout
FROM bet_settlements
WHERE mode = 'live' AND settled_at IS NOT NULL AND left(budget_key, 4) = %s
"""

_SQL_SEALS = """
SELECT count(*)::int, count(DISTINCT budget_key)::int, max(sealed_at)
FROM tax_ledger_seals WHERE left(budget_key, 4) = %s
"""


def _rows(conn, sql, params):
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:   # noqa: BLE001 - 未適用のスキーマがあっても他は出す
        return []


def ichiji_shotoku(bought_hit: int, payout: int) -> int:
    """一時所得(ハズレ馬券は経費にならない)。

    ★**的中した馬券の購入額だけ**が経費。課税対象は (払戻 − 的中分の購入額 − 50万) の 1/2。
    """
    base = max(0, payout - bought_hit - ICHIJI_DEDUCTION)
    return base // 2


def zatsu_shotoku(bought_all: int, payout: int) -> int:
    """雑所得(馬券購入費の全額が経費)。特別控除は無い。"""
    return payout - bought_all


def build(conn, year: str) -> dict:
    """年次の集計を組む。表示はしない(CLI と admin が使う)。"""
    monthly = [{"ym": r[0], "days": r[1], "receipts": r[2],
                "bought": int(r[3] or 0), "payout": int(r[4] or 0)}
               for r in _rows(conn, _SQL_MONTHLY, (year,))]
    bought = sum(m["bought"] for m in monthly)
    payout = sum(m["payout"] for m in monthly)

    cov: dict[str, dict] = {}
    for ym, status, n, tickets in _rows(conn, _SQL_COVERAGE, (year,)):
        e = cov.setdefault(ym, {"races": 0, "evaluated": 0, "bought": 0, "tickets": 0,
                                "by_status": {}})
        e["races"] += n
        e["by_status"][status] = n
        e["tickets"] += int(tickets or 0)
        if status in ("bought", "no_candidate", "race_skipped"):
            e["evaluated"] += n
        if status == "bought":
            e["bought"] += n
    races = sum(c["races"] for c in cov.values())
    evaluated = sum(c["evaluated"] for c in cov.values())
    bought_races = sum(c["bought"] for c in cov.values())

    manual = [{"budget_key": r[0], "race_id": r[1], "bet_type": r[2],
               "selection_id": r[3], "amount": int(r[4] or 0), "receipt": r[5]}
              for r in _rows(conn, _SQL_MANUAL, (year,))]
    versions = [{"id": r[0], "version": r[1], "mode": r[2], "effective_from": r[3],
                 "effective_to": r[4], "params_hash": r[5]}
                for r in _rows(conn, _SQL_VERSIONS, (f"{year}1231", f"{year}0101"))]
    hc = _rows(conn, _SQL_HIT_COST, (year,))
    h = hc[0] if hc else (0, 0, 0, 0, 0)
    settled = {"n_hit": int(h[0] or 0), "n_settled": int(h[1] or 0),
               "hit_cost": int(h[2] or 0), "cost": int(h[3] or 0),
               "payout": int(h[4] or 0)}
    # ★決済が追いついていないと一時所得の計算ができない。できないと言う。
    settled["complete"] = bool(settled["n_settled"])

    seal = _rows(conn, _SQL_SEALS, (year,))
    seals = ({"links": seal[0][0], "days": seal[0][1], "last": str(seal[0][2])}
             if seal else {"links": 0, "days": 0, "last": None})

    return {
        "year": year,
        "totals": {
            "bought": bought, "payout": payout, "pnl": payout - bought,
            "roi": (payout / bought) if bought else None,
            "days": len({m["ym"] for m in monthly}) and sum(m["days"] for m in monthly),
            "receipts": sum(m["receipts"] for m in monthly),
        },
        "monthly": monthly,
        "months_active": len([m for m in monthly if m["bought"] > 0]),
        "coverage": {
            "races": races, "evaluated": evaluated,
            "evaluated_ratio": (evaluated / races) if races else None,
            "bought_races": bought_races,
            "bought_ratio": (bought_races / races) if races else None,
            "by_month": cov,
        },
        "manual": {"n": len(manual), "amount": sum(m["amount"] for m in manual),
                   "rows": manual},
        "strategy_versions": versions,
        "seals": seals,
        "settled": settled,
        # 両方の計算を並べる。どちらを採るかは税理士の判断
        "tax": {
            "ichiji": (ichiji_shotoku(settled["hit_cost"], payout)
                       if settled["complete"] else None),
            "ichiji_deduction": ICHIJI_DEDUCTION,
            "zatsu": zatsu_shotoku(bought, payout),
        },
        # ★的中分の購入額は払戻のある受付からしか出せない。受付単位で券種が混ざる
        #   場合があるので按分はしない(v_ipat_pnl の mixed と同じ方針)。
        "note": "一時所得の計算に要る『的中した馬券の購入額』は受付単位でしか出せない",
    }

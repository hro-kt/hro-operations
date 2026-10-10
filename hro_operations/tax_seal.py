"""日次の封印。過去の記録を書き換えたら分かるようにする。

★税務の証憑でいちばん弱いのは「後から作れる」ことで、その日のうちに記録した
  という裏づけが要る。各日の内容をハッシュし、**直前の封印のハッシュを含める**。
  鎖になっているので、過去の1日を書き換えるとその日以降が全部合わなくなる。
★封印は追記のみ。締めをやり直して払戻が増えたら、上書きせず**新しい環を足す**
  (revision が増える)。「いつ何が分かって、いつ訂正したか」がそのまま残る。
★金額の真実源は IPAT 側(ipat_receipts)。我々の bet_orders ではない。
"""

from __future__ import annotations

import hashlib
import json

# 鎖の起点。最初の封印はこれを prev_hash に持つ(NULL と区別できるようにしない)
GENESIS = None


def content_hash(content: dict, prev_hash: str | None) -> str:
    """封印の内容 + 直前のハッシュ から SHA-256。

    ★prev_hash を**ハッシュの入力に含める**。content だけを固めて prev を隣に
      置くだけでは鎖にならない(過去を差し替えても個々のハッシュは合ってしまう)。
    """
    blob = json.dumps({"prev": prev_hash, "content": content},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_SQL_ORDERS = """
SELECT o.race_id, o.bet_type, o.selection_id, sum(o.amount)::bigint, count(*)::bigint,
       min(o.strategy_version_id)
FROM bet_orders o WHERE o.budget_key = %s
GROUP BY 1,2,3 ORDER BY 1,2,3
"""

_SQL_VOTES = """
SELECT receipt, race_id, bet_type, selection_id,
       sum(amount * n_combos)::bigint
FROM ipat_vote_lines WHERE budget_key = %s
GROUP BY 1,2,3,4 ORDER BY 1,2,3,4
"""

_SQL_RECEIPTS = """
SELECT receipt, coalesce(bought,0)::bigint, coalesce(payout,0)::bigint
FROM ipat_receipts WHERE budget_key = %s ORDER BY receipt
"""

_SQL_VERSIONS = """
SELECT DISTINCT v.id, v.version, v.params_hash, v.mode
FROM bet_orders o JOIN strategy_versions v ON v.id = o.strategy_version_id
WHERE o.budget_key = %s ORDER BY v.id
"""


def build_content(conn, budget_key: str) -> dict:
    """その日の「封印する内容」を組む。

    ★入れるのは**金額と件数と依拠した戦略の版**。判断の中身(decision logs)まで
      含めると封印が巨大になり、しかも訂正のたびに全部が変わって差分が読めない。
      根拠は別表に残っているので、ここでは件数で足跡だけ押さえる。
    """
    orders = [list(r) for r in conn.execute(_SQL_ORDERS, (budget_key,)).fetchall()]
    votes = [list(r) for r in conn.execute(_SQL_VOTES, (budget_key,)).fetchall()]
    receipts = [list(r) for r in conn.execute(_SQL_RECEIPTS, (budget_key,)).fetchall()]
    versions = [list(r) for r in conn.execute(_SQL_VERSIONS, (budget_key,)).fetchall()]
    n_dlogs = conn.execute(
        "SELECT count(*) FROM bet_decision_logs WHERE budget_key=%s",
        (budget_key,)).fetchone()[0]
    bought = sum(r[1] for r in receipts)
    payout = sum(r[2] for r in receipts)
    return {
        "v": 1,
        "budget_key": budget_key,
        "strategy_versions": versions,
        "orders": orders,
        "ipat_votes": votes,
        "ipat_receipts": receipts,
        # ★金額の真実源は IPAT 側。bet_orders は「出した指示」であって成立額ではない
        "totals": {"bought": int(bought), "payout": int(payout),
                   "n_receipts": len(receipts), "n_orders": len(orders),
                   "n_decision_logs": int(n_dlogs)},
    }


def last_seal(conn) -> tuple[int, str | None]:
    """直前の封印の (seq, content_hash)。無ければ (0, GENESIS)。"""
    row = conn.execute(
        "SELECT seq, content_hash FROM tax_ledger_seals ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    return (int(row[0]), row[1]) if row else (0, GENESIS)


def seal_day(conn, budget_key: str, *, force: bool = False) -> dict:
    """その日を封印する。内容が前回と同じなら何もしない(冪等)。

    戻り: {"status": "sealed"|"unchanged"|"empty", "revision":..., "hash":...}
    """
    content = build_content(conn, budget_key)
    if not force and not content["totals"]["n_orders"] \
            and not content["totals"]["n_receipts"]:
        return {"status": "empty", "budget_key": budget_key}

    prev_rev, prev_content = 0, None
    row = conn.execute(
        "SELECT revision, content FROM tax_ledger_seals WHERE budget_key=%s "
        "ORDER BY revision DESC LIMIT 1", (budget_key,)).fetchone()
    if row:
        prev_rev, prev_content = int(row[0]), row[1]
    if prev_content == content and not force:
        return {"status": "unchanged", "budget_key": budget_key, "revision": prev_rev}

    _, prev_hash = last_seal(conn)
    h = content_hash(content, prev_hash)
    conn.execute(
        "INSERT INTO tax_ledger_seals(budget_key, revision, content, content_hash, prev_hash)"
        " VALUES(%s,%s,%s::jsonb,%s,%s)",
        (budget_key, prev_rev + 1, json.dumps(content, ensure_ascii=False, sort_keys=True),
         h, prev_hash))
    return {"status": "sealed", "budget_key": budget_key, "revision": prev_rev + 1,
            "hash": h, "prev_hash": prev_hash,
            "bought": content["totals"]["bought"],
            "payout": content["totals"]["payout"]}


def verify_chain_rows(rows) -> list[dict]:
    """封印の列(seq 昇順)を頭から検証する。壊れている環だけを返す(空なら健全)。

    rows は (seq, budget_key, revision, content, content_hash, prev_hash, sealed_at)
    の並び、または同名のキーを持つ dict。**DB に触らない純粋関数**にしてあるのは、
    admin 側が同じ判定を自前の接続で行えるようにするため。
    """
    def g(r, i, k):
        return r[k] if isinstance(r, dict) else r[i]

    bad: list[dict] = []
    expect_prev = GENESIS
    for r in rows:
        seq, bk, rev = g(r, 0, "seq"), g(r, 1, "budget_key"), g(r, 2, "revision")
        content, h = g(r, 3, "content"), g(r, 4, "content_hash")
        prev, at = g(r, 5, "prev_hash"), g(r, 6, "sealed_at")
        why = []
        if prev != expect_prev:
            why.append(f"prev_hash が鎖と不一致(期待 {expect_prev} / 記録 {prev})")
        if content_hash(content, prev) != h:
            why.append("内容のハッシュが記録と不一致(封印後に書き換えられている)")
        if why:
            bad.append({"seq": seq, "budget_key": bk, "revision": rev,
                        "sealed_at": str(at), "problems": why})
        expect_prev = h
    return bad


def verify_chain(conn) -> list[dict]:
    """DB から読んで鎖を検証する。"""
    return verify_chain_rows(conn.execute(
        "SELECT seq, budget_key, revision, content, content_hash, prev_hash, sealed_at"
        " FROM tax_ledger_seals ORDER BY seq").fetchall())

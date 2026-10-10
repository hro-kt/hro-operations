"""机上検証(バックテスト)の結果を、実行したその場で記録する。

★⑧収益性の検証を自由記述にしない。バックテストは実際に走らせたものなので、
  結果そのものを残す方が強い。人が転記すると数字が合っている保証が無く、
  都合のよい結果だけ書いた疑いも晴れない。
★**いつ検証したか**が効く。戦略を適用した日より前に検証していた、という順序が
  「事前に期待回収率を見積もって購入していた」の裏づけになる。
★実行したコマンドをそのまま残す。再現できない検証は検証ではない。
"""

from __future__ import annotations

import json
import sys

STRATEGY_ID = "flow_tan"


def command_line() -> str:
    """いま走っているコマンドを復元する(再現用)。

    ★argv をそのまま残す。引数を組み立て直すと、実際に打った内容とずれる。
    """
    import shlex
    return "hro-ops " + " ".join(shlex.quote(a) for a in sys.argv[1:])


def extract(rep: dict) -> dict:
    """summarize_bets の結果から、一覧と文書で使う代表値を取り出す。"""
    ci = rep.get("ci") or {}
    return {
        "n_bets": int(rep.get("bets") or 0),
        "roi": (float(rep["roi"]) if rep.get("roi") is not None else None),
        "hit_rate": (float(rep["hit_rate"]) if rep.get("hit_rate") is not None else None),
        "p_le_1": (float(ci["p_le_1"]) if ci.get("p_le_1") is not None else None),
    }


def record(conn, *, label: str, kind: str, bet_type: str | None,
           period: tuple[str, str], params: dict, result: dict,
           command: str | None = None, note: str | None = None) -> dict:
    """1回の検証を記録する。追記のみ。

    ★同じ条件を何度回しても、そのたびに行が増える。「何回も回して良い結果だけ
      採った」かどうかは、行が全部残っていて初めて判断できる。上書きしない。
    """
    rep = {k: v for k, v in (result or {}).items() if k not in ("details", "_bets")}
    vals = extract(rep)
    row = conn.execute(
        "INSERT INTO strategy_backtests(strategy_id, label, kind, bet_type,"
        " period_from, period_to, params, result, n_bets, roi, hit_rate, p_le_1,"
        " command, note)"
        " VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s)"
        " RETURNING id",
        (STRATEGY_ID, label, kind, bet_type, period[0], period[1],
         json.dumps(params, ensure_ascii=False, sort_keys=True, default=str),
         json.dumps(rep, ensure_ascii=False, sort_keys=True, default=str),
         vals["n_bets"], vals["roi"], vals["hit_rate"], vals["p_le_1"],
         command or command_line(), note)).fetchone()
    return {"id": int(row[0]), **vals}


def fetch(conn, *, include_superseded: bool = False) -> list[dict]:
    """記録した検証を新しい順に。"""
    sql = ("SELECT id, label, kind, bet_type, period_from, period_to, n_bets, roi,"
           " hit_rate, p_le_1, command, ran_at, note, superseded_at, params"
           " FROM strategy_backtests WHERE strategy_id=%s"
           + ("" if include_superseded else " AND superseded_at IS NULL")
           + " ORDER BY ran_at DESC")
    keys = ("id", "label", "kind", "bet_type", "period_from", "period_to", "n_bets",
            "roi", "hit_rate", "p_le_1", "command", "ran_at", "note",
            "superseded_at", "params")
    return [dict(zip(keys, r)) for r in conn.execute(sql, (STRATEGY_ID,)).fetchall()]


def supersede(conn, backtest_id: int, note: str | None = None) -> int:
    """検証を取り下げる。**消さない**。

    ★前提が間違っていたと分かった検証(混入したデータで回した等)も、消すと
      「都合の悪い結果を消した」と区別がつかない。印を付けて残す。
    """
    rows = conn.execute(
        "UPDATE strategy_backtests SET superseded_at=now(),"
        " note=coalesce(%s, note) WHERE id=%s AND superseded_at IS NULL RETURNING id",
        (note, backtest_id)).fetchall()
    return len(rows)

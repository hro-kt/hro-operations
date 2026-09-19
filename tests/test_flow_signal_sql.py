"""flow_signal の SQL を **本物の PostgreSQL パーサ** で構文検査する。

本番で `syntax error at or near "UNION"` が出た(PostgreSQL は UNION の前に ORDER BY を
書けない)。DB に繋がない環境でも同じ失敗を検出できるよう、pglast があれば構文を検査する。
pglast が無い環境ではスキップする(必須依存にはしない)。
"""

from __future__ import annotations

import re

import pytest

from hro_operations.flow_signal import _SQL_SOKUHO, _SQL_TS

pglast = pytest.importorskip("pglast", reason="pglast 未導入のため SQL 構文検査はスキップ")


@pytest.mark.parametrize("name,sql", [("ts", _SQL_TS), ("sokuho", _SQL_SOKUHO)])
def test_sql_parses(name, sql):
    pglast.parse_sql(re.sub(r"%\(\w+\)s", "NULL", sql))


@pytest.mark.parametrize("sql", [_SQL_TS, _SQL_SOKUHO])
def test_no_order_by_directly_before_union(sql):
    """回帰: ORDER BY を UNION の直前に置かない(枝は CTE に切り出す)。"""
    flat = " ".join(sql.split())
    assert not re.search(r"ORDER BY[^()]*?UNION", flat)

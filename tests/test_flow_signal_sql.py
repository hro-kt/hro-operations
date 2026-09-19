"""flow_signal の SQL を **本物の PostgreSQL パーサ** で構文検査する。

本番で `syntax error at or near "UNION"` が出た(PostgreSQL は UNION の前に ORDER BY を
書けない)。DB に繋がない環境でも同じ失敗を検出できるよう、pglast があれば構文を検査する。
pglast が無い環境ではスキップする(必須依存にはしない)。
"""

from __future__ import annotations

import re

import pytest

from hro_operations.flow_signal import _SQL_COVERAGE, _SQL_SOKUHO, _SQL_TS

pglast = pytest.importorskip("pglast", reason="pglast 未導入のため SQL 構文検査はスキップ")


@pytest.mark.parametrize("name,sql", [("ts", _SQL_TS), ("sokuho", _SQL_SOKUHO)])
def test_sql_parses(name, sql):
    pglast.parse_sql(re.sub(r"%\(\w+\)s", "NULL", sql))


@pytest.mark.parametrize("sql", [_SQL_TS, _SQL_SOKUHO])
def test_no_order_by_directly_before_union(sql):
    """回帰: ORDER BY を UNION の直前に置かない(枝は CTE に切り出す)。"""
    flat = " ".join(sql.split())
    assert not re.search(r"ORDER BY[^()]*?UNION", flat)


@pytest.mark.parametrize("name,sql", [("ts", _SQL_TS), ("sokuho", _SQL_SOKUHO),
                                     ("coverage", _SQL_COVERAGE)])
def test_jra_times_are_pinned_to_jst(name, sql):
    """nl_ra/ts_o1 の時刻は JST。to_timestamp はセッションTZ依存なので固定すること。

    セッションが UTC のまま observed_at(実時刻)と比べると 9 時間ずれ、
    「取得の余裕 32,325秒」のような値になる(実際に出た)。
    """
    assert "AT TIME ZONE 'Asia/Tokyo'" in sql
    # 素の to_timestamp(TZ 固定なし)が残っていないこと
    import re as _re
    bare = _re.findall(r"to_timestamp\([^)]*\)(?!\s*::timestamp)", sql)
    assert not bare, bare

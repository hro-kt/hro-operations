"""同じ常駐を二重に起動しない。

★2026-10-17: オーケストレータ(orchestrator#210)と admin-ui の両方が
  netkeiba_odds / flow_day を投入し、flow ランナーが2本同時に走った
  (job 220 15:40:13 / job 221 15:40:23)。
  agent は並行数を持つので、別の行として投入された同じ常駐は普通に同時実行される。

  重複購入の防止はプロセス内の _seen と**起動時の** preload だけ。
  同時に走る2本は互いの購入を見られないので、**同じ買い目を二重に買い得る**。
  実弾なので、ここは黙って通してはいけない。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from hro_operations.agent import _SQL_REJECT_DUP, reject_duplicate_residents  # noqa: E402

_SRC = (_ROOT / "hro_operations" / "agent.py").read_text(encoding="utf-8")


class _Conn:
    def __init__(self, rows):
        self.rows, self.sql, self.params = rows, None, None
        self.commits = 0

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def commit(self):
        self.commits += 1


def test_duplicates_are_cancelled_not_started():
    conn = _Conn([(221, "flow_day")])
    got = reject_duplicate_residents(conn, "windows")
    assert got == [(221, "flow_day")]
    assert conn.params == ("windows",)
    assert conn.commits == 1


def test_nothing_happens_when_there_is_no_duplicate():
    assert reject_duplicate_residents(_Conn([]), "windows") == []


def test_only_resident_jobs_are_rejected():
    """★単発(同期・締め)は同時に走っても構わない。常駐だけを対象にする。"""
    assert "(q.args->>'resident') = 'true'" in _SQL_REJECT_DUP


def test_the_match_is_on_kind_and_date():
    """★別の日のジョブを巻き込まない。"""
    assert "r.kind = q.kind" in _SQL_REJECT_DUP
    assert "coalesce(r.args->>'date','') = coalesce(q.args->>'date','')" in _SQL_REJECT_DUP


def test_it_does_not_cancel_itself():
    assert "r.id <> q.id" in _SQL_REJECT_DUP


def test_only_running_jobs_block():
    """queued 同士でぶつけない(どちらも起動していないなら片方は走ってよい)。"""
    assert "r.status = 'running'" in _SQL_REJECT_DUP
    assert "q.status = 'queued'" in _SQL_REJECT_DUP


def test_the_reason_is_written_into_the_job_log():
    """★UI で『なぜ起動しなかったか』が分かること。"""
    assert "同じ常駐" in _SQL_REJECT_DUP and "二重購入" in _SQL_REJECT_DUP


def test_claim_rejects_duplicates_before_taking_a_job():
    """★SKIP LOCKED が防ぐのは『同じ行を2人が取る』ことだけ。

    別の行として投入された同じ常駐は防げないので、取りに行く前に畳む。
    """
    m = re.search(r"def _claim\(conn, server: str\):(.*?)\n    return row", _SRC, re.S)
    assert m, "_claim が見つからない"
    body = m.group(1)
    assert "reject_duplicate_residents(conn, server)" in body
    assert body.index("reject_duplicate_residents") < body.index("UPDATE ops_job SET status='running'")


# --------------------------------------------------------------------------- #
# ★手で起動した常駐を引き継ぐ(date が空でも)
# --------------------------------------------------------------------------- #
def test_adopt_also_matches_jobs_without_a_date():
    """★常駐は date 省略なら当日を見る。手動起動(admin UI)は args に date が
    入らないことがある。完全一致だけを見ていたため 2026-10-17 に
    flow ランナーが2本走った(job 220 date=20261017 / job 221 date 無し)。
    """
    import re as _re

    src = (_ROOT / "hro_operations" / "orchestrator.py").read_text(encoding="utf-8")
    m = _re.search(r"def _adopt\(.*?\n(?=\n\ndef )", src, _re.S)
    assert m, "_adopt が見つからない"
    body = m.group(0)
    assert "IN (%s, '')" in body, "date が空のジョブを引き継げない"
    assert "status IN ('queued','running')" in body
    assert "NOT cancel_requested" in body


def test_adopt_is_used_before_enqueue():
    """★先に引き継ぎを試すこと。投入してから気づいても遅い。"""
    import re as _re

    src = (_ROOT / "hro_operations" / "orchestrator.py").read_text(encoding="utf-8")
    m = _re.search(r"def _start_all\(.*?\n(?=\n\ndef )", src, _re.S)
    assert m
    body = m.group(0)
    assert body.index("_adopt(") < body.index("_enqueue(")

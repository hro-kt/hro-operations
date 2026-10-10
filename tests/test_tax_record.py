"""税務のための記録: 戦略の版と、日次封印のハッシュ鎖。

★後から作れない形であることが本題。版は**稼働した設定そのもの**から起こし、
  封印は前の封印を含めてハッシュするので、過去を書き換えると鎖が切れる。
"""

from __future__ import annotations

import json

import pytest

from hro_operations.race_day import DayConfig
from hro_operations.strategy import (
    canonical_params,
    describe,
    params_hash,
    resolve_version,
)
from hro_operations.tax_seal import content_hash, seal_day, verify_chain


def _cfg(**kw):
    base = dict(date="20261011", win_model="", place_model="", results_path="",
                strategy="flow", flow_source="netkeiba", flow_lead_seconds=90,
                flow_minutes=6, flow_thresholds={90: 0.1368},
                flow_bet_type="tan:1000,umatan:100", flow_min_ninki=7,
                flow_max_ninki=25, flat_amount=1000, mode="live",
                deadline_lead_seconds=60, lead_seconds=70,
                max_amount_per_order=5000, max_amount_per_day=20000)
    base.update(kw)
    return DayConfig(**base)


# --- 戦略の版 -----------------------------------------------------------

def test_params_come_from_the_config_that_actually_runs():
    """★版は稼働した設定そのものから起こす。手で書いた宣言は後から書ける。"""
    p = canonical_params(_cfg())
    assert p["flow"]["source"] == "netkeiba" and p["flow"]["lead_seconds"] == 90
    assert p["flow"]["thresholds"] == {"90": 0.1368}
    assert p["flow"]["min_ninki"] == 7 and p["flow"]["max_ninki"] == 25
    assert p["bets"] == [{"bet_type": "tan", "amount": 1000},
                         {"bet_type": "umatan", "amount": 100}]
    assert p["mode"] == "live"


def test_new_flow_config_fields_are_captured_automatically():
    """★除外リスト方式。FlowConfig に足した設定は黙って版へ入る。

    項目を書き並べる方式だと、足した設定が版に入らず**別の戦略が同じ版に見える**。
    """
    import dataclasses

    from hro_operations.flow_signal import FlowConfig
    from hro_operations.strategy import _NOT_STRATEGY
    names = {f.name for f in dataclasses.fields(FlowConfig)} - set(_NOT_STRATEGY)
    assert names <= set(canonical_params(_cfg())["flow"])


@pytest.mark.parametrize("change", [
    {"flow_min_ninki": 4}, {"flow_lead_seconds": 120}, {"flow_source": "sokuho"},
    {"flow_bet_type": "tan:2000,umatan:100"}, {"mode": "paper"},
    {"flow_thresholds": {90: 0.2}}, {"max_amount_per_order": 3000},
    {"lead_seconds": 80},
])
def test_any_change_that_moves_the_bets_makes_a_new_hash(change):
    """★買い目や買える上限に効くものを落とすと、別の戦略が同じ版に見える。"""
    assert params_hash(canonical_params(_cfg())) != \
        params_hash(canonical_params(_cfg(**change)))


def test_same_settings_give_the_same_hash():
    assert params_hash(canonical_params(_cfg())) == \
        params_hash(canonical_params(_cfg()))


def test_hash_is_stable_against_key_order():
    p = canonical_params(_cfg())
    shuffled = json.loads(json.dumps(dict(reversed(list(p.items())))))
    assert params_hash(p) == params_hash(shuffled)


def test_describe_names_the_settings_that_matter():
    t = describe(canonical_params(_cfg()))
    assert "netkeiba" in t and "T-90s" in t and "min_ninki=7" in t
    assert "tan:1000,umatan:100" in t and "mode=live" in t


# --- 版の採番(偽の接続で SQL の筋だけ見る) ------------------------------

class _VerConn:
    def __init__(self, existing=None):
        self.existing = existing      # params_hash -> id
        self.sql: list = []

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))
        if sql.strip().startswith("SELECT id FROM strategy_versions"):
            self._r = [(self.existing,)] if self.existing else []
        elif "INSERT INTO strategy_versions" in sql:
            self._r = [(7,)]
        else:
            self._r = []
        return self

    def fetchone(self):
        return self._r[0] if self._r else None


def test_existing_version_is_reused():
    conn = _VerConn(existing=3)
    assert resolve_version(conn, canonical_params(_cfg()), date="20261011") == 3
    assert not any("INSERT" in q for q, _ in conn.sql)


def test_new_version_closes_the_previous_one():
    """★期間が重ならないこと。いつからいつまでどの戦略かが一意に読める。"""
    conn = _VerConn(existing=None)
    assert resolve_version(conn, canonical_params(_cfg()), date="20261011") == 7
    upd = [q for q, _ in conn.sql if q.startswith("UPDATE strategy_versions")]
    assert upd and "effective_to" in upd[0] and "effective_to IS NULL" in upd[0]


# --- 封印のハッシュ鎖 ---------------------------------------------------

class _SealConn:
    """tax_ledger_seals と、封印が読む集計だけを持つ最小の偽接続。"""

    def __init__(self, content):
        self.content = content
        self.rows: list = []      # (seq, budget_key, revision, content, hash, prev, at)
        self._r: list = []

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        if q.startswith("SELECT seq, content_hash FROM tax_ledger_seals ORDER BY seq DESC"):
            self._r = [(self.rows[-1][0], self.rows[-1][4])] if self.rows else []
        elif q.startswith("SELECT revision, content FROM tax_ledger_seals WHERE budget_key"):
            same = [r for r in self.rows if r[1] == params[0]]
            self._r = [(same[-1][2], same[-1][3])] if same else []
        elif q.startswith("SELECT seq, budget_key, revision, content"):
            self._r = list(self.rows)
        elif q.startswith("INSERT INTO tax_ledger_seals"):
            bk, rev, content, h, prev = params
            self.rows.append((len(self.rows) + 1, bk, rev, json.loads(content), h, prev, "t"))
            self._r = []
        elif q.startswith("SELECT o.race_id"):
            self._r = self.content.get("orders", [])
        elif q.startswith("SELECT receipt, race_id"):
            self._r = self.content.get("votes", [])
        elif q.startswith("SELECT receipt, coalesce(bought"):
            self._r = self.content.get("receipts", [])
        elif q.startswith("SELECT DISTINCT v.id"):
            self._r = self.content.get("versions", [])
        elif q.startswith("SELECT count(*) FROM bet_decision_logs"):
            self._r = [(self.content.get("n_dlogs", 0),)]
        else:                                    # pragma: no cover - 想定外は落とす
            raise AssertionError(f"未知のSQL: {q[:70]}")
        return self

    def fetchone(self):
        return self._r[0] if self._r else None

    def fetchall(self):
        return list(self._r)


def _conn(bought=8900, payout=24110):
    return _SealConn({
        "orders": [("R1", "win", "08", 1000, 1, 7)],
        "votes": [("0007", "R1", "win", "08", 1000)],
        "receipts": [("0007", bought, payout)],
        "versions": [(7, 1, "abc", "live")],
        "n_dlogs": 12,
    })


def test_seal_records_the_ipat_amounts_as_the_truth():
    """★金額の真実源は IPAT 側。bet_orders は『出した指示』で成立額ではない。"""
    conn = _conn()
    r = seal_day(conn, "20261010")
    assert r["status"] == "sealed" and r["revision"] == 1
    assert r["bought"] == 8900 and r["payout"] == 24110


def test_sealing_twice_with_the_same_content_is_a_no_op():
    conn = _conn()
    seal_day(conn, "20261010")
    assert seal_day(conn, "20261010")["status"] == "unchanged"
    assert len(conn.rows) == 1


def test_a_later_correction_appends_a_link_instead_of_overwriting():
    """★締めをやり直して払戻が増えても上書きしない。訂正の事実ごと残す。"""
    conn = _conn(payout=0)
    seal_day(conn, "20261010")
    conn.content["receipts"] = [("0007", 8900, 24110)]
    r = seal_day(conn, "20261010")
    assert r["status"] == "sealed" and r["revision"] == 2
    assert len(conn.rows) == 2
    assert conn.rows[1][5] == conn.rows[0][4]      # prev_hash が前の環を指す


def test_chain_is_healthy_when_untouched():
    conn = _conn()
    seal_day(conn, "20261010")
    conn.content["receipts"] = [("0007", 1000, 0)]
    seal_day(conn, "20261011")
    assert verify_chain(conn) == []


def test_rewriting_a_sealed_day_breaks_the_chain():
    """★これが本題。過去の1日を書き換えたら分かること。"""
    conn = _conn()
    seal_day(conn, "20261010")
    conn.content["receipts"] = [("0007", 1000, 0)]
    seal_day(conn, "20261011")

    seq, bk, rev, content, h, prev, at = conn.rows[0]
    content["totals"]["payout"] = 999999          # こっそり書き換える
    conn.rows[0] = (seq, bk, rev, content, h, prev, at)

    bad = verify_chain(conn)
    assert len(bad) == 1 and bad[0]["budget_key"] == "20261010"
    assert any("書き換え" in w for w in bad[0]["problems"])


def test_prev_hash_is_part_of_the_hash_input():
    """★content だけを固めて prev を隣に置くだけでは鎖にならない。"""
    c = {"a": 1}
    assert content_hash(c, None) != content_hash(c, "deadbeef")


def test_empty_day_is_not_sealed():
    conn = _SealConn({})
    assert seal_day(conn, "20261012")["status"] == "empty"
    assert conn.rows == []

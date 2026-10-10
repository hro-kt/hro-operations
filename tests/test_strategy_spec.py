"""戦略書(仕様)。**運用実績は入れない**。

★これまでの生成物は運用報告書だった。購入額・回収率・網羅性は「どう運用したか」で
  あって「何をなぜどう買うのか」ではない。
★自動生成はそのまま確定版にしない。下書き → 編集 → 確定。確定後は変更できない。
"""

from __future__ import annotations

import pytest

from hro_operations.strategy import flatten, params_diff
from hro_operations.strategy_spec import (
    discard_draft,
    missing_sections,
    publish,
    render,
    save_draft,
    save_note,
)


def _ver(**kw):
    v = {"id": 3, "name": None, "version": 2, "params_hash": "c" * 64, "mode": "live",
         "params": {"flow": {"source": "netkeiba", "lead_seconds": 90,
                             "flow_minutes": 6, "thresholds": {"90": 0.1368},
                             "min_ninki": 7, "max_ninki": 25, "partners": 3,
                             "window_tolerance_sec": 60},
                    "bets": [{"bet_type": "tan", "amount": 1000},
                             {"bet_type": "umatan", "amount": 100}],
                    "money": {"flat_amount": 1000, "max_amount_per_order": 5000,
                              "max_amount_per_day": 20000},
                    "execution": {"deadline_lead_seconds": 60,
                                  "act_lead_seconds": 70}}}
    v.update(kw)
    return v


def _doc(notes=None, **kw):
    return render(_ver(**kw), notes or [], now="2026-12-01 09:00 JST")


# --- 構成 ---------------------------------------------------------------

def test_spec_has_the_specified_sections():
    d = _doc()
    for head in ("③. 目的・基本仮説", "④. 対象範囲", "⑤. 購入判定ロジック",
                 "⑥. 資金配分ロジック", "⑦. モデル仕様", "⑧. 収益性の検証",
                 "⑨. 実行・例外処理"):
        assert f"## {head}" in d, head


def test_spec_excludes_the_metadata_sections():
    """★② 適用期間・⑩ 変更管理・⑪ 運用実績は仕様書の外で記録する。"""
    d = _doc()
    for head in ("## ②", "## ⑩", "## ⑪"):
        assert head not in d, head
    # 運用実績の数字が紛れ込んでいないこと
    # ★「期待回収率」は ⑤ の判定根拠なので仕様の一部。実績の「回収率」とは別物
    for word in ("購入総額", "払戻総額", "網羅率", "継続性", "購入日数", "払戻総額"):
        assert word not in d, word
    assert "期待回収率" in d                      # これは仕様として在ってよい
    # どこに在るかは明記する
    assert "本書の対象外" in d


def test_spec_identifies_the_strategy_and_the_config_hash():
    d = _doc()
    assert "`flow_tan`" in d and "v2" in d and "c" * 64 in d


# --- 機械が出す事実 -----------------------------------------------------

def test_scope_states_jra_only_and_the_exclusions():
    d = _doc()
    assert "日本中央競馬会(JRA)" in d and "地方競馬・海外競馬は一切対象としない" in d
    assert "7 番人気以降" in d and "25 番人気まで" in d
    assert "個々の競走を選んで購入することはしない" in d


def test_decision_logic_states_source_leads_and_threshold():
    d = _doc()
    assert "発走 90 秒前" in d and "発走 6 分前" in d
    assert "netkeiba" in d and "0.1368" in d
    assert "閾値が未定義の決定時点" in d


def test_thresholds_are_not_dumped_as_a_python_dict():
    """★提出資料に {'90': 0.1368} と出さない。"""
    d = _doc()
    assert "{'90'" not in d and '{"90"' not in d


def test_money_section_lists_each_bet_type_and_the_caps():
    d = _doc()
    assert "単勝 1点あたり | 1,000 円" in d
    assert "馬単(軸1着固定) 1点あたり | 100 円" in d
    assert "5,000 円" in d and "20,000 円" in d
    assert "定額" in d


def test_execution_section_states_the_deadline_policy():
    d = _doc()
    assert "発走 70 秒前" in d and "発走 60 秒前" in d
    assert "その後の変動で判断を変えない" in d
    assert "復旧後に遡って購入することはしない" in d


def test_model_section_says_it_is_model_free():
    d = _doc()
    assert "model-free" in d and "学習は行わない" in d


# --- 人が書く部分 -------------------------------------------------------

def test_authored_sections_are_marked_as_missing_when_empty():
    """★未記入のまま提出すると、仮説も検証も無いまま買っていたように見える。"""
    d = _doc()
    assert d.count("**未記入。**") == 2          # purpose と evidence
    assert "strategy-note --version 3 --section purpose" in d


def test_authored_body_is_shown_with_its_author():
    d = _doc([{"section": "purpose", "version_id": None, "revision": 1,
               "body": "直前の資金移動に情報が集中する。", "authored_by": "友田",
               "authored_at": "2026-12-01T09:00:00"}])
    assert "直前の資金移動に情報が集中する。" in d
    assert "記載者: 友田" in d
    assert d.count("**未記入。**") == 1          # evidence だけ残る


def test_version_specific_note_wins_over_the_shared_one():
    """版ごとの検証結果が、全版共通の記述に負けないこと。"""
    d = _doc([{"section": "evidence", "version_id": None, "revision": 1,
               "body": "共通の記述", "authored_by": "a", "authored_at": ""},
              {"section": "evidence", "version_id": 3, "revision": 1,
               "body": "この版の検証", "authored_by": "b", "authored_at": ""}])
    assert "この版の検証" in d and "共通の記述" not in d


def test_missing_sections_lists_only_the_essential_ones():
    assert missing_sections([]) == ["purpose", "evidence"]
    got = missing_sections([{"section": "purpose", "version_id": None}])
    assert got == ["evidence"]


# --- 変更管理(⑩) --------------------------------------------------------

def test_diff_is_computed_not_authored():
    """★差分を手で書かせると書き漏れるし、実際と合う保証も無い。"""
    a = {"flow": {"min_ninki": 7, "lead_seconds": 90}, "mode": "live"}
    b = {"flow": {"min_ninki": 4, "lead_seconds": 90}, "mode": "live"}
    assert params_diff(a, b) == {"flow.min_ninki": {"from": 7, "to": 4}}


def test_diff_reports_added_and_removed_keys():
    d = params_diff({"a": 1}, {"a": 1, "b": {"c": 2}})
    assert d == {"b.c": {"from": None, "to": 2}}


def test_first_version_has_no_diff():
    assert params_diff(None, {"a": 1}) == {}


def test_flatten_handles_nesting():
    assert flatten({"a": {"b": {"c": 1}}, "d": 2}) == {"a.b.c": 1, "d": 2}


# --- 下書き / 確定 ------------------------------------------------------

class _SpecConn:
    def __init__(self):
        self.rows: list[dict] = []
        self._r = []

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        draft = next((r for r in self.rows
                      if r["version_id"] == params[0] and r["status"] == "draft"), None) \
            if params else None
        if q.startswith("SELECT id, revision, content_hash FROM strategy_specs"):
            self._r = [(draft["id"], draft["revision"], draft["hash"])] if draft else []
        elif q.startswith("SELECT max(revision) FROM strategy_specs"):
            rs = [r["revision"] for r in self.rows if r["version_id"] == params[0]]
            self._r = [(max(rs) if rs else None,)]
        elif q.startswith("INSERT INTO strategy_specs"):
            sid, vid, rev, md, h = params
            self.rows.append({"id": len(self.rows) + 1, "version_id": vid,
                              "revision": rev, "status": "draft", "md": md, "hash": h})
            self._r = [(self.rows[-1]["id"],)]
        elif q.startswith("UPDATE strategy_specs SET markdown"):
            md, h, rid = params
            r = next(x for x in self.rows if x["id"] == rid)
            if r["status"] == "published":
                raise RuntimeError("確定済みの戦略書は変更できません")
            r.update(md=md, hash=h)
            self._r = []
        elif q.startswith("UPDATE strategy_specs SET status='published'"):
            by, rid = params
            r = next(x for x in self.rows if x["id"] == rid)
            r.update(status="published", by=by)
            self._r = []
        elif q.startswith("DELETE FROM strategy_specs"):
            gone = [r for r in self.rows
                    if r["version_id"] == params[0] and r["status"] == "draft"]
            self.rows = [r for r in self.rows if r not in gone]
            self._r = [(r["id"],) for r in gone]
        else:                                    # pragma: no cover
            raise AssertionError(q[:70])
        return self

    def fetchone(self):
        return self._r[0] if self._r else None

    def fetchall(self):
        return list(self._r)


def test_generating_creates_a_draft():
    c = _SpecConn()
    r = save_draft(c, 3, "# spec")
    assert r["status"] == "created" and r["revision"] == 1
    assert c.rows[0]["status"] == "draft"


def test_regenerating_does_not_overwrite_an_edited_draft():
    """★人が編集した内容を機械が黙って消すのが最悪。"""
    c = _SpecConn()
    save_draft(c, 3, "# spec")
    save_draft(c, 3, "# 人が直した")
    r = save_draft(c, 3, "# 機械が作り直した", regenerate=True)
    assert r["status"] == "draft_exists"
    assert c.rows[0]["md"] == "# 人が直した"


def test_editing_updates_the_draft_in_place():
    c = _SpecConn()
    save_draft(c, 3, "# spec")
    r = save_draft(c, 3, "# 直した")
    assert r["status"] == "edited" and r["revision"] == 1
    assert len(c.rows) == 1 and c.rows[0]["md"] == "# 直した"


def test_publishing_fixes_the_draft():
    c = _SpecConn()
    save_draft(c, 3, "# spec")
    r = publish(c, 3, by="友田")
    assert r["status"] == "published" and r["revision"] == 1
    assert c.rows[0]["status"] == "published" and c.rows[0]["by"] == "友田"


def test_published_spec_cannot_be_edited():
    """★確定後は変更できない。DB のトリガでも拒否する。"""
    c = _SpecConn()
    save_draft(c, 3, "# spec")
    publish(c, 3, by="友田")
    # 確定済みしか無いので下書きは見つからず、新しい revision の下書きになる
    r = save_draft(c, 3, "# 直したい")
    assert r["status"] == "created" and r["revision"] == 2
    assert c.rows[0]["md"] == "# spec"          # 確定版は変わらない


def test_publishing_without_a_draft_is_refused():
    assert publish(_SpecConn(), 3)["status"] == "no_draft"


def test_discard_removes_only_the_draft():
    c = _SpecConn()
    save_draft(c, 3, "# spec")
    publish(c, 3)
    save_draft(c, 3, "# 次の下書き")
    assert discard_draft(c, 3) == 1
    assert len(c.rows) == 1 and c.rows[0]["status"] == "published"


def test_note_section_is_validated():
    with pytest.raises(ValueError):
        save_note(_SpecConn(), section="nonsense", body="x", version_id=None,
                  authored_by=None)


# --- ⑧ 机上検証の記録 ---------------------------------------------------

def _bt(**kw):
    b = {"label": "単勝×人気7+ 2窓OOS 前半", "bet_type": "tan",
         "period_from": "20250906", "period_to": "20260301", "n_bets": 658,
         "hit_rate": 0.0836, "roi": 1.6690, "p_le_1": 0.007,
         "ran_at": "2026-09-28T12:00:00"}
    b.update(kw)
    return b


def test_evidence_section_renders_recorded_backtests():
    """★自由記述にしない。実際に走らせた結果をそのまま載せる。"""
    d = _doc(**{}) if False else render(_ver(effective_from="20261011"), [],
                                        now="x", backtests=[_bt()])
    assert "1.6690" in d and "0.007" in d and "658" in d
    assert "転記ではありません" in d


def test_evidence_marks_validation_done_before_deployment():
    """★「事前に期待回収率を見積もって購入していた」の裏づけは順序。"""
    v = _ver(effective_from="20261011")
    d = render(v, [], now="x", backtests=[_bt(ran_at="2026-09-28T12:00:00"),
                                          _bt(label="後から回した検証",
                                              ran_at="2026-11-02T10:00:00")])
    pre = next(x for x in d.splitlines() if "2窓OOS 前半" in x)
    post = next(x for x in d.splitlines() if "後から回した検証" in x)
    assert "★" in pre and "★" not in post
    assert "事前検証" in d


def test_evidence_section_says_when_nothing_is_recorded():
    d = render(_ver(), [], now="x", backtests=[])
    assert "**未記録。**" in d and "--record" in d


def test_evidence_interpretation_is_separate_from_the_numbers():
    """★数字は機械、解釈は人。文書上で分ける。"""
    d = render(_ver(), [{"section": "evidence", "version_id": None, "revision": 1,
                         "body": "人気7+ に集中している。", "authored_by": "友田",
                         "authored_at": "2026-12-01T09:00:00"}],
               now="x", backtests=[_bt()])
    assert "### 机上検証(バックテスト)" in d and "### 検証の解釈" in d
    assert "人気7+ に集中している。" in d and "記載者: 友田" in d


def test_extract_pulls_the_headline_values():
    from hro_operations.evidence import extract
    rep = {"bets": 658, "roi": 1.669, "hit_rate": 0.0836,
           "ci": {"lo": 1.1, "hi": 2.3, "p_le_1": 0.007}}
    assert extract(rep) == {"n_bets": 658, "roi": 1.669, "hit_rate": 0.0836,
                            "p_le_1": 0.007}


def test_extract_survives_a_run_without_ci():
    from hro_operations.evidence import extract
    assert extract({"bets": 0})["p_le_1"] is None


def test_recorded_result_drops_the_per_bet_detail():
    """★明細まで入れると行が巨大になる。代表値と設定だけ残す。"""
    from hro_operations.evidence import record

    class _C:
        def __init__(self):
            self.params = None

        def execute(self, sql, params=()):
            self.params = params
            return self

        def fetchone(self):
            return (1,)

    import json
    c = _C()
    record(c, label="L", kind="flow-backtest", bet_type="tan",
           period=("20250906", "20260301"),
           params={"threshold": 0.1368},
           result={"bets": 10, "roi": 1.5, "details": [1, 2, 3], "_bets": [4, 5]},
           command="hro-ops flow-backtest …")
    stored = json.loads(c.params[7])
    assert "details" not in stored and "_bets" not in stored
    assert stored["bets"] == 10

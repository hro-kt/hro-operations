"""執行の所要時間を実測ログから出す。

★「判断をどこまで遅らせられるか」は所要時間の**分布**でしか決まらない。
  いまは発注開始 発走-90秒 / 締切 発走-60秒 で持ち時間30秒。
  «たいてい3秒» では決められない。«悪い日でも何秒» が要る。
★新しい計測は足さない。IPAT の各相は完了時に "<相>: <ms>ms" を出しており
  (ipat.TIMED_PHASES)、エージェントが ops_job.log へ流している。過去ぶんが全部残っている。
"""

from __future__ import annotations

import pytest

from hro_operations.exec_timing import (IN_WINDOW, PHASES, budget_advice,
                                        parse_log, percentile, phase_table, upper)

_LOG = """
2026-10-24 16:11:30,001 INFO 1234: 画面を先に進めます(T-150s, 式別=win)
2026-10-24 16:11:30,002 INFO preselect: 2310ms
2026-10-24 16:13:31,101 INFO vote: 820ms
2026-10-24 16:13:32,101 INFO vote: 910ms
2026-10-24 16:13:33,101 INFO vote_done: 120ms
2026-10-24 16:13:34,101 INFO confirm: 640ms
2026-10-24 16:13:35,101 INFO execute: 851ms
2026-10-24 16:13:36,101 INFO receipt: 430ms
2026-10-24 16:13:36,201 INFO IPAT 投票受付 京都 12 単勝 2点 計2000円 receipt=0007
"""


def test_a_submit_ends_at_the_receipt():
    done, partial = parse_log(_LOG)
    assert len(done) == 1 and partial == []
    assert done[0].by_phase() == {"preselect": 2310, "vote": 1730,
                                  "vote_done": 120, "confirm": 640,
                                  "execute": 851, "receipt": 430}


def test_preselect_is_not_counted_against_the_deadline():
    """★preselect は判断の2分前に終わっている前提。含めると«前倒しが効いて
       いない日»の時間で全体を悲観することになる。"""
    done, _ = parse_log(_LOG)
    assert done[0].total() == 1730 + 120 + 640 + 851 + 430
    assert "preselect" not in IN_WINDOW


def test_repeated_votes_are_summed():
    """★1レースで複数頭買うと vote が頭数ぶん走る。合計が窓を食う。"""
    done, _ = parse_log(_LOG)
    assert done[0].by_phase()["vote"] == 820 + 910


def test_a_failed_attempt_is_kept_not_discarded():
    """★receipt に届かなかった塊も返す。失敗した投票も窓を食うので、
       捨てると都合の良い分布になる。"""
    log = _LOG + "\n2026-10-24 16:14:00,000 INFO preselect: 500ms\n" \
                 "2026-10-24 16:14:03,000 INFO vote: 18300ms\n"
    done, partial = parse_log(log)
    assert len(done) == 1 and len(partial) == 1
    assert partial[0].total() == 18300


def test_a_new_preselect_starts_a_new_group():
    log = ("INFO preselect: 100ms\nINFO vote: 200ms\n"
           "INFO preselect: 300ms\nINFO vote: 400ms\nINFO receipt: 50ms\n")
    done, partial = parse_log(log)
    assert len(partial) == 1 and partial[0].by_phase() == {"preselect": 100, "vote": 200}
    assert done[0].by_phase() == {"preselect": 300, "vote": 400, "receipt": 50}


def test_unrelated_lines_are_ignored():
    assert parse_log("INFO 何かのメッセージ 1234ms かかりました\n") == ([], [])


def test_every_timed_phase_is_recognised():
    """★ipat.TIMED_PHASES と顔ぶれを揃える。片方だけ増やすと黙って測り落とす。"""
    from hro_buyer.ipat import TIMED_PHASES

    assert set(PHASES) == set(TIMED_PHASES)


# --- 判断 ------------------------------------------------------------------- #
def _subs(totals):
    from hro_operations.exec_timing import Submit
    return [Submit(phases=[("vote", t), ("receipt", 0)]) for t in totals]


def test_it_uses_an_upper_quantile_not_the_median():
    """★«たいてい間に合う»では駄目。間に合わなかった日はレースを丸ごと落とす。"""
    b = budget_advice(_subs([1000] * 99 + [25000]), lead_seconds=90,
                      deadline_lead_seconds=60, safety=0.99)
    assert b["p50"] == 1000
    # ★補間の p99 は 1,240ms になり、実際に起きた25秒を予算から外してしまう。
    assert percentile([1000.0] * 99 + [25000.0], 0.99) < 2000
    assert b["need_ms"] == 25000          # 上側に倒している


def test_it_says_how_much_later_we_could_act():
    b = budget_advice(_subs([5000] * 100), lead_seconds=90,
                      deadline_lead_seconds=60, safety=0.99)
    assert b["have_ms"] == 30000
    assert b["slack_ms"] == pytest.approx(25000)
    assert b["suggested_lead_seconds"] == 65      # 締切60s + 5s


def test_it_never_suggests_acting_after_the_deadline():
    """★どんなに速くても締切より後ろには行かない。"""
    b = budget_advice(_subs([1] * 10), lead_seconds=90, deadline_lead_seconds=60)
    assert b["suggested_lead_seconds"] > 60


def test_it_says_so_when_there_is_not_enough_time():
    b = budget_advice(_subs([40000] * 10), lead_seconds=90, deadline_lead_seconds=60)
    assert b["slack_ms"] < 0
    assert b["suggested_lead_seconds"] > 90       # いまより早める必要がある


def test_upper_falls_back_to_the_worst_when_samples_are_few():
    """★件数が少ないと高い分位は推定できない。自然に最大値へ寄ること。"""
    assert upper([1.0, 2.0, 99.0], 0.99) == 99.0
    assert upper([5.0], 0.99) == 5.0
    assert upper([], 0.99) == 0.0


def test_percentile_interpolates():
    assert percentile([0.0, 10.0], 0.5) == pytest.approx(5.0)
    assert percentile([], 0.9) == 0.0


def test_phase_table_marks_which_phases_eat_the_window():
    done, _ = parse_log(_LOG)
    t = {r["phase"]: r["in_window"] for r in phase_table(done)}
    assert t["preselect"] is False and t["execute"] is True


def test_harmless_groups_are_not_reported_as_failures():
    """★session_check だけの塊は窓内 0ms で無害。数が多いのが普通(実測 281件)。
       «投票を始めたのに receipt に届かなかった» 塊だけが本当の失敗。
       混ぜると件数に驚くだけで読めない。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "__main__.py").read_text(encoding="utf-8")
    i = src.index("相のログだけの塊")
    head = src[i - 500:i]
    assert "x.total() > 0" in head


def test_the_estimate_says_what_it_does_not_cover():
    """★IPAT の操作だけ。信号の計算と DB 往復はこの前にあり入っていない。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "hro_operations" / "__main__.py").read_text(encoding="utf-8")
    assert "IPAT の操作だけ" in src
    assert "信号の計算と DB 往復" in src

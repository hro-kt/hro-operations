"""執行に何秒かかっているかを、実測ログから出す。

★「判断をどこまで遅らせられるか」は**所要時間の分布**でしか決まらない。
  いまは発注開始 = 発走-90秒、締切 = 発走-60秒 で持ち時間30秒。これを詰めるなら、
  «たいてい3秒» ではなく «悪い日でも何秒» を知る必要がある。平均は役に立たない。

★データは既にある。IPAT の各相は完了時に `"<相>: <ms>ms"` を INFO で出しており
  (ipat.TIMED_PHASES)、エージェントが子の標準出力を ops_job.log へ流している。
  新しく計測を足さなくても、過去の開催日ぶんが全部残っている。

★相の並び(submit_batch):
    session_check → preselect → vote×N → vote_done → confirm → execute → receipt
  このうち **preselect は判断の2分前に終わっているはず**なので、締切までの
  持ち時間に乗るのは vote 以降。preselect が窓の中で走っていたら、それは
  前倒しが効いていない日(別途警告を出している)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ipat.TIMED_PHASES と同じ顔ぶれ。★片方だけ増やすと黙って測り落とす。
PHASES = ("session_check", "preselect", "vote", "vote_umatan", "vote_done",
          "confirm", "execute", "receipt")

# ★締切までの持ち時間に乗る相。preselect は前倒し済みの前提なので入れない。
IN_WINDOW = ("vote", "vote_umatan", "vote_done", "confirm", "execute", "receipt")

# 例: "2026-10-24 16:13:33,219 INFO execute: 851ms"
_LINE = re.compile(r"\b(" + "|".join(PHASES) + r"):\s*(\d+)ms\b")


@dataclass
class Submit:
    """1回の送信(= receipt まで到達した1まとまり)。"""
    phases: list[tuple[str, int]] = field(default_factory=list)

    def total(self, only: tuple[str, ...] = IN_WINDOW) -> int:
        return sum(ms for ph, ms in self.phases if ph in only)

    def by_phase(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for ph, ms in self.phases:
            out[ph] = out.get(ph, 0) + ms
        return out


def parse_log(text: str) -> tuple[list[Submit], list[Submit]]:
    """ログから (成立した送信, 途中で終わったもの) を取り出す。★純粋関数。

    ★`receipt` が1回の送信の終わり。そこで区切る。
    ★`receipt` に届かなかった塊も捨てずに返す。失敗した投票がどれだけ時間を
      食ったかは「遅らせてよいか」の判断に直接効く(捨てると都合の良い分布になる)。
    ★相は accumulate しない。ログ行は**その回の**所要時間を出している。
    """
    done: list[Submit] = []
    partial: list[Submit] = []
    cur = Submit()
    for line in text.splitlines():
        m = _LINE.search(line)
        if m is None:
            continue
        phase, ms = m.group(1), int(m.group(2))
        # ★session_check / preselect は新しい送信の入口。直前の塊を打ち切る。
        if phase in ("session_check", "preselect") and cur.phases:
            partial.append(cur)
            cur = Submit()
        cur.phases.append((phase, ms))
        if phase == "receipt":
            done.append(cur)
            cur = Submit()
    if cur.phases:
        partial.append(cur)
    return done, partial


def percentile(xs: list[float], q: float) -> float:
    """昇順でなくてよい。q は 0〜1。★numpy を引き込まない。"""
    if not xs:
        return 0.0
    ys = sorted(xs)
    if q <= 0:
        return ys[0]
    if q >= 1:
        return ys[-1]
    pos = q * (len(ys) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ys) - 1)
    return ys[lo] + (ys[hi] - ys[lo]) * (pos - lo)


def upper(xs: list[float], q: float) -> float:
    """上側に倒した分位(nearest-rank)。★予算はこちらで取る。

    補間の分位は «実際には起きていない中間の値» を返す。100件中1件だけ25秒
    かかった日があるとき、補間の p99 は 1.24秒になり、**その25秒を予算から
    外してしまう**。間に合わなかった日はそのレースを丸ごと落とすので、
    ここは楽観してはいけない。件数が少ないときは自然に最大値へ寄る。
    """
    import math

    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, math.ceil(q * (len(ys) - 1)))]


def phase_table(submits: list[Submit]) -> list[dict]:
    """相ごとの分布。どこに時間を使っているかを見る。"""
    out = []
    for ph in PHASES:
        xs = [float(ms) for s in submits for p, ms in s.phases if p == ph]
        if not xs:
            continue
        out.append({"phase": ph, "n": len(xs),
                    "p50": percentile(xs, 0.50), "p90": percentile(xs, 0.90),
                    "p95": percentile(xs, 0.95), "max": max(xs),
                    "in_window": ph in IN_WINDOW})
    return out


def budget_advice(submits: list[Submit], *, lead_seconds: int,
                  deadline_lead_seconds: int, safety: float = 0.99) -> dict:
    """いまの持ち時間と、必要な時間の分布を突き合わせる。

    ★基準は中央値ではなく上側の分位。投票は «たいてい間に合う» では駄目で、
      間に合わなかった日はそのレースを丸ごと落とす。
    ★preselect は含めない(判断の2分前に終わっている前提)。含めると
      «前倒しが効いていない日» の時間で全体を悲観することになる。
    """
    have_ms = (lead_seconds - deadline_lead_seconds) * 1000.0
    xs = [float(s.total()) for s in submits]
    need = upper(xs, safety)
    # 必要なぶんだけ残して、残りは遅らせられる
    slack_ms = have_ms - need
    return {
        "n": len(xs),
        "have_ms": have_ms,
        "p50": percentile(xs, 0.50),
        "p90": percentile(xs, 0.90),
        "p95": percentile(xs, 0.95),
        "need_ms": need,
        "safety": safety,
        "max_ms": max(xs) if xs else 0.0,
        "slack_ms": slack_ms,
        # ★遅らせられる先。締切より後ろには絶対に行かない。
        "suggested_lead_seconds": max(
            deadline_lead_seconds + 1,
            int(deadline_lead_seconds + (need / 1000.0) + 0.999)),
    }

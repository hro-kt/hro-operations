"""締切直前の単勝プール資金移動シグナル(flow_tan)で複勝の発注候補を作る。

検証結果(2026-09, `hro-backtest/docs/2026-09_flow_signal.md`):
  現象は TYB直前オッズ→確定オッズ で **11年中11年**に再現(+0.144, 対照+0.004)。
  運用形(決定時点で見える情報のみ・払戻は実績)で **複勝 ROI 1.1741 [1.058,1.298]**、
  P(ROI<=1)=0.001、8ヶ月中8ヶ月、的中34.2%、平均odds3.61、月約185本。
  リーク監査済(締切後 0/48,329、実際の決定時点は T−60s)。

スコア: レース内の単勝プール占有率 share_i = (1/tan_odds_i) / Σ_j (1/tan_odds_j) の
        logit 差 = logit(share_i at T−lead) − logit(share_i at T−flow)
        1/tan_odds は単勝プールの占有率そのもの(複勝下限は他馬の組合せに依存し粗い)。
選別: **fit 期間で決めた絶対閾値**を超えたものを全部買う。1レース固定N点だと信号の弱い
      レースでも無理に買って薄まる(実測 1.17→1.006)。効果の大半は「どのレースで賭けるか」。
★モデルを一切使わない。フィットするパラメータも無い。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import timedelta

log = logging.getLogger(__name__)

# ★時刻の扱い: nl_ra.hasso_time(発走 HHMM)も ts_o1.hasso_time(スナップ MMDDHHMI)も **JST**。
#   to_timestamp はセッションのタイムゾーンで解釈するので、セッションが UTC だと 9 時間ずれる。
#   スナップ同士の比較なら同じだけずれて相殺されるが、observed_at(実時刻)と比べると破綻する
#   (実際に「取得の余裕 32,325秒」のような値が出た)。必ず AT TIME ZONE 'Asia/Tokyo' で固定する。

# 学習不要。ts_o1 / ts_sokuho_o1 のどちらからでも同じ量が作れる。
_SRC = {
    "ts": ("ts_o1", "hasso_time"),            # 公式時系列(0B41)。発表時刻の格子
    "sokuho": ("ts_sokuho_o1", "hasso_time"),   # 自前10秒ポーリング(0B30)。基準は発表時刻
    # netkeiba は**実時刻(observed_at)**が基準。分格子ではないのでリードを秒で選べる。
    # JV-Link は締切(発走-60秒)前に届く発表が T-120s までだが、netkeiba は締切直前まで
    # 秒単位で動くため、判断時点を発走-70秒付近まで寄せられる。
    "netkeiba": ("ts_netkeiba_o1", "observed_at"),
}


@dataclass
class FlowConfig:
    lead_seconds: int = 60      # 決定時点 = 発走 − これ秒(0B41 の格子は T−60s)
    flow_minutes: int = 6       # フローの起点 = 発走 − これ分
    threshold: float = 0.0      # スコアの絶対閾値(fit 期間の分位から決めた値)
    source: str = "ts"          # ts | sokuho
    max_odds: float = 0.0       # >0 で複勝オッズ上限(荒れすぎを弾く)
    # ★決定時点の**単勝**オッズで帯を絞る。回収率がオッズ帯で大きく違うため
    #   (2026-09-25 実測, ts@T-60s を3期間に分割):
    #     2.0-4.0倍  1.038 / 1.025 / 1.046   ← 小さいが極めて安定(的中67-71%)
    #     4.0-20倍   前後で入れ替わり一貫性なし
    #     20-40倍    1.584 / 1.255 / 1.566   ← 大きい(347本で平均1.45)
    #     40倍超     0.546 / 0.903 / 2.617   ← 人気-穴バイアスで不安定
    #   ★帯は同じデータから見つけたもの。真の OOS 検証は**これから先の期間**でしかできない。
    min_tan_odds: float = 0.0   # >0 でこの倍率未満を除外
    max_tan_odds: float = 0.0   # >0 でこの倍率超を除外
    # ★人気で絞る。オッズ帯と近い領域を指すが、**頭数で正規化されている**ぶん
    #   頑健かもしれない(2026-09-25 実測: 7-10番人気が前半1.206/後半1.293で一貫、527本)。
    min_ninki: int = 0
    max_ninki: int = 0
    # ★出走頭数の下限。**複勝は出走8頭以上で3着まで、5〜7頭は2着まで、
    #   4頭以下は発売なし**。2着までの複勝は別物なので混ぜると条件の違うレースを
    #   まとめて最適化することになる。複勝を買うなら 8 を指定する。
    #   (単勝には関係ないので既定は 0 = 制限なし)
    min_horses: int = 0
    # ★1レースで買う上限。いまの executor は注文ごとに
    #   「投票→確認→送信→受付」を1周するので、同一レースで複数選ばれると
    #   2件目以降が締切を超えて捨てられる(2026-10-03 に実害: 3件中1件のみ成立)。
    #   どのみち通らないなら、失敗に時間を使って次のレースの準備を圧迫するより
    #   最初から絞る。スコアの高い順に残す。
    #   ★本来の解は購入予定リストへの**まとめ入力**(確認・送信を1回にする)。
    max_per_race: int = 0
    # ★そのレースの値動き総量で正規化するか(S/N比にする)。
    #   実測(2026-09-29, ts@T-60s を2期間): 値動きが**小さい**レースほど回収率が高い
    #     下位25% 1.182/1.235 → 下位50% 1.060/1.212 → 上位25% 0.994/0.985(単調・両期間一致)
    #   race_move はレース全体の変動量=**ノイズの水準**、flow はその馬の変動量=シグナル。
    #   盤面が荒れている中の1頭の動きは薄く、静かな盤面で動いたら情報、という読み。
    #   ★正規化すると尺度が変わるので**閾値を必ず取り直す**こと。
    normalize: bool = False
    # ★買う券種。エッジは人気7番以降=中穴に集中しているので、**単勝の方が効率が
    #   良い可能性がある**(配当が大きい)。確定払戻は nl_hr に年単位で揃っているので
    #   測るだけなら安い。live の発注は現状 place 固定なので、採用するなら
    #   flow_orders の bet_type も変える必要がある。
    bet_type: str = "fuku"   # fuku | tan | umatan(払戻の参照先)
    # ★組み合わせ券の相手に使う人気上位の頭数。2窓OOSで3頭が最良
    #   (5頭は本数が1.5倍になるが1円あたりは劣る。上限が自由なら単価で調整できる)。
    partners: int = 3
    # ★リード別の閾値 {実際のリード秒: 閾値}。決定時点は配信遅れでレースごとに変わり
    #   (2026-09-21 実測: T-120s が41%、残りは T-180s)、スコアの尺度もリードで変わる
    #   (ts@60 比の傾き T-120s=0.454 / T-180s=0.252)。単一の閾値を当てると、片方で
    #   ほぼ全件、もう片方でほぼ0件になる。**黙って0件**が一番危ないので分けて持つ。
    thresholds: dict[int, float] | None = None
    # ★実効窓(起点リード − 決定リード)が意図から何秒ずれるまで許すか。
    #   格子に穴があると実際の間隔が意図とずれ、窓が長いほどスコアが大きく出る。
    #   日によって窓が違うと**絶対閾値が比較できない**(2026-09-22: 疎なポーリングの日で
    #   取った閾値0.1631を、窓が正しく短い日に当てて候補0になった)。
    window_tolerance_sec: int = 60


def _logit(x: float, lo: float = 1e-6) -> float:
    x = min(max(x, lo), 1.0 - lo)
    return math.log(x / (1.0 - x))


_SQL_TS = """
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
snap AS (
  SELECT t.umaban, t.tan_odds, t.fuku_odds_low, (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts
  FROM ts_o1 t
  WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
),
-- PostgreSQL は UNION の**前**に ORDER BY を書けない(構文エラー)。DISTINCT ON は
-- ORDER BY と組で意味を持つので、枝ごとに CTE へ切り出す。
late AS (
  SELECT DISTINCT ON (umaban) umaban, tan_odds, fuku_odds_low, ts,
         EXTRACT(EPOCH FROM (ra.post - ts))::int AS lead_sec
  FROM snap, ra WHERE ts <= ra.post - make_interval(secs => %(lead)s)
  ORDER BY umaban, ts DESC
),
early AS (
  SELECT DISTINCT ON (umaban) umaban, tan_odds, fuku_odds_low, ts,
         EXTRACT(EPOCH FROM (ra.post - ts))::int AS lead_sec
  FROM snap, ra WHERE ts <= ra.post - make_interval(mins => %(flow)s)
  ORDER BY umaban, ts DESC
)
SELECT umaban, tan_odds, fuku_odds_low, ts, lead_sec, 'late' AS which FROM late
UNION ALL
SELECT umaban, tan_odds, fuku_odds_low, ts, lead_sec, 'early' AS which FROM early
"""

_SQL_SOKUHO = """
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
snap AS (
  SELECT t.umaban, t.tan_odds, t.fuku_odds_low,
         (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts
  FROM ts_sokuho_o1 t
  WHERE t.hasso_time ~ '^[0-9]{8}$'
    AND (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
),
-- PostgreSQL は UNION の**前**に ORDER BY を書けない(構文エラー)。DISTINCT ON は
-- ORDER BY と組で意味を持つので、枝ごとに CTE へ切り出す。
late AS (
  SELECT DISTINCT ON (umaban) umaban, tan_odds, fuku_odds_low, ts,
         EXTRACT(EPOCH FROM (ra.post - ts))::int AS lead_sec
  FROM snap, ra WHERE ts <= ra.post - make_interval(secs => %(lead)s)
  ORDER BY umaban, ts DESC
),
early AS (
  SELECT DISTINCT ON (umaban) umaban, tan_odds, fuku_odds_low, ts,
         EXTRACT(EPOCH FROM (ra.post - ts))::int AS lead_sec
  FROM snap, ra WHERE ts <= ra.post - make_interval(mins => %(flow)s)
  ORDER BY umaban, ts DESC
)
SELECT umaban, tan_odds, fuku_odds_low, ts, lead_sec, 'late' AS which FROM late
UNION ALL
SELECT umaban, tan_odds, fuku_odds_low, ts, lead_sec, 'early' AS which FROM early
"""



# ★netkeiba は単勝しか持たない。複勝(発注する券種)のオッズは JV 側の直近値を添える。
#   無ければその馬は落とす(BetOrder に odds が要るため)。
_SQL_NETKEIBA = """
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
snap AS (
  SELECT t.umaban, t.tan_odds, t.observed_at AS ts
  FROM ts_netkeiba_o1 t
  WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND t.tan_odds IS NOT NULL AND t.tan_odds > 0
),
fk AS (
  SELECT DISTINCT ON (t.umaban) t.umaban, t.fuku_odds_low
  FROM ts_sokuho_o1 t
  WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND t.hasso_time ~ '^[0-9]{8}$'
  ORDER BY t.umaban, t.hasso_time DESC
),
late AS (
  SELECT DISTINCT ON (s.umaban) s.umaban, s.tan_odds, s.ts,
         EXTRACT(EPOCH FROM (ra.post - s.ts))::int AS lead_sec
  FROM snap s, ra WHERE s.ts <= ra.post - make_interval(secs => %(lead)s)
  ORDER BY s.umaban, s.ts DESC
),
early AS (
  SELECT DISTINCT ON (s.umaban) s.umaban, s.tan_odds, s.ts,
         EXTRACT(EPOCH FROM (ra.post - s.ts))::int AS lead_sec
  FROM snap s, ra WHERE s.ts <= ra.post - make_interval(mins => %(flow)s)
  ORDER BY s.umaban, s.ts DESC
)
SELECT l.umaban, l.tan_odds, fk.fuku_odds_low, l.ts, l.lead_sec, 'late' AS which
FROM late l LEFT JOIN fk ON fk.umaban = l.umaban
UNION ALL
SELECT e.umaban, e.tan_odds, fk.fuku_odds_low, e.ts, e.lead_sec, 'early' AS which
FROM early e LEFT JOIN fk ON fk.umaban = e.umaban
"""


def _hhmmss(v) -> str:
    """理由文用の時刻表記。ts が NULL や文字列でも落とさない(発注を止めないため)。"""
    try:
        return v.strftime("%H:%M:%S")
    except Exception:
        return str(v)


def _num(v) -> float | None:
    """JV-Data のオッズ(10倍の整数文字列)→ 倍率。'0000'/空/'----' は None。"""
    s = (str(v) or "").strip()
    if not s.isdigit() or int(s) <= 0:
        return None
    return int(s) / 10.0


def _num_plain(v) -> float | None:
    """netkeiba のオッズ(NUMERIC、そのままの倍率)→ float。

    ★JV の10倍整数と**同じ関数で読んではいけない**。42.8 を _num に渡すと isdigit が
      False で None になり、全頭落ちて「スナップショット不足」に見える。
    """
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def _tan_reader(source: str):
    return _num_plain if source == "netkeiba" else _num


def threshold_for(cfg: "FlowConfig", lead_sec: int | None) -> float | None:
    """実際に使ったリードに対応する閾値。対応が無ければ None(=そのレースは見送る)。

    発表時刻は分格子なので実測リードは 60 の倍数に丸める。未設定のリードで
    近い値を流用すると尺度がずれた閾値で買うことになるため、あえて見送る。
    """
    if cfg.source == "netkeiba":
        # ★netkeiba は実時刻基準で、リードは我々が指定した値にほぼ一致する(格子が無い)。
        #   60秒格子に丸めると存在しないキーを引いて**黙って見送る**ので丸めない。
        if cfg.thresholds:
            return cfg.thresholds.get(int(cfg.lead_seconds))
        return cfg.threshold
    if not cfg.thresholds:
        return cfg.threshold
    if lead_sec is None:
        return None
    return cfg.thresholds.get(int(round(lead_sec / 60.0)) * 60)


def flow_scores(db, race: tuple[str, ...], cfg: FlowConfig) -> dict[str, dict]:
    """{馬番: {'score','fuku_odds','tan_odds','ts_late','ts_early'}}。取れない馬は含めない。"""
    y, m, j, k, n, r = race
    sql = {"ts": _SQL_TS, "sokuho": _SQL_SOKUHO, "netkeiba": _SQL_NETKEIBA}.get(cfg.source)
    if sql is None:
        raise ValueError(f"不明な flow 信号源: {cfg.source!r} (ts|sokuho|netkeiba)")
    tan_of = _tan_reader(cfg.source)
    rows = db.query(sql, {"y": y, "m": m, "j": j, "k": k, "n": n, "r": r,
                          "lead": cfg.lead_seconds, "flow": cfg.flow_minutes})
    late = {x["umaban"]: x for x in rows if x["which"] == "late"}
    early = {x["umaban"]: x for x in rows if x["which"] == "early"}
    if not late or not early:
        return {}
    # ★決定時点と起点が同じスナップを指したら flow は測れていない。ここで 0 を返すと
    #   「資金が動かなかった」と区別が付かず、threshold_from の分位点が構造的ゼロで
    #   汚れる(発走直前のスナップが無い日が混ざると閾値が実際より低く出る)。
    t_late, t_early = next(iter(late.values()))["ts"], next(iter(early.values()))["ts"]
    if t_late is not None and t_late == t_early:   # NULL なら判定不能→発注は止めない
        return {}
    s_late = sum(1.0 / o for x in late.values() if (o := tan_of(x["tan_odds"])))
    s_early = sum(1.0 / o for x in early.values() if (o := tan_of(x["tan_odds"])))
    if s_late <= 0 or s_early <= 0:
        return {}
    out: dict[str, dict] = {}
    n_no_fuku = 0
    for um, xl in late.items():
        xe = early.get(um)
        tl, te = tan_of(xl["tan_odds"]), tan_of(xe["tan_odds"]) if xe else None
        # ★複勝オッズは**常に JV-Link 側(ts_sokuho_o1)由来**。netkeiba は単勝しか出さない。
        #   速報ポーリングが止まっていると全頭ここで落ち、「スコア算出不可」に見える。
        fk = _num(xl["fuku_odds_low"])
        if fk is None:
            n_no_fuku += 1
        # ★複勝オッズを**必要なときだけ**必須にする。単勝を買うのに複勝オッズで
        #   馬を落としていた。2026-10-03 の再起動で速報ポーリングが止まった3レースは、
        #   単勝なら問題なく使えたのに全頭が消えた。
        #   さらに悪いのは**部分欠け**で、一部の馬だけ落ちると assign_ninki が
        #   残った馬だけで人気を振り直すため、「人気7番以降」が別の馬を指す。
        need_fk = cfg.bet_type == "fuku" or cfg.max_odds > 0
        if tl is None or te is None or (need_fk and fk is None):
            continue
        out[um] = {
            "score": _logit((1.0 / tl) / s_late) - _logit((1.0 / te) / s_early),
            "fuku_odds": fk, "tan_odds": tl,
            "ts_late": xl["ts"], "ts_early": xe["ts"],
            "lead_late": xl.get("lead_sec"), "lead_early": xe.get("lead_sec"),
            "window_sec": (xe["lead_sec"] - xl["lead_sec"]
                           if xl.get("lead_sec") is not None and xe.get("lead_sec") is not None
                           else None),
        }
    # ★人気は**決定時点の単勝オッズ順**から導出する。DB の tan_ninki は信号源によって
    #   有無が違い(netkeiba は持たない)、そのままだとバックテストとライブで別物になる。
    #   定義を1つにしておくこと。
    assign_ninki(out)
    # ★レース全体の値動き総量(ノイズの水準)。正規化にも診断にも使う。
    move = sum(abs(d["share_late"] - d["share_early"]) for d in out.values()) \
        if out and "share_late" in next(iter(out.values())) else None
    if move is None:
        move = 0.0
        for um, xl in late.items():
            xe = early.get(um)
            tl, te = tan_of(xl["tan_odds"]), tan_of(xe["tan_odds"]) if xe else None
            if tl and te:
                move += abs((1.0 / tl) / s_late - (1.0 / te) / s_early)
    for d in out.values():
        d["race_move"] = move
        if cfg.normalize:
            # ★0除算を避ける。動きが皆無なら信号も無いので 0 にする
            d["score"] = (d["score"] / move) if move > 1e-9 else 0.0
    if not out and n_no_fuku:
        # ★原因を名指しする。ここを黙って空で返すと「netkeiba が取れていない」と
        #   誤診して、動いている側を触って1日溶かす。
        log.warning("%s: 複勝オッズ(ts_sokuho_o1)が無いため %d/%d 頭を除外しました。"
                    "JV-Link の速報ポーリング(poll-odds)が止まっていませんか",
                    "".join(race), n_no_fuku, len(late))
    return out


_SQL_DIAG_TS = """
SELECT count(*) AS rows,
       count(DISTINCT t.hasso_time) AS snaps,
       count(DISTINCT t.umaban) AS horses,
       min((to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo')) AS first_ts,
       max((to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo')) AS last_ts,
       min(t.hasso_time) AS raw_min, max(t.hasso_time) AS raw_max
FROM ts_o1 t
WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
    = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
"""

_SQL_DIAG_SOKUHO = """
SELECT count(*) AS rows,
       count(DISTINCT t.hasso_time) AS snaps,
       count(DISTINCT t.umaban) AS horses,
       min((to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
            AT TIME ZONE 'Asia/Tokyo')) AS first_ts,
       max((to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
            AT TIME ZONE 'Asia/Tokyo')) AS last_ts,
       min(t.hasso_time) AS raw_min, max(t.hasso_time) AS raw_max
FROM ts_sokuho_o1 t
WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
    = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
"""


# ★netkeiba は時間軸が observed_at(実時刻)。JV 用の診断SQLを流用すると、
#   netkeiba を指定したのに **JV のスナップ格子を表示する**(2026-09-26 に実害:
#   30秒刻みのはずが「0s,60s,120s…」と出て、診断を信じると判断を誤る)。
_SQL_DIAG_NETKEIBA = """
SELECT count(*) AS rows,
       count(DISTINCT t.observed_at) AS snaps,
       count(DISTINCT t.umaban) AS horses,
       min(t.observed_at) AS first_ts,
       max(t.observed_at) AS last_ts,
       to_char(min(t.observed_at) AT TIME ZONE 'Asia/Tokyo', 'MMDDHH24MISS') AS raw_min,
       to_char(max(t.observed_at) AT TIME ZONE 'Asia/Tokyo', 'MMDDHH24MISS') AS raw_max
FROM ts_netkeiba_o1 t
WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
    = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
"""

_SQL_GRID_NETKEIBA = """
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
)
SELECT DISTINCT EXTRACT(EPOCH FROM (ra.post - t.observed_at))::int AS lead_sec
FROM ts_netkeiba_o1 t, ra
WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
    = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
  AND t.observed_at <= ra.post
ORDER BY 1
LIMIT %(lim)s
"""

_SQL_GRID = """
-- 発走前のスナップショットが「発走の何秒前」に在るか(格子の粗さを見る)
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
sn AS (
  SELECT DISTINCT t.hasso_time,
         (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts
  FROM ts_o1 t
  WHERE (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
)
SELECT sn.hasso_time,
       EXTRACT(EPOCH FROM (ra.post - sn.ts)) AS lead_sec
FROM sn, ra
WHERE sn.ts <= ra.post
ORDER BY sn.ts DESC
LIMIT %(lim)s
"""

_SQL_GRID_SOKUHO = """
WITH ra AS (
  SELECT (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
    AND hasso_time ~ '^[0-9]{4}$'
),
sn AS (
  SELECT DISTINCT t.hasso_time,
         (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts
  FROM ts_sokuho_o1 t
  WHERE t.hasso_time ~ '^[0-9]{8}$'
    AND (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num)
      = (%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
)
SELECT sn.hasso_time,
       EXTRACT(EPOCH FROM (ra.post - sn.ts)) AS lead_sec
FROM sn, ra
WHERE sn.ts <= ra.post
ORDER BY sn.ts DESC
LIMIT %(lim)s
"""


def snapshot_grid(db, race: tuple[str, ...], cfg: FlowConfig, limit: int = 15) -> list[dict]:
    """発走直前のスナップショットが「何秒前」に在るかを新しい順に返す。

    格子が粗いと、決定時点を早めたときに起点と同じスナップを引いてスコアが 0 になる
    (実際に lead 90/120/180 秒で全部同じ結果=スコアほぼ全ゼロになった)。
    """
    y, m, j, k, n, r = race
    sql = {"ts": _SQL_GRID, "sokuho": _SQL_GRID_SOKUHO,
           "netkeiba": _SQL_GRID_NETKEIBA}[cfg.source]
    return db.query(sql, {"y": y, "m": m, "j": j, "k": k, "n": n, "r": r, "lim": limit})


_SQL_POST = """
SELECT hasso_time,
       (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
FROM nl_ra
WHERE (year,month_day,jyo_cd,kaiji,nichiji,race_num)=(%(y)s,%(m)s,%(j)s,%(k)s,%(n)s,%(r)s)
"""


_SQL_COVERAGE = """
WITH ra AS (
  SELECT year, month_day, jyo_cd, kaiji, nichiji, race_num, hasso_time,
         (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE year=%(y)s AND month_day=%(m)s AND hasso_time ~ '^[0-9]{4}$'
    AND jyo_cd IN ('01','02','03','04','05','06','07','08','09','10')
),
sn AS (
  SELECT t.year, t.month_day, t.jyo_cd, t.kaiji, t.nichiji, t.race_num,
         (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts,
         t.observed_at
  FROM ts_o1 t
  WHERE t.year=%(y)s AND t.month_day=%(m)s
)
SELECT ra.jyo_cd, ra.race_num, ra.hasso_time, ra.post,
       count(sn.ts) AS snaps,
       max(sn.ts) FILTER (WHERE sn.ts <= ra.post - make_interval(secs => %(lead)s)) AS late_ts,
       max(sn.ts) FILTER (WHERE sn.ts <= ra.post - make_interval(mins => %(flow)s)) AS early_ts,
       max(sn.observed_at) FILTER (WHERE sn.ts <= ra.post - make_interval(secs => %(lead)s))
         AS late_fetched_at
FROM ra
LEFT JOIN sn USING (year, month_day, jyo_cd, kaiji, nichiji, race_num)
GROUP BY ra.jyo_cd, ra.race_num, ra.hasso_time, ra.post
ORDER BY ra.jyo_cd, ra.race_num
"""


_SQL_USABLE = """
-- 「締切の X 秒前に判断するとき、手元にある最新スナップは何秒前のものか」
-- observed_at は**最初に手元へ入った時刻**(上書きしない upsert に変更済み)。
WITH ra AS (
  SELECT year, month_day, jyo_cd, kaiji, nichiji, race_num, hasso_time,
         (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE year=%(y)s AND month_day=%(m)s AND hasso_time ~ '^[0-9]{4}$'
    AND jyo_cd IN ('01','02','03','04','05','06','07','08','09','10')
),
sn AS (
  SELECT t.year, t.month_day, t.jyo_cd, t.kaiji, t.nichiji, t.race_num, t.hasso_time,
         (to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS ts,
         min(t.observed_at) AS seen
  FROM %(TABLE)s t
  WHERE t.year=%(y)s AND t.month_day=%(m)s AND t.hasso_time ~ '^[0-9]{8}$'
  GROUP BY t.year, t.month_day, t.jyo_cd, t.kaiji, t.nichiji, t.race_num, t.hasso_time
)
SELECT ra.jyo_cd, ra.race_num, ra.hasso_time, ra.post,
       max(sn.ts) FILTER (WHERE sn.seen <= ra.post - make_interval(secs => %(dec)s))
         AS usable_ts,
       min(sn.seen) FILTER (WHERE sn.ts = ra.post - make_interval(secs => %(want)s))
         AS want_seen
FROM ra
LEFT JOIN sn USING (year, month_day, jyo_cd, kaiji, nichiji, race_num)
GROUP BY ra.jyo_cd, ra.race_num, ra.hasso_time, ra.post
ORDER BY ra.jyo_cd, ra.race_num
"""


def usable_snapshot(db, date: str, cfg: FlowConfig, *, deadline_seconds: int = 60,
                    margin_seconds: int = 10) -> list[dict]:
    """締切の margin 秒前に判断するとして、そのとき**実際に手元にある**最新スナップを見る。

    判断時刻 = 発走 − (deadline_seconds + margin_seconds)。
    want_seen は「発走 deadline_seconds 秒前と印の付いたスナップが最初に届いた時刻」で、
    これが判断時刻より前なら、検証と同じスナップをそのまま使える。
    """
    table = "ts_o1" if cfg.source == "ts" else "ts_sokuho_o1"
    sql = _SQL_USABLE.replace("%(TABLE)s", table)
    dec = deadline_seconds + margin_seconds
    rows = db.query(sql, {"y": date[:4], "m": date[4:8], "dec": dec, "want": deadline_seconds})
    out = []
    for r in rows:
        post = r["post"]
        decide_at = post - timedelta(seconds=dec) if post else None
        out.append({
            "jyo_cd": r["jyo_cd"], "race_num": r["race_num"], "hasso_time": r["hasso_time"],
            "usable_lead_sec": ((post - r["usable_ts"]).total_seconds()
                                if (post and r["usable_ts"]) else None),
            # 検証と同じスナップが判断時刻までに届いていたか(正なら余裕、負なら間に合わない)
            "want_margin_sec": ((decide_at - r["want_seen"]).total_seconds()
                                if (decide_at and r["want_seen"]) else None),
        })
    return out


def flow_coverage(db, date: str, cfg: FlowConfig) -> list[dict]:
    """開催日の全レースについて、決定時点(T−lead)のオッズが**間に合って**取れているかを見る。

    2つは別物なので両方返す:
      - スナップショットの時刻(hasso_time)が T−lead 以前にあるか … 信号を作れるか
      - その行を**いつ取得したか**(observed_at)が T−lead より前か … 締切前に使えたか
        (後から取り込んだ場合、検証はできても当日の発注には間に合っていない)
    """
    rows = db.query(_SQL_COVERAGE, {"y": date[:4], "m": date[4:8],
                                    "lead": cfg.lead_seconds, "flow": cfg.flow_minutes})
    out = []
    for r in rows:
        post, late_ts, fetched = r["post"], r["late_ts"], r["late_fetched_at"]
        decide_at = post - timedelta(seconds=cfg.lead_seconds) if post else None
        out.append({
            "jyo_cd": r["jyo_cd"], "race_num": r["race_num"], "hasso_time": r["hasso_time"],
            "post": post, "snaps": r["snaps"],
            "late_lead_sec": (post - late_ts).total_seconds() if (post and late_ts) else None,
            "has_early": r["early_ts"] is not None,
            # 取得が決定時点に間に合っていたか(正の秒数なら余裕、負なら間に合っていない)
            "fetch_margin_sec": ((decide_at - fetched).total_seconds()
                                 if (decide_at and fetched) else None),
        })
    return out


def flow_diagnose(db, race: tuple[str, ...], cfg: FlowConfig) -> dict:
    """なぜ発注が出ないのかを切り分けるための材料を集める(発注はしない)。

    見るのは3点: (1) 発走時刻が nl_ra に在るか (2) オッズのスナップショットが
    何時から何時まで何本在るか (3) 各馬のスコアと閾値。
    """
    y, m, j, k, n, r = race
    key = {"y": y, "m": m, "j": j, "k": k, "n": n, "r": r}
    post = db.query(_SQL_POST, key)
    # ★信号源ごとに診断SQLを分ける。流用すると netkeiba を指定したのに JV の格子を
    #   表示し、「30秒刻みのはずが60秒刻み」と読んで判断を誤る。
    diag_sql = {"ts": _SQL_DIAG_TS, "sokuho": _SQL_DIAG_SOKUHO,
                "netkeiba": _SQL_DIAG_NETKEIBA}[cfg.source]
    diag = db.query(diag_sql, key)
    scores = flow_scores(db, race, cfg)
    return {
        "race_id": "".join(race),
        "post": post[0] if post else None,
        "snapshots": diag[0] if diag else None,
        "scores": scores,
        "cutoff_late_seconds": cfg.lead_seconds,
        "cutoff_early_minutes": cfg.flow_minutes,
        "threshold": cfg.threshold,
        "n_above": sum(1 for d in scores.values() if d["score"] >= cfg.threshold),
    }


def _spearman(a: list[float], b: list[float]) -> float | None:
    """順位相関。scipy を使わない(依存を増やさない)。"""
    n = len(a)
    if n < 3:
        return None

    def rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        for pos, i in enumerate(order):
            r[i] = float(pos)
        return r

    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    dbb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return num / (da * dbb) if da and dbb else None


def lead_scan(db, races, leads: list[int], cfg: FlowConfig,
              ref_source: str = "ts", ref_lead: int = 60) -> list[dict]:
    """決定時点を早めたときに信号がどれだけ保たれるかを測る。

    基準は「検証で使った時点」(既定: 公式時系列の発走60秒前)。各 lead について
      - 順位相関(基準との一致)
      - 回帰の傾き(尺度の違い。絶対閾値をそのまま使えるか)
      - 閾値で選ぶ馬の重なり(Jaccard)
    をレース横断で集計する。ROI は測れない(必要な履歴が無い)ので、信号の保存度で判断する。
    """
    ref_cfg = FlowConfig(lead_seconds=ref_lead, flow_minutes=cfg.flow_minutes,
                         threshold=cfg.threshold, source=ref_source)
    out = []
    for lead in leads:
        c = FlowConfig(lead_seconds=lead, flow_minutes=cfg.flow_minutes,
                       threshold=cfg.threshold, source=cfg.source)
        rhos: list[float] = []
        xs: list[float] = []
        ys: list[float] = []
        inter = union = n_ref = n_cur = n_races = 0
        for race in races:
            ref = flow_scores(db, race, ref_cfg)
            cur = flow_scores(db, race, c)
            common = sorted(set(ref) & set(cur))
            if len(common) < 3:
                continue
            n_races += 1
            a = [ref[u]["score"] for u in common]
            b = [cur[u]["score"] for u in common]
            rho = _spearman(a, b)
            if rho is not None:
                rhos.append(rho)
            xs += a
            ys += b
            # ★同じ絶対閾値を両側に当てると、尺度が違う(傾き≠1)だけで重なりが
            #   潰れて「別物」に見える。選ぶ本数を揃えて比べるのが正しい比較。
            sa = {u for u in ref if ref[u]["score"] >= cfg.threshold}
            sb = set(sorted(cur, key=lambda u: -cur[u]["score"])[:len(sa)])
            n_ref += len(sa)
            n_cur += len(sb)
            inter += len(sa & sb)
            union += len(sa | sb)
        slope = None
        if len(xs) >= 3:
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            if sxx:
                slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        out.append({
            "lead": lead, "races": n_races,
            "rho": (sum(rhos) / len(rhos)) if rhos else None,
            "slope": slope,
            "n_ref": n_ref, "n_cur": n_cur,
            "jaccard": (inter / union) if union else None,
        })
    return out


def horse_gates(cfg: "FlowConfig", d: dict) -> list[dict]:
    """1頭ぶんの選別規則を、**判定に使った値ごと**に並べて返す。

    ★`eligible` はこれを使って絞る。判断の記録(bet_decision_logs)も同じものを
      読む。別々に書くと、記録の方だけが現実とずれて**嘘の根拠**が残る。
      「なぜ買った/買わなかった」を後から信じられる形にするのが目的なので、
      判定の実体と記録は必ず同じ関数から出す。
    """
    gates: list[dict] = []
    if cfg.max_odds > 0:
        fo = d.get("fuku_odds")
        gates.append({"rule": "fuku_odds<=max", "max": cfg.max_odds, "got": fo,
                      "pass": fo is not None and fo <= cfg.max_odds})
    if cfg.min_tan_odds > 0 or cfg.max_tan_odds > 0:
        gates.append({"rule": "tan_odds band", "min": cfg.min_tan_odds or None,
                      "max": cfg.max_tan_odds or None, "got": d.get("tan_odds"),
                      "pass": _in_tan_band(cfg, d.get("tan_odds"))})
    if cfg.min_ninki > 0 or cfg.max_ninki > 0:
        gates.append({"rule": "ninki band", "min": cfg.min_ninki or None,
                      "max": cfg.max_ninki or None, "got": d.get("ninki"),
                      "pass": _in_ninki_band(cfg, d.get("ninki"))})
    return gates


def eligible(cfg: "FlowConfig", sc: dict) -> dict:
    """閾値を当てる**前**の母集団。スコア以外の選別規則をすべて適用する。

    ★threshold_from(分位を取る側)と flow_orders(買う側)が**同じ集合**を見ること。
      分位を全馬で取って人気7+ だけに当てる、のような食い違いがあると
      「その部分集合の上位5%」という検証条件が再現されない。
      単勝×人気7+×上位5% は OOS 1.6690 だが、これは**人気7+ の中での上位5%**。
      全馬の95%点を人気7+ に当てると、実際の選別率は5%から大きくずれる。
    """
    if cfg.min_horses > 0 and len(sc) < cfg.min_horses:
        return {}
    return {um: d for um, d in sc.items()
            if all(g["pass"] for g in horse_gates(cfg, d))}


def threshold_from(db, races, cfg: FlowConfig, quantile: float = 0.95) -> dict:
    """手元のレース群から flow_tan の絶対閾値(上側分位)を求める。

    検証の選別規則は「fit 期間の分位で決めた絶対閾値」。決定時点や信号源を変えるとスコアの
    尺度が変わる(実測: 発走120秒前は60秒前の約0.49倍)ので、**使う設定と同じ条件で**
    取り直さないと、買う本数が想定から外れる。
    """
    vals: list[float] = []
    n_races = n_skewed = 0
    want = cfg.flow_minutes * 60 - cfg.lead_seconds
    for race in races:
        sc = flow_scores(db, race, cfg)
        if not sc:
            continue
        # ★実効窓がずれたレースは混ぜない。窓が長いほどスコアが大きく出るので、
        #   スナップが疎な日(ポーリングが遅い日)が混ざると閾値が水増しされ、
        #   窓が正しい日に当てたとき誰も超えなくなる(2026-09-22 に発生)。
        got = next(iter(sc.values())).get("window_sec")
        if got is not None and abs(got - want) > cfg.window_tolerance_sec:
            n_skewed += 1
            continue
        sc = eligible(cfg, sc)      # ★買う側と同じ母集団で分位を取る
        if not sc:
            continue
        n_races += 1
        vals += [d["score"] for d in sc.values()]
    if not vals:
        return {"threshold": None, "n": 0, "races": 0, "n_above": 0,
                "races_skewed_window": n_skewed}
    vals.sort()
    thr = vals[min(len(vals) - 1, int(len(vals) * quantile))]
    return {"threshold": thr, "n": len(vals), "races": n_races,
            "n_above": sum(1 for v in vals if v >= thr), "quantile": quantile,
            "races_skewed_window": n_skewed}



def _in_tan_band(cfg: "FlowConfig", tan: float | None) -> bool:
    """決定時点の単勝オッズが指定帯に入っているか。帯未指定なら常に True。"""
    if tan is None:
        return not (cfg.min_tan_odds > 0 or cfg.max_tan_odds > 0)
    if cfg.min_tan_odds > 0 and tan < cfg.min_tan_odds:
        return False
    return not (cfg.max_tan_odds > 0 and tan > cfg.max_tan_odds)




def assign_ninki(scores: dict[str, dict]) -> None:
    """決定時点の単勝オッズ順に 1,2,3... を振る(同値は馬番順)。

    ★DB の tan_ninki を使わない。信号源によって有無が違い(netkeiba は持たない)、
      そのままだとバックテストとライブで**別の定義**になる。
    """
    for rank, um in enumerate(
            sorted(scores, key=lambda u: (scores[u]["tan_odds"], u)), 1):
        scores[um]["ninki"] = rank


# バックテストの券種名(nl_hr の bet_type)→ hro_buyer の券種名
# ★馬単を追加(2026-10-06)。2窓OOSで 2.21/1.96 と単勝(1.67/1.69)を上回った。
#   軸=flow候補(人気7+)を**1着に固定**し、相手=人気上位N頭と組む。
_BET_TYPE = {"fuku": "place", "tan": "win", "umatan": "umatan"}

# 軸1頭では完結せず、相手との組が要る券種
_COMBO_BET_TYPES = {"umatan"}


def _in_ninki_band(cfg: "FlowConfig", ninki) -> bool:
    """決定時点の単勝人気が指定帯に入っているか。帯未指定なら常に True。"""
    if not (cfg.min_ninki > 0 or cfg.max_ninki > 0):
        return True
    n = (str(ninki) or "").strip()
    if not n.isdigit() or int(n) <= 0:
        return False            # 帯を指定したのに人気が取れない馬は買わない
    v = int(n)
    if cfg.min_ninki > 0 and v < cfg.min_ninki:
        return False
    return not (cfg.max_ninki > 0 and v > cfg.max_ninki)


def _combo_orders(BetOrder, race_id: str, axis: str, d: dict, mates: list[str],
                  cfg: "FlowConfig", amount: int, model_version: str,
                  thr: float, lead_used):
    """軸1頭ぶんの組み合わせ券を作る。**軸を1着に固定**する。

    ★selection_id は "軸-相手" の順序付き。並べ替えると別の馬券になる
      (hro_buyer 側は ORDERED_HR_TYPES で順序を保持して組番にする)。
    ★odds は組のオッズが手元に無いので 0.0。判断時の**軸の単勝オッズ**は
      reason に残す(後からオッズ帯でスライスするため)。
    """
    out = []
    for mate in mates:
        if mate == axis:
            continue
        out.append(BetOrder(
            race_id=race_id, selection_id=f"{axis}-{mate}",
            bet_type=_BET_TYPE[cfg.bet_type], amount=amount,
            probability=0.0, odds=0.0,
            expected_return=0.0, edge=0.0, kelly_fraction=0.0,
            model_version=model_version,
            reason=(f"flow_tan={d['score']:+.4f}>={thr:+.4f}@T-{lead_used}s "
                    f"axis={axis} mate={mate}(人気{cfg.partners}位以内) "
                    f"tan={d['tan_odds']:.1f} ninki={d.get('ninki')} "
                    f"ken={_BET_TYPE[cfg.bet_type]} "
                    f"late={_hhmmss(d['ts_late'])} early={_hhmmss(d['ts_early'])} "
                    f"src={cfg.source}")))
    return out


DECISION_SCHEMA = 1


def _race_gates(cfg: "FlowConfig", sc: dict, *, lead_used, window_sec,
                thr: float | None) -> list[dict]:
    """レース全体に効く規則(頭数・実効窓・閾値の有無)。"""
    want = cfg.flow_minutes * 60 - cfg.lead_seconds
    gates = [{"rule": "window", "want_sec": want, "got_sec": window_sec,
              "tol_sec": cfg.window_tolerance_sec,
              "pass": window_sec is None
                      or abs(window_sec - want) <= cfg.window_tolerance_sec}]
    if cfg.min_horses > 0:
        gates.append({"rule": "min_horses", "min": cfg.min_horses, "got": len(sc),
                      "pass": len(sc) >= cfg.min_horses})
    gates.append({"rule": "threshold_for_lead", "lead_sec": lead_used,
                  "table": sorted(cfg.thresholds or {}) or None, "got": thr,
                  "pass": thr is not None})
    return gates


def decision_payload(cfg: "FlowConfig", d: dict | None, *, lead_used, window_sec,
                     thr: float | None, race_gates: list[dict],
                     bet_type: str, amount: int, combo: dict | None = None) -> dict:
    """判断の根拠を**構造化して**残す(bet_decision_logs.constraints)。

    ★reason の文字列だけでは後から切れない。「人気帯で落ちた件数」「窓ずれで
      見送ったレース」を数えるのに正規表現を書く羽目になる。機械可読にしておく。
    ★設定(どのロジックか)と観測値(何を見たか)と判定(どの規則で決まったか)を
      分けて持つ。設定だけ分かっても、その日の値が無ければ再現できない。
    """
    gates = list(race_gates)
    if d is not None:
        gates += horse_gates(cfg, d)
        if thr is not None:
            gates.append({"rule": "flow_tan>=threshold", "threshold": thr,
                          "got": d["score"], "pass": d["score"] >= thr})
    out: dict = {
        "v": DECISION_SCHEMA,
        "strategy": "flow",
        "rule": "flow_tan >= threshold",
        "config": {
            "source": cfg.source, "lead_sec": cfg.lead_seconds,
            "flow_minutes": cfg.flow_minutes, "normalize": cfg.normalize,
            "bet_type": cfg.bet_type, "partners": cfg.partners,
            "min_ninki": cfg.min_ninki or None, "max_ninki": cfg.max_ninki or None,
            "min_horses": cfg.min_horses or None, "max_odds": cfg.max_odds or None,
            "min_tan_odds": cfg.min_tan_odds or None,
            "max_tan_odds": cfg.max_tan_odds or None,
            "max_per_race": cfg.max_per_race or None,
        },
        "threshold": {"value": thr, "lead_used_sec": lead_used,
                      "table": {str(k): v for k, v in (cfg.thresholds or {}).items()}
                                or None},
        "gates": gates,
        "bet": {"type": bet_type, "amount": amount},
    }
    if d is not None:
        out["signal"] = {
            "name": "flow_tan", "score": d["score"],
            "lead_late_sec": d.get("lead_late"), "lead_early_sec": d.get("lead_early"),
            "window_sec": window_sec,
            "ts_late": _hhmmss(d.get("ts_late")), "ts_early": _hhmmss(d.get("ts_early")),
        }
        out["market"] = {"tan_odds": d.get("tan_odds"), "fuku_odds": d.get("fuku_odds"),
                         "ninki": d.get("ninki")}
    if combo:
        out["combo"] = combo
    return out


def _dlog(race_id: str, selection_id: str, bet_type: str, *, accepted: bool,
          reason: str, odds: float, amount: int, payload: dict):
    from hro_optimizer.models import DecisionLog
    return DecisionLog(
        race_id=race_id, selection_id=selection_id, bet_type=bet_type,
        decision=("accepted" if accepted else "rejected"), reason=reason,
        probability=0.0, odds=odds or 0.0, expected_return=0.0, edge=0.0,
        recommended_amount=(amount if accepted else 0), constraints=payload)


def _failed(gates: list[dict]) -> str:
    """落ちた規則を1行で。reason 欄(人が読む側)に入れる。"""
    bad = [g for g in gates if not g["pass"]]
    return "; ".join(f"{g['rule']}({g.get('got')})" for g in bad) if bad else "all gates passed"


def flow_orders(db, race: tuple[str, ...], cfg: FlowConfig, amount: int, model_version: str,
                logs: list | None = None):
    """閾値を超えた馬の複勝 BetOrder を作る。モデルは使わない。

    ★logs を渡すと、**評価した全頭ぶん**の判断記録(DecisionLog)を積む。
      採用だけでなく却下も残すのは、「なぜこの馬だけ買ったのか」が却下側を
      見ないと分からないため。constraints に設定・観測値・各規則の判定が入る。
      リアルタイムの発注経路では使わない(呼び出し側が後で DB へ書く)。
    """
    from hro_moneymanager.models import BetOrder

    race_id = "".join(race)
    bt = _BET_TYPE[cfg.bet_type]
    sc = flow_scores(db, race, cfg)
    if not sc:
        log.info("%s: flow スコア算出不可(スナップショット不足)", race_id)
        if logs is not None:
            gates = [{"rule": "snapshots", "got": 0, "pass": False}]
            logs.append(_dlog(race_id, "*", bt, accepted=False,
                              reason="スナップショット不足でスコアを出せない",
                              odds=0.0, amount=amount,
                              payload=decision_payload(
                                  cfg, None, lead_used=None, window_sec=None,
                                  thr=None, race_gates=gates,
                                  bet_type=bt, amount=amount)))
        return []
    # ★実際に使ったスナップのリードで閾値を選ぶ。配信遅れでレースごとに T-120s に
    #   なったり T-180s になったりするため、固定の閾値だと片方で全く買わない。
    first = next(iter(sc.values()))
    lead_used = first.get("lead_late")
    # ★実効窓が意図とずれたレースは見送る。窓が違えばスコアの尺度が違い、
    #   閾値を当てても意味がない(格子の穴で起きる)。
    want = cfg.flow_minutes * 60 - cfg.lead_seconds
    got = first.get("window_sec")
    def _skip_race(thr_val, msg: str):
        """レース全体を見送るときに、理由を1行だけ記録する。"""
        if logs is not None:
            gates = _race_gates(cfg, sc, lead_used=lead_used, window_sec=got, thr=thr_val)
            logs.append(_dlog(race_id, "*", bt, accepted=False, reason=msg,
                              odds=0.0, amount=amount,
                              payload=decision_payload(
                                  cfg, None, lead_used=lead_used, window_sec=got,
                                  thr=thr_val, race_gates=gates,
                                  bet_type=bt, amount=amount)))
        return []

    if got is not None and abs(got - want) > cfg.window_tolerance_sec:
        log.warning("%s: 実効窓 %ds が想定 %ds から外れているため見送り"
                    "(決定 T-%ss / 起点 T-%ss)",
                    race_id, got, want, lead_used, first.get("lead_early"))
        return _skip_race(None, f"実効窓 {got}s が想定 {want}s から外れている")
    # ★複勝の払戻対象頭数はレースの出走頭数で変わる(8頭以上=3着まで / 5〜7頭=2着まで /
    #   4頭以下=発売なし)。2着までのレースを混ぜると条件が揃わない。
    if cfg.min_horses > 0 and len(sc) < cfg.min_horses:
        log.info("%s: 出走 %d 頭は下限 %d 頭未満のため見送り(複勝の払戻対象が変わる)",
                 race_id, len(sc), cfg.min_horses)
        return _skip_race(None, f"出走 {len(sc)} 頭が下限 {cfg.min_horses} 頭未満")
    thr = threshold_for(cfg, lead_used)
    if thr is None:
        log.warning("%s: 実測リード T-%ss の閾値が未設定のため見送り(設定: %s)",
                    race_id, lead_used, sorted(cfg.thresholds or {}))
        return _skip_race(None, f"実測リード T-{lead_used}s の閾値が未設定")
    # ★組み合わせ券は「軸 × 相手」。相手は**人気上位N頭**(flow 上位ではない)。
    #   軸に要るのは「市場が間違っている馬」=変化量だが、相手に要るのは
    #   「2着に来る素の確率」=水準で、それを最もよく表すのが人気。
    #   2窓OOS: 相手=人気 2.2756/2.0645 対 相手=flow 1.7545/1.2770(的中率が半減)。
    combo = cfg.bet_type in _COMBO_BET_TYPES
    mates: list[str] = []
    if combo:
        mates = [u for u, _d in sorted(sc.items(), key=lambda kv: kv[1]["ninki"])
                 if sc[u]["ninki"] <= cfg.partners]

    rgates = _race_gates(cfg, sc, lead_used=lead_used, window_sec=got, thr=thr)

    def _log_horse(um: str, d: dict, *, accepted: bool, odds: float,
                   combo_info: dict | None = None, note: str = "") -> None:
        """1頭ぶんの判断を記録する。**却下も残す**。

        ★「なぜこの馬だけ買ったのか」は、買わなかった馬の理由が無いと答えられない。
        """
        if logs is None:
            return
        payload = decision_payload(cfg, d, lead_used=lead_used, window_sec=got,
                                   thr=thr, race_gates=rgates, bet_type=bt,
                                   amount=amount, combo=combo_info)
        reason = note or _failed(payload["gates"])
        sel = f"{um}-{combo_info['mate']}" if combo_info else um
        logs.append(_dlog(race_id, sel, bt, accepted=accepted, reason=reason,
                          odds=odds, amount=amount, payload=payload))

    picked = eligible(cfg, sc)
    if logs is not None:
        # ★母集団から落ちた馬(オッズ帯・人気帯)を先に記録する。閾値の手前で
        #   消えているので、下のループには現れない。
        for um, d in sc.items():
            if um not in picked:
                _log_horse(um, d, accepted=False, odds=d.get("tan_odds") or 0.0)

    orders = []
    for um, d in sorted(picked.items(), key=lambda kv: -kv[1]["score"]):
        if d["score"] < thr:
            _log_horse(um, d, accepted=False, odds=d.get("tan_odds") or 0.0)
            continue
        if combo:
            for mate in mates:
                if mate != um:
                    _log_horse(um, d, accepted=True, odds=d.get("tan_odds") or 0.0,
                               combo_info={"axis": um, "mate": mate,
                                           "mate_ninki": sc[mate].get("ninki"),
                                           "partners": cfg.partners,
                                           "mate_rule": "人気上位N頭(flow 上位ではない)"})
            orders += _combo_orders(BetOrder, race_id, um, d, mates, cfg,
                                    amount, model_version, thr, lead_used)
            continue
        orders.append(BetOrder(
            # ★券種は設定から取る。単勝×人気7+×上位5% が OOS で 1.6690(P=0.007)と
            #   複勝(1.0768)を大きく上回ったため可変にした。既定は従来どおり複勝。
            race_id=race_id, selection_id=um, bet_type=_BET_TYPE[cfg.bet_type],
            amount=amount,
            probability=0.0,                 # flow は確率を推定しない(順位/閾値で選ぶ)
            # ★買う券種のオッズを入れる。単勝を買うのに複勝オッズを記録すると、
            #   後からオッズ帯で回収率をスライスできない(帯で大きく違う)。
            odds=(d["tan_odds"] if cfg.bet_type == "tan" else d["fuku_odds"]) or 0.0,
            expected_return=0.0, edge=0.0, kelly_fraction=0.0,
            model_version=model_version,
            # ★決定時点の**単勝**オッズを残す。回収率がオッズ帯で大きく違う
            #   (20-40倍 1.45 / 全帯 1.08)ので、後から帯別に評価できないと
            #   ライブの結果を検証できない。odds 欄は発注する複勝の値なので別に持つ。
            # ★fuku_odds は単勝のとき None でありうる(速報ポーリングが止まっていても
            #   単勝は買えるようにしたため)。整形で落とさない。
            reason=(f"flow_tan={d['score']:+.4f}>={thr:+.4f}@T-{lead_used}s "
                    f"tan={d['tan_odds']:.1f} "
                    f"fuku={'-' if d['fuku_odds'] is None else format(d['fuku_odds'], '.1f')} "
                    f"ninki={d.get('ninki')} ken={_BET_TYPE[cfg.bet_type]} "
                    f"late={_hhmmss(d['ts_late'])} early={_hhmmss(d['ts_early'])} "
                    f"src={cfg.source}"),
        ))
        _log_horse(um, d, accepted=True,
                   odds=(d["tan_odds"] if cfg.bet_type == "tan" else d["fuku_odds"]) or 0.0)
    if cfg.max_per_race > 0 and len(orders) > cfg.max_per_race:
        log.info("%s: 候補 %d 頭のうち上位 %d 頭に絞ります"
                 "(順次処理では2件目以降が締切を超えるため)",
                 race_id, len(orders), cfg.max_per_race)
        dropped = {o.selection_id for o in orders[cfg.max_per_race:]}
        orders = orders[:cfg.max_per_race]       # flow_orders はスコア降順に作っている
        if logs is not None:
            # ★max_per_race で落ちた馬は「規則としては通ったが枠が無かった」。
            #   規則落ちと混ぜると、閾値の効き具合を見誤る。
            for lg in logs:
                if lg.decision == "accepted" and lg.selection_id in dropped:
                    lg.decision = "rejected"
                    lg.recommended_amount = 0
                    lg.reason = f"max_per_race={cfg.max_per_race} の枠外(規則は通過)"
                    lg.constraints["gates"].append(
                        {"rule": "max_per_race", "max": cfg.max_per_race,
                         "got": len(dropped) + cfg.max_per_race, "pass": False})
    log.info("%s: flow 候補 %d/%d 頭 (閾値 %+.4f, 実測 T-%ss, src=%s)",
             race_id, len(orders), len(sc), thr, lead_used, cfg.source)
    return orders


# --- バックテスト: 期間まるごとを1クエリで引く(レースごとに往復すると遅すぎる) ---
# 候補CSVもモデルも要らない。flow は model-free なので、スナップ2本と払戻だけで完結する。
# ★_SQL_BT と _SQL_BT_NK は **末尾(SELECT と JOIN)を共有する**。以前は NK 側が
#   コピーだったため、_SQL_BT への修正が片方にしか当たらず腐った
#   (2026-10-01: NK 側だけ `LEFT JOIN fk USING` が残り、nl_se の year と衝突して
#    AmbiguousColumn。作った時から壊れていて、一度も走らせていなかったので露見しなかった)。
_BT_TAIL = """SELECT l.year||l.month_day||l.jyo_cd||l.kaiji||l.nichiji||l.race_num AS rid,
       l.year||l.month_day AS ymd, l.umaban,
       l.t1, {F1}, e.t0, l.ht1, e.ht0, l.lead1, h.pay, se.i_jyo_cd,
       -- スライス用(すべて既に JOIN 済みのテーブルから取るので追加コストは小さい)
       se.wakuban, se.zogen_fugo, se.zogen_sa, se.ba_taijyu,
       ra2.kyori, ra2.track_cd, ra2.grade_cd,
       -- ★公表イベント。終盤の資金移動の一部は「情報を持った金」ではなく
       --   **公開ニュースへの反応**かもしれない。雑音なら除外、織り込み不足なら
       --   むしろ狙い目、と行動が変わるので測れるようにする。
       EXISTS (SELECT 1 FROM nl_jc jc
                WHERE (jc.year,jc.month_day,jc.jyo_cd,jc.kaiji,jc.nichiji,jc.race_num,
                       jc.umaban)
                    = (l.year,l.month_day,l.jyo_cd,l.kaiji,l.nichiji,l.race_num,
                       l.umaban)) AS has_jc,
       EXISTS (SELECT 1 FROM nl_cc cc
                WHERE (cc.year,cc.month_day,cc.jyo_cd,cc.kaiji,cc.nichiji,cc.race_num)
                    = (l.year,l.month_day,l.jyo_cd,l.kaiji,l.nichiji,l.race_num)) AS has_cc,
       -- ★天候/馬場の変更は**開催単位**(race_num を持たない)
       hc.haron_time_4f AS chokyo_4f, hc.chokyo_date,
       EXISTS (SELECT 1 FROM nl_we we
                WHERE (we.year,we.month_day,we.jyo_cd,we.kaiji,we.nichiji)
                    = (l.year,l.month_day,l.jyo_cd,l.kaiji,l.nichiji)) AS has_we,
       -- ★そのレースの複勝払戻が1行でも存在するか。無い=まだ結果が入っていない。
       --   これを見ないと「未確定」を「全部外れ」として数えてしまう。
       EXISTS (SELECT 1 FROM nl_hr h2
                WHERE (h2.year,h2.month_day,h2.jyo_cd,h2.kaiji,h2.nichiji,h2.race_num)
                    = (l.year,l.month_day,l.jyo_cd,l.kaiji,l.nichiji,l.race_num)
                  AND h2.bet_type = {BET}) AS has_payout
FROM late l
JOIN early e USING (year,month_day,jyo_cd,kaiji,nichiji,race_num,umaban)
JOIN ra ra2 USING (year,month_day,jyo_cd,kaiji,nichiji,race_num)
-- ★異常区分。出走取消/発走除外/競走除外 は**返還**であって外れではない。
--   払戻表(nl_hr)には行が立たないので、これを見ないと全損として数えてしまう。
LEFT JOIN nl_se se
  ON (se.year,se.month_day,se.jyo_cd,se.kaiji,se.nichiji,se.race_num,se.umaban)
   = (l.year,l.month_day,l.jyo_cd,l.kaiji,l.nichiji,l.race_num,l.umaban)
-- ★坂路調教。市場とは独立した唯一の大規模データ(5,560,062行)。
--   その馬の**レース前で最も新しい**追い切りを1本だけ取る。
--   索引 idx_nl_hc_horse_date (ketto_num, chokyo_date DESC) が無いと全走査になる。
--   ★美浦/栗東の坂路のみ。ウッドチップだけの馬や外国馬は行が無い(= データなしも情報)。
LEFT JOIN LATERAL (
  SELECT hc0.haron_time_4f, hc0.chokyo_date
  FROM nl_hc hc0
  WHERE hc0.ketto_num = se.ketto_num
    AND hc0.chokyo_date < l.year||l.month_day
    AND hc0.haron_time_4f ~ '^[0-9]+$' AND hc0.haron_time_4f::int > 0
  ORDER BY hc0.chokyo_date DESC, hc0.chokyo_time DESC
  LIMIT 1) hc ON true
LEFT JOIN nl_hr h
  ON (h.year,h.month_day,h.jyo_cd,h.kaiji,h.nichiji,h.race_num)
   = (l.year,l.month_day,l.jyo_cd,l.kaiji,l.nichiji,l.race_num)
 AND h.bet_type = {BET}
 AND regexp_replace(h.kumi,'[^0-9]','','g') = l.umaban
{EXTRA_JOIN}"""

_SQL_BT = """
WITH ra AS (
  SELECT year, month_day, jyo_cd, kaiji, nichiji, race_num,
         kyori, track_cd, grade_cd, syusso_tosu,
         to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp AS post,
         to_char(to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
                 - make_interval(secs => %(lead)s), 'MMDDHH24MI') AS cut_late,
         to_char(to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
                 - make_interval(mins => %(flow)s), 'MMDDHH24MI') AS cut_early
  FROM nl_ra
  WHERE jyo_cd BETWEEN '01' AND '10'            -- ★JRA のみ(地方/海外を混ぜない)
    AND year||month_day BETWEEN %(d0)s AND %(d1)s
    AND hasso_time ~ '^[0-9]{4}$'
),
-- ★ORDER BY は **hasso_time(MMDDHHMI の文字列)**で行う。ここを計算した timestamp に
--   すると主キー (…,race_num,umaban,hasso_time,source_spec) の索引が使えず、全期間の
--   スナップ数百万行を2回ソートすることになって終わらない。文字列順=時刻順なので
--   索引のまま最新1本が取れる(年跨ぎのみ順序が崩れるが、決定時点は発走の数分前で
--   同日のスナップを指すため実害は無い。取れなければ races_scored に出ない)。
late AS (
  SELECT DISTINCT ON (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban)
         t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
         t.tan_odds AS t1, t.fuku_odds_low AS f1, t.hasso_time AS ht1,
         EXTRACT(EPOCH FROM (ra.post
           - to_timestamp(t.year||t.hasso_time,'YYYYMMDDHH24MI')::timestamp))::int AS lead1
  FROM {TABLE} t JOIN ra USING (year,month_day,jyo_cd,kaiji,nichiji,race_num)
  WHERE t.hasso_time <= ra.cut_late
  ORDER BY t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
           t.hasso_time DESC
),
early AS (
  SELECT DISTINCT ON (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban)
         t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
         t.tan_odds AS t0, t.hasso_time AS ht0
  FROM {TABLE} t JOIN ra USING (year,month_day,jyo_cd,kaiji,nichiji,race_num)
  WHERE t.hasso_time <= ra.cut_early
  ORDER BY t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
           t.hasso_time DESC
)
""" + _BT_TAIL.replace("{F1}", "l.f1").replace("{EXTRA_JOIN}", "")

# netkeiba は複勝を持たないので JV(ts_sokuho_o1)の直近値を fk CTE から添える。
# ★USING ではなく ON で結合する(左側に nl_se の同名列があり USING は曖昧になる)。
_SQL_BT_NK = """
WITH ra AS (
  -- ★共有する末尾(_BT_TAIL)が ra2.kyori 等を参照する。列を _SQL_BT と揃えること。
  SELECT year, month_day, jyo_cd, kaiji, nichiji, race_num,
         kyori, track_cd, grade_cd, syusso_tosu,
         (to_timestamp(year||month_day||hasso_time,'YYYYMMDDHH24MI')::timestamp
          AT TIME ZONE 'Asia/Tokyo') AS post
  FROM nl_ra
  WHERE jyo_cd BETWEEN '01' AND '10'            -- ★JRA のみ(地方/海外を混ぜない)
    AND year||month_day BETWEEN %(d0)s AND %(d1)s
    AND hasso_time ~ '^[0-9]{4}$'
),
-- 複勝は netkeiba に無いので JV 側の直近値を添える(上限フィルタと明細表示用)
fk AS (
  SELECT DISTINCT ON (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban)
         t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
         t.fuku_odds_low AS f1
  FROM ts_sokuho_o1 t JOIN ra USING (year,month_day,jyo_cd,kaiji,nichiji,race_num)
  WHERE t.hasso_time ~ '^[0-9]{8}$'
  ORDER BY t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
           t.hasso_time DESC
),
late AS (
  SELECT DISTINCT ON (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban)
         t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
         t.tan_odds AS t1, t.observed_at AS ht1,
         EXTRACT(EPOCH FROM (ra.post - t.observed_at))::int AS lead1
  FROM ts_netkeiba_o1 t JOIN ra USING (year,month_day,jyo_cd,kaiji,nichiji,race_num)
  WHERE t.observed_at <= ra.post - make_interval(secs => %(lead)s)
    AND t.tan_odds IS NOT NULL AND t.tan_odds > 0
  ORDER BY t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
           t.observed_at DESC
),
early AS (
  SELECT DISTINCT ON (t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban)
         t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
         t.tan_odds AS t0, t.observed_at AS ht0
  FROM ts_netkeiba_o1 t JOIN ra USING (year,month_day,jyo_cd,kaiji,nichiji,race_num)
  WHERE t.observed_at <= ra.post - make_interval(mins => %(flow)s)
    AND t.tan_odds IS NOT NULL AND t.tan_odds > 0
  ORDER BY t.year,t.month_day,t.jyo_cd,t.kaiji,t.nichiji,t.race_num,t.umaban,
           t.observed_at DESC
)
""" + _BT_TAIL.replace("{F1}", "fk.f1").replace(
    "{EXTRA_JOIN}",
    "LEFT JOIN fk\n"
    "  ON (fk.year,fk.month_day,fk.jyo_cd,fk.kaiji,fk.nichiji,fk.race_num,fk.umaban)\n"
    "   = (l.year,l.month_day,l.jyo_cd,l.kaiji,l.nichiji,l.race_num,l.umaban)")




# ★netkeiba 用。時間軸が **observed_at(実時刻)** なので、分格子前提の _SQL_BT は使えない。
#   ここを分けずに {TABLE} を差し替えるだけにすると、netkeiba を指定したのに
#   ts_sokuho_o1 を読んで「別ソースの数字を netkeiba の成績として報告する」ことになる。



def backtest(db, d_from: str, d_to: str, cfg: FlowConfig, *,
             max_odds: float | None = None, amount: int = 100,
             with_ci: bool = True) -> dict:
    """flow_tan(複勝)の期間回収率。モデルも候補CSVも使わない。

    ★払戻は nl_hr の確定複勝(pay=100円あたり)。パリミュチュエルなので判断時の
    オッズでは払われない。判断に使うのはスナップのオッズ、決済は必ず確定払戻。
    レース単位でブートストラップして CI を出す(同一レース内の馬は独立でない)。
    """
    if cfg.bet_type not in ("fuku", "tan"):
        raise ValueError(f"不明な券種: {cfg.bet_type!r} (fuku|tan)")
    if cfg.source == "netkeiba":
        sql = _SQL_BT_NK
    elif cfg.source in ("ts", "sokuho"):
        sql = _SQL_BT.replace("{TABLE}", "ts_o1" if cfg.source == "ts" else "ts_sokuho_o1")
    else:
        raise ValueError(f"不明な flow 信号源: {cfg.source!r}")
    sql = sql.replace("{BET}", f"'{cfg.bet_type}'")
    tan_of = _tan_reader(cfg.source)
    rows = db.query(sql, {"d0": d_from, "d1": d_to,
                          "lead": cfg.lead_seconds, "flow": cfg.flow_minutes})

    by_race: dict[str, list[dict]] = {}
    for r in rows:
        by_race.setdefault(r["rid"], []).append(r)

    bets: list[tuple] = []                    # (rid, 賭け金, 払戻, 返還フラグ)
    details: list[dict] = []                  # 1点ずつの明細(目視確認用)
    n_races = n_scored = n_degenerate = n_refund = n_unsettled = n_small = 0
    for rid, rs in by_race.items():
        n_races += 1
        if rs[0]["ht1"] is not None and rs[0]["ht1"] == rs[0]["ht0"]:
            n_degenerate += 1                   # 決定時点と起点が同じスナップ=測れていない
            continue
        s1 = sum(1.0 / o for x in rs if (o := tan_of(x["t1"])))
        s0 = sum(1.0 / o for x in rs if (o := tan_of(x["t0"])))
        if s1 <= 0 or s0 <= 0:
            continue
        # ★払戻が1行も無いレースは「未確定」。外れとして数えると回収率が0に張り付く。
        if not rs[0].get("has_payout", True):
            n_unsettled += 1
            continue
        if cfg.min_horses > 0 and len(rs) < cfg.min_horses:
            n_small += 1            # 複勝の払戻対象頭数が違うレースは混ぜない
            continue
        n_scored += 1
        # ★そのレースで市場がどれだけ動いたか(全頭のシェア変化の総量)。
        #   ほとんど動いていないレースの flow は雑音のはずで、大きく動いたレースなら
        #   情報が入っている可能性が高い。「いつ効くのか」を切る軸として使う。
        #   総変動 = Σ|share_late - share_early|(0〜2 の範囲)。
        race_move = 0.0
        for _x in rs:
            _t1, _t0 = tan_of(_x["t1"]), tan_of(_x["t0"])
            if _t1 and _t0:
                race_move += abs((1.0 / _t1) / s1 - (1.0 / _t0) / s0)
        # ★坂路4Fの**レース内順位**(速い順)。時計の絶対値は時期・馬場で動くので、
        #   同一レース内での相対化が最も素直。データが無い馬は順位を付けない。
        def _4f(x):
            v = (str(x.get("chokyo_4f") or "")).strip()
            return int(v) if v.isdigit() and int(v) > 0 else None

        _worked = [x for x in rs if _4f(x) is not None]
        chokyo_rank = {x["umaban"]: i for i, x in enumerate(
            sorted(_worked, key=lambda r: (_4f(r), r["umaban"])), 1)}
        # ★人気は決定時点の単勝オッズ順から導出(live の flow_scores と同じ定義)
        ninki_of = {r["umaban"]: i for i, r in enumerate(
            sorted((r for r in rs if tan_of(r["t1"])),
                   key=lambda r: (tan_of(r["t1"]), r["umaban"])), 1)}
        for x in rs:
            # ★複勝(f1)は JV 由来なので常に _num。単勝だけソース別に読む
            t1, t0, f1 = tan_of(x["t1"]), tan_of(x["t0"]), _num(x["f1"])
            if t1 is None or t0 is None or f1 is None:
                continue
            score = _logit((1.0 / t1) / s1) - _logit((1.0 / t0) / s0)
            if cfg.normalize:
                score = (score / race_move) if race_move > 1e-9 else 0.0
            if score < cfg.threshold:
                continue
            if max_odds is not None and t1 > max_odds:
                continue
            if not _in_tan_band(cfg, t1):
                continue
            if not _in_ninki_band(cfg, ninki_of.get(x["umaban"])):
                continue
            # 異常区分 1=出走取消 2=発走除外 3=競走除外 は返還(元金が戻る)。
            # 4=競走中止 5=失格 は出走しているので外れ扱いのままでよい。
            lead = x.get("lead1")
            if str(x.get("i_jyo_cd") or "").strip() in ("1", "2", "3"):
                n_refund += 1
                bets.append((rid, amount, amount, True))     # 返還: 元金が戻る
                details.append({"rid": rid, "umaban": x["umaban"], "score": score,
                                "lead": lead, "tan": t1, "fuku": f1, "n_horses": len(rs), "race_move": race_move,
                                "ninki": ninki_of.get(x["umaban"]), "waku": x.get("wakuban"),
                                "zogen_fugo": x.get("zogen_fugo"),
                                "zogen_sa": x.get("zogen_sa"),
                                "kyori": x.get("kyori"), "track_cd": x.get("track_cd"),
                                "grade_cd": x.get("grade_cd"),
                                "has_jc": x.get("has_jc"), "has_cc": x.get("has_cc"),
                                "has_we": x.get("has_we"),
                                "chokyo_rank": chokyo_rank.get(x["umaban"]),
                                "chokyo_n": len(chokyo_rank),
                                "amount": amount, "payout": amount, "note": "返還"})
                continue
            pay = x["pay"]
            payout = int(round(int(pay) * amount / 100)) if pay not in (None, "") else 0
            bets.append((rid, amount, payout, False))
            details.append({"rid": rid, "umaban": x["umaban"], "score": score,
                            "lead": lead, "tan": t1, "fuku": f1, "n_horses": len(rs), "race_move": race_move,
                            "ninki": ninki_of.get(x["umaban"]), "waku": x.get("wakuban"),
                            "zogen_fugo": x.get("zogen_fugo"), "zogen_sa": x.get("zogen_sa"),
                            "kyori": x.get("kyori"), "track_cd": x.get("track_cd"),
                            "grade_cd": x.get("grade_cd"),
                            "has_jc": x.get("has_jc"), "has_cc": x.get("has_cc"),
                            "has_we": x.get("has_we"),
                            "chokyo_rank": chokyo_rank.get(x["umaban"]),
                            "chokyo_n": len(chokyo_rank),
                            "amount": amount, "payout": payout,
                            "note": "的中" if payout else "外れ"})

    rep = summarize_bets(bets, races=n_races, races_scored=n_scored,
                         races_degenerate=n_degenerate, with_ci=with_ci)
    rep["details"] = details
    rep["races_unsettled"] = n_unsettled
    rep["races_small_field"] = n_small
    return rep



def summarize_bets(bets, *, races: int = 0, races_scored: int = 0,
                   races_degenerate: int = 0, with_ci: bool = True) -> dict:
    """購入明細を集計する。期間を分割して回したときに合算できるよう切り出してある。"""
    staked = sum(b[1] for b in bets)
    returned = sum(b[2] for b in bets)
    # 返還は的中ではない。払戻>0 で数えると的中率が水増しされる
    # (複勝の最低払戻は1.0倍=元金と同額なので、金額では返還と区別できない)。
    refunds = sum(1 for b in bets if len(b) > 3 and b[3])
    live = [b for b in bets if not (len(b) > 3 and b[3])]
    hits = sum(1 for b in live if b[2] > 0)
    return {
        "races": races, "races_scored": races_scored, "races_degenerate": races_degenerate,
        "bets": len(bets), "staked": staked, "returned": returned, "hits": hits,
        "refunds": refunds,
        "roi": (returned / staked) if staked else None,
        "hit_rate": (hits / len(live)) if live else None,
        "ci": _bootstrap_roi(bets) if with_ci else None,
        "_bets": bets,
    }


def _bootstrap_roi(bets, n_boot: int = 2000, seed: int = 20260921):
    """レース単位の復元抽出で回収率の95%CIと P(ROI<=1)。

    ★馬単位でブートストラップすると同一レース内の相関を無視して CI が狭く出る。
    賭けたのはレースなので、レースごと丸ごと抜き差しする。
    """
    import random
    if not bets:
        return None
    per_race: dict[str, list[tuple[int, int]]] = {}
    for b in bets:                      # (rid, 賭け金, 払戻[, 返還フラグ])
        per_race.setdefault(b[0], []).append((b[1], b[2]))
    races = list(per_race.values())
    rnd = random.Random(seed)
    rois = []
    n = len(races)
    for _ in range(n_boot):
        st = rt = 0
        for _ in range(n):
            for amt, pay in races[rnd.randrange(n)]:
                st += amt
                rt += pay
        if st:
            rois.append(rt / st)
    if not rois:
        return None
    rois.sort()
    return {"lo": rois[int(0.025 * len(rois))], "hi": rois[int(0.975 * len(rois))],
            "p_le_1": sum(1 for r in rois if r <= 1.0) / len(rois)}


# --- netkeiba と JV-Link(速報)の突き合わせ -------------------------------- #
# ★秒単位に見える動きが「本物の票数由来」か「公式の分更新を補間しているだけ」かを
#   判定する。補間なら分境界の間を単調に移動するだけで、新しい情報は無い。
#   同じ発表時刻の値が一致するか / 分の途中に公式には無い値が出るか、で見分ける。
_SQL_NK_COMPARE = """
WITH nk AS (
  SELECT jyo_cd, race_num, umaban, observed_at, tan_odds,
         to_char(observed_at AT TIME ZONE 'Asia/Tokyo', 'MMDDHH24MI') AS minute_key
  FROM ts_netkeiba_o1
  WHERE year = %(y)s AND month_day = %(m)s
),
jv AS (
  SELECT jyo_cd, race_num, umaban, hasso_time AS minute_key,
         tan_odds::numeric / 10.0 AS jv_odds
  FROM ts_sokuho_o1
  WHERE year = %(y)s AND month_day = %(m)s
    AND tan_odds ~ '^[0-9]+$' AND tan_odds::numeric > 0
)
-- ★列名は Python 側が引くキーと一致させる(英語)。表示の日本語は CLI 側で付ける。
-- ★「netkeiba が分内で動いたか」は **馬ごとに値が2つ以上あるか** で判定する。
--   全馬を通じた distinct 値数で見ると頭数とほぼ同じ値になり、常に「動いた」に見える
--   (2026-09-26 に誤読した)。nk_pairs > horses が真の判定。
SELECT nk.jyo_cd, nk.race_num, nk.minute_key,
       count(*) AS n,
       count(DISTINCT nk.observed_at) AS nk_snaps,
       count(DISTINCT nk.umaban) AS horses,
       count(DISTINCT (nk.umaban, nk.tan_odds)) AS nk_pairs,
       count(DISTINCT jv.jv_odds) AS jv_values,
       count(*) FILTER (WHERE abs(nk.tan_odds - jv.jv_odds) < 0.051) AS agree,
       min(nk.observed_at AT TIME ZONE 'Asia/Tokyo')::time(0) AS first_at,
       max(nk.observed_at AT TIME ZONE 'Asia/Tokyo')::time(0) AS last_at
FROM nk JOIN jv
  ON (jv.jyo_cd, jv.race_num, jv.umaban, jv.minute_key)
   = (nk.jyo_cd, nk.race_num, nk.umaban, nk.minute_key)
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""


def netkeiba_compare(db, date: str) -> list[dict]:
    """同じ「発表分」の中で netkeiba と公式速報を突き合わせる。

    読み方:
      - 一致 / 突合数 が高い          → 同じプールを見ている(信用できる)
      - nk_snaps が 2 以上            → 1分に2点以上ある(公式より細かい)
      - nk_pairs > horses             → **馬ごとに値が動いている**(本当に細かい)
      - 一致率は netkeiba が1分に2点あると上限が約50%になる。低い=外れ、ではない
    """
    cols = ["jyo_cd", "race_num", "minute_key", "n", "nk_snaps", "horses",
            "nk_pairs", "jv_values", "agree", "first_at", "last_at"]
    rows = db.query(_SQL_NK_COMPARE, {"y": date[:4], "m": date[4:8]})
    if rows and isinstance(rows[0], dict):
        return rows
    return [dict(zip(cols, r)) for r in rows]

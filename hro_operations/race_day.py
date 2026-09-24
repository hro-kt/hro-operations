"""開催日ランナー(Linux 側)。

当日の JRA レースを nl_ra から取得し、各レースの発走時刻 − lead 秒(既定 T-30s)に
    build_race_features → 能力値(win/place) → PL 候補 → フラット¥100 選別 (hro-backtest.harness)
    → paper 購入(hro-buyer: 締切/発売可否/実行直前ガード)
を実行して結果 JSONL に追記する。**実投票はしない(paper)**。

検証済みのフラット複勝戦略(min_er>=1.3 & min_prob>=0.15, ¥100)をそのまま前向きに回すのが目的。
モデルは学習時と同じ特徴スキーマ(例: HRO_ABLATE_SED=1 の no-SED 279)である必要がある
(harness.load_models が schema hash 不一致なら fail-fast)。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from hro_features.config import load_config as load_features_config
from hro_features.db import FeatureDB
from hro_optimizer.config import BettingConfig, KellyConfig, SimConfig
from hro_optimizer.db import connect as opt_connect
from hro_moneymanager.config import MoneyManagerConfig

# ★hro_backtest は LightGBM(hro-predictor)を引き込む重い依存。flow 戦略はモデルを
#   一切使わないので、モジュール冒頭で読むとモデル用の一式が無い機械(IPAT を叩く
#   Windows 等)で ImportError になり、model-free のはずの戦略が動かせない。
#   実際に使うモデル分岐の中で遅延 import する。

from hro_buyer.config import BuyerConfig
from hro_buyer.models import COMMITTED_STATUSES, MODE_LIVE, MODE_PAPER
from hro_buyer.postgres import (
    JST,
    PostgresConfig,
    PostgresDeadlineProvider,
    PostgresResultSink,
    PostgresSaleProvider,
    deadline_from,
)
from hro_buyer.service import BuyerService
from hro_buyer.sinks import JsonlResultSink
from hro_buyer.sources import InMemoryOrderSource

log = logging.getLogger("hro_operations")

_JRA_JYO = ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10")


@dataclass
class DayConfig:
    """開催日ランナーの設定。"""

    date: str  # YYYYMMDD (JST)
    win_model: str
    place_model: str
    results_path: str
    min_er: float = 1.3
    min_prob: float = 0.15
    flat_amount: int = 100
    max_odds_age: float = 60.0  # live 鮮度上限(秒)。poll-odds は10s周期なので余裕
    lead_seconds: int = 30  # 発走時刻 − これ秒 に発注(T-30s)
    grace_seconds: int = 180  # 締切をこれ秒超過していたら見送り(遅延起動の取りこぼし防止)
    # ★発売締切は発走時刻ちょうどではない。IPAT の画面実測では **発走の1分前**
    #   (中山10R 発走15:05 / 締切15:04、阪神10R 発走14:50 / 締切14:49)。
    #   ここを0にすると「まだ買える」と誤判定して締切後に投票しようとする。
    deadline_lead_seconds: int = 60
    # 判断の何秒前に画面をレース/式別まで進めておくか(preselect)。締切直前に残す
    # 作業を馬番と金額だけにするための前倒し。live 以外では何も起きない。
    preselect_lead_seconds: int = 120
    mode: str = MODE_PAPER
    bet_types: tuple[str, ...] = ("place",)
    source: str = "live"  # live=ts_sokuho(本番) / confirmed=nl_o*(過去レースでの検証用)
    max_odds: float = 50.0  # オッズ上限。★trioは配当が数十〜数千倍なので 50 だと全弾き→trioは2000等に上げる
    # --- trio運用(er_cal帯選別・較正・分数Kelly) ---
    max_er: float | None = None       # 期待値の上限(帯選別)。trioは [min_er, max_er)=例[1.7,2.0)
    calib_path: str | None = None     # trio較正JSON(fit-trio-calib出力)。EV計算前に組合せ確率へ適用
    simultaneous: bool = False        # True=レース内joint Kelly(trioは脚が相関するので推奨)
    bankroll: int = 0                 # >0 で分数Kelly(0=flat_amountのまま)
    kelly_fraction: float = 0.25      # フラクショナルケリー係数(裾が重いので保守的に1/4)
    daily_budget: int = 20_000        # 1日購入上限(Kelly時)
    race_max_amount: int = 10_000     # 1レース購入上限(Kelly時)
    ticket_min_amount: int = 100      # 1点最低額
    ticket_max_amount: int = 5_000    # 1点最大額
    max_tickets_per_race: int = 3     # 1レース最大点数
    ev_lcb_z: float = 0.0             # EV下側信頼限界のz(0=従来)。MC二項SEでpを保守化=勝者の呪い対策
    # flow シグナル運用(モデル非使用)。strategy="flow" で有効化。
    strategy: str = "model"          # model | flow
    flow_threshold: float = 0.0                          # fit 期間の分位から決めた絶対閾値
    # ★リード別の閾値。配信遅れで決定時点がレースごとに変わる(2026-09-21 実測:
    #   T-120s が41%、残りは T-180s)。スコアの尺度もリードで変わるので、単一の閾値だと
    #   片方でほぼ0件になる。指定すると実測リードに対応する値で判定する。
    flow_thresholds: dict[int, float] | None = None
    flow_lead_seconds: int = 60      # 決定時点 = 発走 − これ秒(0B41 の格子は T−60s)
    flow_minutes: int = 6            # フロー起点 = 発走 − これ分
    # ts(公式時系列 0B41) | sokuho(自前ポーリング 0B30) | netkeiba(実時刻・秒単位)
    # ★netkeiba だけ時間軸が分格子でなく実時刻なので、flow_lead_seconds を秒で細かく
    #   指定できる(例 75)。JV-Link は配信遅れで T-120s が限界。
    flow_source: str = "ts"
    # --- live(IPAT 実投票)。mode="live" のとき有効。多重ゲート: confirm_live + 1件/1日上限 + レシピ verified ---
    confirm_live: bool = False
    ipat_recipe: str | None = None      # レシピ JSON(hro-buyer ipat show-recipe の雛形を実画面で調整)
    ipat_headless: bool = True
    ipat_screenshot_dir: str | None = None
    manual_confirm: bool = False        # 確認画面ごとに端末で y を求める(半自動)
    max_amount_per_order: int = 0       # live 必須(>0)
    max_amount_per_day: int = 0         # live 必須(>0)
    max_amount_per_race: int = 0
    # 券種別プラン(併用運用)。各 dict: {bet_types, min_er, max_er, min_prob, max_odds}。
    # 空なら従来の単一設定(min_er/max_er/min_prob/bet_types)。例: trio帯[1.7,2.0] と wide er>=1.7&prob>=0.10 同時。
    plans: tuple = ()


def _betting(cfg: DayConfig) -> BettingConfig:
    # confirmed/replay は鮮度概念なし=無制限。live は max_odds_age で締める。
    age = float("inf") if cfg.source in ("confirmed", "replay") else cfg.max_odds_age
    return BettingConfig(
        min_expected_return=cfg.min_er,
        max_expected_return=cfg.max_er,     # er_cal帯の上限(None=無し)
        min_probability=cfg.min_prob,
        max_odds=cfg.max_odds,              # ★trioは既定50だと全弾き。cfg(preset trio=2000)を反映
        max_odds_age_seconds=age,
        allowed_bet_types=tuple(cfg.bet_types),
        ev_lcb_z=cfg.ev_lcb_z,
    )


def _betting_plans(cfg: DayConfig) -> list:
    """券種別プラン(cfg.plans)を BettingConfig のリストに。空なら単一設定を1件だけ。"""
    if not cfg.plans:
        return [_betting(cfg)]
    age = float("inf") if cfg.source in ("confirmed", "replay") else cfg.max_odds_age
    out = []
    for p in cfg.plans:
        out.append(BettingConfig(
            min_expected_return=p.get("min_er", cfg.min_er),
            max_expected_return=p.get("max_er", cfg.max_er),
            min_probability=p.get("min_prob", cfg.min_prob),
            max_odds=p.get("max_odds", cfg.max_odds),
            max_odds_age_seconds=age,
            allowed_bet_types=tuple(p["bet_types"]),
            ev_lcb_z=p.get("ev_lcb_z", cfg.ev_lcb_z),
        ))
    return out


def _money(cfg: DayConfig) -> MoneyManagerConfig:
    """bankroll>0 なら分数Kelly、そうでなければ従来のフラット固定額。"""
    if cfg.bankroll > 0:
        # 分数Kelly: 金額 = kelly_fraction × fractional_kelly_multiplier × bankroll、券/レース/日で上限。
        return MoneyManagerConfig(
            flat_amount=0,                                  # 0=Kelly経路
            bankroll=cfg.bankroll,
            fractional_kelly_multiplier=cfg.kelly_fraction,
            ticket_min_amount=cfg.ticket_min_amount,
            ticket_max_amount=cfg.ticket_max_amount,
            max_tickets_per_race=cfg.max_tickets_per_race,
            daily_budget=cfg.daily_budget,
            race_max_amount=cfg.race_max_amount,
        )
    a = cfg.flat_amount                                     # フラット(検証と同一土俵)
    return MoneyManagerConfig(
        flat_amount=a, ticket_min_amount=a, ticket_max_amount=a,
        max_tickets_per_race=999, daily_budget=10 ** 9, race_max_amount=10 ** 9,
    )


def day_races(db: FeatureDB, date: str) -> list[tuple[tuple[str, ...], str]]:
    """当日の JRA レース [(race_key6, hasso_time"HHMM")] を発走時刻順で返す(nl_ra)。

    未来の開催日は feat_labels(結果)にはまだ無いので、レース master の nl_ra から引く。
    synchronizer が当日の RA を同期していないと空になる。
    """
    y, md = date[:4], date[4:]
    rows = db.query(
        "SELECT DISTINCT year, month_day, jyo_cd, kaiji, nichiji, race_num, hasso_time "
        "FROM nl_ra "
        "WHERE year = %(y)s AND month_day = %(md)s "
        "  AND jyo_cd IN ('01','02','03','04','05','06','07','08','09','10') "
        "  AND hasso_time IS NOT NULL AND hasso_time <> ''",
        {"y": y, "md": md},
    )
    seen: dict[tuple[str, ...], str] = {}
    for r in rows:
        key = (r["year"], r["month_day"], r["jyo_cd"],
               r["kaiji"], r["nichiji"], r["race_num"])
        seen.setdefault(key, str(r["hasso_time"]))  # 複数版があれば最初を採用
    items = list(seen.items())
    items.sort(key=lambda kh: (kh[1], kh[0][2], kh[0][5]))  # hasso_time, jyo, race
    return items


_CALIB_CACHE: dict = {}


def _calibrators(path: str | None) -> dict | None:
    """trio較正JSON(fit-trio-calib出力)をパスでキャッシュして読む。None=較正なし。"""
    if not path:
        return None
    if path not in _CALIB_CACHE:
        from hro_optimizer.calibration import load_calibrators
        _CALIB_CACHE[path] = load_calibrators(path)
    return _CALIB_CACHE[path]


def decide_orders(cfg: DayConfig, win_b, place_b, race: tuple[str, ...]) -> tuple[dict | None, list]:
    """1レースの発注候補を live オッズで判断(較正→er_cal帯選別→分数Kelly)。
    戻り (abilities_dict|None, orders)。abilities は監視用 prediction_log 記録に使う。

    strategy="flow" のときはモデルを使わず、締切直前の単勝プール資金移動で選ぶ。
    ★検証(2026-09-25, ts_o1 13か月3,555レース, T-60s, 起点6分前, 上側5%):
      全期間 2,449件 ROI 1.0768 CI[1.005,1.158] P(<=1)=0.017
      OOS(閾値 202509-202602 → 検証 202603-202609) 1,584件 ROI 1.0941 P(<=1)=0.025
      ただし**13か月中5か月がマイナス**(0.91-1.37)。薄いエッジなので1日の結果では判断しない。
      T-60s は締切ちょうどで JV-Link では買えない。執行は netkeiba で T-75s に寄せる。
    abilities は None を返す(確率を推定しないので prediction_log には残らない)。
    """
    if cfg.strategy == "flow":
        from .flow_signal import FlowConfig, flow_orders
        db = FeatureDB(load_features_config())
        try:
            fc = FlowConfig(lead_seconds=cfg.flow_lead_seconds, flow_minutes=cfg.flow_minutes,
                            threshold=cfg.flow_threshold, source=cfg.flow_source,
                            thresholds=cfg.flow_thresholds,
                            max_odds=cfg.max_odds or 0.0)
            # ★実際に使った閾値を記録に残す。thresholds を使うと flow_threshold は 0.0 の
            #   ままなので、単一値だけ書くと「どの閾値で買ったのか」が後から分からない。
            thr_note = (",".join(f"{k}:{v:+.4f}" for k, v in sorted(fc.thresholds.items()))
                        if fc.thresholds else f"{cfg.flow_threshold:+.4f}")
            return None, flow_orders(db, race, fc, cfg.flat_amount,
                                     f"flow@{cfg.flow_source}:T-{cfg.flow_lead_seconds}s:{thr_note}")
        finally:
            db.close()
    from hro_backtest import harness          # モデル戦略のみ(LightGBM を引き込む)

    db = FeatureDB(load_features_config())
    conn = opt_connect()
    try:
        abilities, orders = harness.orders_for_race_multi(
            db, conn, win_b, place_b, race,
            bettings=_betting_plans(cfg), money=_money(cfg),
            sim=SimConfig(), kelly=KellyConfig(),
            source=cfg.source, simultaneous=cfg.simultaneous,
            prob_calibrators=_calibrators(cfg.calib_path),
        )
        return abilities, orders
    finally:
        conn.close()
        db.close()


class _MultiResultSink:
    """複数の ResultSink に emit を配る(結果を JSONL=決済用 と DB=admin表示用 の両方へ)。"""

    def __init__(self, sinks: list) -> None:
        self._sinks = sinks

    def emit(self, results: list) -> None:
        for s in self._sinks:
            s.emit(results)


def _clear_race(cfg: DayConfig, race_id: str) -> None:
    """当該レース×budget_key の bet_orders/bet_results を削除(④再実行の重複防止=冪等)。"""
    import psycopg
    pg = PostgresConfig.from_env()
    with psycopg.connect(pg.conninfo) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM bet_orders   WHERE race_id=%s AND budget_key=%s", (race_id, cfg.date))
        # live で成立(submitted/unknown)した行は監査記録なので絶対に消さない
        # (再起動時の preload の元でもある)。それ以外(paper/dry_run/skipped/failed)は消して再記録。
        cur.execute("DELETE FROM bet_results  WHERE race_id=%s AND budget_key=%s "
                    "AND NOT (mode=%s AND status = ANY(%s))",
                    (race_id, cfg.date, MODE_LIVE, sorted(COMMITTED_STATUSES)))
        conn.commit()


def _persist_orders(cfg: DayConfig, orders: list) -> None:
    """発注候補を bet_orders(+ decision_logs は空) へ記録(admin「購入指示」に反映)。"""
    from hro_moneymanager.postgres import PostgresOrderSink
    sink = PostgresOrderSink(PostgresConfig.from_env(), budget_key=cfg.date,
                             decided_at=datetime.now(JST))
    try:
        sink.emit(orders, [])
    finally:
        close = getattr(sink, "close", None)
        if callable(close):
            close()


def _buyer_config(cfg: DayConfig) -> BuyerConfig:
    return BuyerConfig(
        mode=cfg.mode,
        bet_unit=(cfg.ticket_min_amount if cfg.bankroll > 0 else cfg.flat_amount),
        require_deadline=True,
        confirm_live=cfg.confirm_live,
        max_amount_per_order=cfg.max_amount_per_order,
        max_amount_per_day=cfg.max_amount_per_day,
        max_amount_per_race=cfg.max_amount_per_race,
    )


def build_day_executor(cfg: DayConfig):
    """開催日を通して使う executor を作る(live のみ。paper/dry_run は None=レース毎に自動構築)。

    live は 1 日 1 つの executor を共有する: IPAT セッション(ログイン)を維持し、1日上限・冪等を
    レース跨ぎで数える。起動時に DB の当日成立分(submitted/unknown)を preload するので、
    途中で落ちて再起動しても同じ買い目を二度買わない。
    """
    if cfg.mode != MODE_LIVE:
        return None
    from hro_buyer.__main__ import build_live_executor
    pg = PostgresConfig.from_env()
    return build_live_executor(
        _buyer_config(cfg),
        deadline_provider=PostgresDeadlineProvider(pg, lead_seconds=cfg.deadline_lead_seconds),
        sale_provider=PostgresSaleProvider(pg),
        recipe_path=cfg.ipat_recipe, headless=cfg.ipat_headless,
        screenshot_dir=cfg.ipat_screenshot_dir, manual_confirm=cfg.manual_confirm,
        preload_budget_key=cfg.date,
    )


def execute_orders(cfg: DayConfig, orders: list, executor=None):
    """発注候補を実行(締切/発売可否/実行直前ガード → paper は記録のみ / live は IPAT 投票)し、
    結果を JSONL(決済用) と bet_results(admin表示用) の両方へ記録。"""
    if cfg.mode == MODE_LIVE and executor is None:
        raise ValueError("live には build_day_executor で作った executor が必要です")
    pg = PostgresConfig.from_env()
    db_sink = PostgresResultSink(pg, budget_key=cfg.date)
    try:
        svc = BuyerService(
            InMemoryOrderSource(orders),
            config=_buyer_config(cfg),
            executor=executor,
            result_sink=_MultiResultSink([JsonlResultSink(cfg.results_path), db_sink]),
            deadline_provider=PostgresDeadlineProvider(pg, lead_seconds=cfg.deadline_lead_seconds),
            sale_provider=PostgresSaleProvider(pg),
        )
        return svc.run()
    finally:
        db_sink.close()


paper_buy = execute_orders   # 後方互換(旧名)


def _persist_predictions(cfg: DayConfig, race: tuple[str, ...], win_b, abilities: dict) -> None:
    """全馬の p_win/p_place を prediction_log へ記録(MLOps監視の土台)。ベストエフォート
    (失敗しても発注は止めない)。source は cfg.source(live/replay/confirmed)を採用。"""
    if not abilities or not abilities.get("runners"):
        return
    try:
        import psycopg
        from hro_backtest.predlog import build_pred_rows, upsert_predictions, model_version_of
        rows = build_pred_rows(race, abilities, model_version_of(win_b.meta),
                               cfg.source, datetime.now(JST))
        pg = PostgresConfig.from_env()
        with psycopg.connect(pg.conninfo) as conn:
            upsert_predictions(conn, rows, commit=True)
    except Exception as e:  # 監視ログの失敗は運用を止めない
        log.warning("%s: prediction_log 記録に失敗(監視のみ影響): %s", "".join(race), e)


def process_race(cfg: DayConfig, win_b, place_b, race: tuple[str, ...], executor=None) -> None:
    """1レース: 判断 → 予測ログ記録 → bet_orders 記録 → (発注があれば) 購入実行(bet_results)。
    executor は live のとき build_day_executor で作った日次共有 executor(paper は None)。"""
    race_id = "".join(race)
    abilities, orders = decide_orders(cfg, win_b, place_b, race)
    if abilities is not None:
        _persist_predictions(cfg, race, win_b, abilities)  # 全馬予測を監視用に保存(発注有無に関わらず)
    _clear_race(cfg, race_id)  # 再実行/古い残骸を除去してから記録(冪等)
    if not orders:
        log.info("%s: 発注なし(live odds未取得 or 条件を満たす候補なし)", race_id)
        return
    _persist_orders(cfg, orders)          # bet_orders(admin「購入指示」)
    res = execute_orders(cfg, orders, executor)   # 実行 → JSONL + bet_results
    log.info("%s: %d件発注 -> %s (intended=%d円) DB+追記=%s",
             race_id, len(orders), res.count_by_status(), res.total_amount, cfg.results_path)
    for r in res.results:
        if r.status == "unknown":
            log.error("%s: ★受付確認不能(unknown) %s/%s %d円 → IPAT 投票履歴で手動照合。再送はしない",
                      race_id, r.bet_type, r.selection_id, r.amount)


class TimingError(ValueError):
    """発注時刻と締切の関係が破綻している(1件も通らない設定)。"""


def check_timing(cfg: DayConfig) -> None:
    """発注時刻が締切より後になっていないかを**開始前に**弾く。

    ★これを黙って通すと最悪の壊れ方をする: run-day は T-lead_seconds まで待って発注するが、
    ガード(_GuardedExecutor._deadline_reason)は T-deadline_lead_seconds を過ぎた発注を
    past voting deadline で捨てる。つまり「正常に1日走りきって、1件も買えていない」に
    なる。開催日は取り返しがつかないので、走り出す前に落とす。
    """
    if cfg.source in ("confirmed", "replay"):
        return                                  # 過去日の配管検証。締切は無関係
    if cfg.lead_seconds <= cfg.deadline_lead_seconds:
        raise TimingError(
            f"発注時刻 T-{cfg.lead_seconds}s が締切 T-{cfg.deadline_lead_seconds}s 以降です。"
            f"この設定では全件が past voting deadline で捨てられ、1件も投票できません。"
            f"--lead-seconds を {cfg.deadline_lead_seconds} より大きくしてください"
            f"(例 {cfg.deadline_lead_seconds + 30})。"
        )
    # ★リードは「発走の何秒前か」なので **大きいほど早い時刻**。
    #   発走-120s(スナップ) → 発走-70s(投票開始) → 発走-60s(締切) の順に起きるので、
    #   正しい関係は flow_lead > lead > deadline_lead。
    #   以前ここを >= で書いており、**正しい設定のほうを弾いていた**(2026-09-22 実害)。
    # ★閾値が決定時点に対応していないと、全レースで「閾値未設定のため見送り」になり
    #   **1日走りきって1件も買えない**。走り出す前に落とす(check_timing の存在理由と同じ)。
    if cfg.strategy == "flow" and cfg.flow_thresholds:
        want = (int(cfg.flow_lead_seconds) if cfg.flow_source == "netkeiba"
                else int(round(cfg.flow_lead_seconds / 60.0)) * 60)
        if want not in cfg.flow_thresholds:
            raise TimingError(
                f"決定時点 T-{cfg.flow_lead_seconds}s に対応する閾値がありません"
                f"(--flow-thresholds のキー: {sorted(cfg.flow_thresholds)})。"
                f"このままでは全レースが見送りになり、1日走って1件も買えません。"
                f"キー {want} を追加するか --flow-lead-seconds を合わせてください。"
                + ("" if cfg.flow_source == "netkeiba" else
                   " ※netkeiba 以外は発表時刻が分格子なので60秒に丸めたキーで引きます。"))
    if cfg.strategy == "flow" and cfg.flow_lead_seconds <= cfg.lead_seconds:
        raise TimingError(
            f"判断に使うスナップショット T-{cfg.flow_lead_seconds}s は、発注時刻 "
            f"T-{cfg.lead_seconds}s にはまだ存在しません(スナップの方が後の時刻)。"
            f"--flow-lead-seconds を --lead-seconds より大きくしてください"
            f"(締切 T-{cfg.deadline_lead_seconds}s も跨げないので、"
            f"flow-lead > lead > {cfg.deadline_lead_seconds} が必要)。"
        )


def run_day(cfg: DayConfig, *, no_wait: bool = False) -> int:
    """開催日を通す。no_wait=True なら待機せず全レースを即処理(当日途中起動/検証用)。"""
    check_timing(cfg)
    win_b = place_b = None
    if cfg.strategy != "flow":        # flow はモデルを使わない
        from hro_backtest import harness      # モデル戦略のみ(LightGBM を引き込む)
        win_b, place_b = harness.load_models(cfg.win_model, cfg.place_model)
    db = FeatureDB(load_features_config())
    try:
        races = day_races(db, cfg.date)
    finally:
        db.close()
    if not races:
        log.warning("%s: 対象レースなし。nl_ra に当日 JRA レースが無い"
                    "(synchronizer の当日同期を確認)", cfg.date)
        return 0
    if cfg.strategy == "flow":
        log.info("%s: %d レース | flow flat¥%d place | 閾値%+.4f | T-%ds起点T-%dm src=%s | mode=%s",
                 cfg.date, len(races), cfg.flat_amount, cfg.flow_threshold,
                 cfg.flow_lead_seconds, cfg.flow_minutes, cfg.flow_source, cfg.mode)
    else:
        log.info("%s: %d レース | flat¥%d place | min_er>=%.2f min_prob>=%.2f | T-%ds | mode=%s",
                 cfg.date, len(races), cfg.flat_amount, cfg.min_er, cfg.min_prob,
                 cfg.lead_seconds, cfg.mode)

    # confirmed/replay は過去日の配管検証用: 締切/鮮度は無関係なので全レースを即処理する。
    verify = cfg.source in ("confirmed", "replay")
    if verify:
        log.info("%s: 検証モード(source=%s) 締切を無視して全レース即処理", cfg.date, cfg.source)

    executor = build_day_executor(cfg)   # live のみ(IPAT ログイン + 当日成立分 preload)
    if executor is not None:
        log.warning("%s: ★★ LIVE(実弾) mode: 1件上限%d円 / 1日上限%d円 / 手動確認=%s / recipe=%s",
                    cfg.date, cfg.max_amount_per_order, cfg.max_amount_per_day,
                    cfg.manual_confirm, cfg.ipat_recipe or "(既定=未検証)")
    processed = 0
    try:
        processed = _run_races(cfg, races, win_b, place_b, executor, verify=verify, no_wait=no_wait)
    finally:
        if executor is not None:
            try:
                executor.client.logout()
            except Exception as e:  # ログアウト失敗は集計を妨げない
                log.warning("IPAT logout failed: %s", e)
    log.info("%s: 完了 (%d/%d レース処理)", cfg.date, processed, len(races))
    return processed


def _run_races(cfg: DayConfig, races, win_b, place_b, executor, *, verify: bool, no_wait: bool) -> int:
    processed = 0
    for race, hasso in races:
        race_id = "".join(race)
        if not verify:
            deadline = deadline_from(race_id, hasso, cfg.lead_seconds)  # JST tz-aware
            if deadline is None:
                log.info("%s: 締切不明(hasso_time=%r) skip", race_id, hasso)
                continue
            wait = (deadline - datetime.now(JST)).total_seconds()
            if wait < -cfg.grace_seconds:
                log.info("%s: 締切を %.0fs 超過 skip", race_id, -wait)
                continue
            if wait > 0 and not no_wait:
                # ★判断の前に、画面を「そのレースの式別」まで進めておく(preselect)。
                #   配信遅れで判断は締切ぎりぎりになるので、締切直前に残す作業を
                #   馬番と金額だけにしておかないと投票が間に合わない。
                pre = getattr(executor, "preselect", None)
                if callable(pre):
                    lead = min(cfg.preselect_lead_seconds, max(0, wait - 5))
                    if wait > lead:
                        time.sleep(wait - lead)
                    log.info("%s: 画面を先に進めます(T-%ds)", race_id, cfg.lead_seconds + lead)
                    pre(race_id, cfg.bet_types[0] if cfg.bet_types else "place")
                    wait = (deadline - datetime.now(JST)).total_seconds()
                log.info("%s: 発走%s の T-%ds(%s)まで %.0fs 待機",
                         race_id, hasso, cfg.lead_seconds,
                         deadline.strftime("%H:%M:%S"), wait)
                if wait > 0:
                    time.sleep(wait)
        try:
            process_race(cfg, win_b, place_b, race, executor)
            processed += 1
        except Exception:  # 1レースの失敗で開催日全体を止めない
            log.exception("%s: 処理失敗(継続)", race_id)
    return processed


def list_day(cfg: DayConfig) -> None:
    """当日レースと締切(T-lead)を表示するだけ(発注しない)。段取り確認用。"""
    db = FeatureDB(load_features_config())
    try:
        races = day_races(db, cfg.date)
    finally:
        db.close()
    # ★締切と投票開始は別物。以前はどちらも lead_seconds で表示していて、
    #   「締切(T-30s)」のように実際より遅い締切を刷っていた。混同すると事故る。
    print(f"=== {cfg.date} JRA races: {len(races)} "
          f"(締切=T-{cfg.deadline_lead_seconds}s / 投票開始=T-{cfg.lead_seconds}s) ===")
    now = datetime.now(JST)
    for race, hasso in races:
        race_id = "".join(race)
        dl = deadline_from(race_id, hasso, cfg.deadline_lead_seconds)
        act = deadline_from(race_id, hasso, cfg.lead_seconds)
        if dl is None or act is None:
            print(f"  {race_id}  hasso={hasso!r:>8}  締切不明")
            continue
        delta = (act - now).total_seconds()
        when = "済" if delta < 0 else f"{delta/60:.0f}分後"
        print(f"  {race_id}  発走{hasso}  投票開始={act.strftime('%H:%M:%S')}"
              f"  締切={dl.strftime('%H:%M:%S')}  {when}")

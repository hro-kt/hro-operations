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

from hro_backtest import harness

from hro_buyer.config import BuyerConfig
from hro_buyer.models import MODE_PAPER
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
    mode: str = MODE_PAPER
    bet_types: tuple[str, ...] = ("place",)
    source: str = "live"  # live=ts_sokuho(本番) / confirmed=nl_o*(過去レースでの検証用)
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


def _betting(cfg: DayConfig) -> BettingConfig:
    # confirmed は鮮度概念なし=無制限。live は max_odds_age で締める。
    age = float("inf") if cfg.source == "confirmed" else cfg.max_odds_age
    return BettingConfig(
        min_expected_return=cfg.min_er,
        max_expected_return=cfg.max_er,     # er_cal帯の上限(None=無し)
        min_probability=cfg.min_prob,
        max_odds_age_seconds=age,
        allowed_bet_types=tuple(cfg.bet_types),
    )


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


def decide_orders(cfg: DayConfig, win_b, place_b, race: tuple[str, ...]) -> list:
    """1レースの発注候補を live オッズで判断して返す(較正→er_cal帯選別→分数Kelly)。"""
    db = FeatureDB(load_features_config())
    conn = opt_connect()
    try:
        _abilities, orders = harness.orders_for_race(
            db, conn, win_b, place_b, race,
            betting=_betting(cfg), money=_money(cfg),
            sim=SimConfig(), kelly=KellyConfig(),
            source=cfg.source, simultaneous=cfg.simultaneous,
            prob_calibrators=_calibrators(cfg.calib_path),
        )
        return orders
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
        cur.execute("DELETE FROM bet_results  WHERE race_id=%s AND budget_key=%s", (race_id, cfg.date))
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


def paper_buy(cfg: DayConfig, orders: list):
    """発注候補を paper モードで実行(締切/発売可否/実行直前ガード)し、
    結果を JSONL(決済用) と bet_results(admin表示用) の両方へ記録。"""
    pg = PostgresConfig.from_env()
    db_sink = PostgresResultSink(pg, budget_key=cfg.date)
    try:
        svc = BuyerService(
            InMemoryOrderSource(orders),
            config=BuyerConfig(mode=cfg.mode,
                               bet_unit=(cfg.ticket_min_amount if cfg.bankroll > 0 else cfg.flat_amount),
                               require_deadline=True),
            result_sink=_MultiResultSink([JsonlResultSink(cfg.results_path), db_sink]),
            deadline_provider=PostgresDeadlineProvider(pg, lead_seconds=0),
            sale_provider=PostgresSaleProvider(pg),
        )
        return svc.run()
    finally:
        db_sink.close()


def process_race(cfg: DayConfig, win_b, place_b, race: tuple[str, ...]) -> None:
    """1レース: 判断 → bet_orders 記録 → (発注があれば) paper 購入(bet_results)。"""
    race_id = "".join(race)
    orders = decide_orders(cfg, win_b, place_b, race)
    _clear_race(cfg, race_id)  # 再実行/古い残骸を除去してから記録(冪等)
    if not orders:
        log.info("%s: 発注なし(live odds未取得 or 条件を満たす候補なし)", race_id)
        return
    _persist_orders(cfg, orders)          # bet_orders(admin「購入指示」)
    res = paper_buy(cfg, orders)          # 実行 → JSONL + bet_results
    log.info("%s: %d件発注 -> %s (intended=%d円) DB+追記=%s",
             race_id, len(orders), res.count_by_status(), res.total_amount, cfg.results_path)


def run_day(cfg: DayConfig, *, no_wait: bool = False) -> int:
    """開催日を通す。no_wait=True なら待機せず全レースを即処理(当日途中起動/検証用)。"""
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
    log.info("%s: %d レース | flat¥%d place | min_er>=%.2f min_prob>=%.2f | T-%ds | mode=%s",
             cfg.date, len(races), cfg.flat_amount, cfg.min_er, cfg.min_prob,
             cfg.lead_seconds, cfg.mode)

    processed = 0
    for race, hasso in races:
        race_id = "".join(race)
        deadline = deadline_from(race_id, hasso, cfg.lead_seconds)  # JST tz-aware
        if deadline is None:
            log.info("%s: 締切不明(hasso_time=%r) skip", race_id, hasso)
            continue
        wait = (deadline - datetime.now(JST)).total_seconds()
        if wait < -cfg.grace_seconds:
            log.info("%s: 締切を %.0fs 超過 skip", race_id, -wait)
            continue
        if wait > 0 and not no_wait:
            log.info("%s: 発走%s の T-%ds(%s)まで %.0fs 待機",
                     race_id, hasso, cfg.lead_seconds,
                     deadline.strftime("%H:%M:%S"), wait)
            time.sleep(wait)
        try:
            process_race(cfg, win_b, place_b, race)
            processed += 1
        except Exception:  # 1レースの失敗で開催日全体を止めない
            log.exception("%s: 処理失敗(継続)", race_id)
    log.info("%s: 完了 (%d/%d レース処理)", cfg.date, processed, len(races))
    return processed


def list_day(cfg: DayConfig) -> None:
    """当日レースと締切(T-lead)を表示するだけ(発注しない)。段取り確認用。"""
    db = FeatureDB(load_features_config())
    try:
        races = day_races(db, cfg.date)
    finally:
        db.close()
    print(f"=== {cfg.date} JRA races: {len(races)} (T-{cfg.lead_seconds}s deadline) ===")
    now = datetime.now(JST)
    for race, hasso in races:
        race_id = "".join(race)
        dl = deadline_from(race_id, hasso, cfg.lead_seconds)
        if dl is None:
            print(f"  {race_id}  hasso={hasso!r:>8}  締切不明")
            continue
        delta = (dl - now).total_seconds()
        when = "済" if delta < 0 else f"{delta/60:.0f}分後"
        print(f"  {race_id}  発走{hasso}  締切(T-{cfg.lead_seconds}s)={dl.strftime('%H:%M:%S')}  {when}")

"""hro-ops CLI: 開催日ランナー。

    # 当日(JST)の全レースを T-30s に flat¥100 place で paper 発注(常駐)
    hro-ops run-day --win-model models/win_prod.joblib --place-model models/place_prod.joblib

    # 段取り確認(発注しない): 当日レースと締切を一覧
    hro-ops list --win-model m --place-model m

    # 1レースを今すぐ判断→paper(liveスモークテスト。待機しない)
    hro-ops once --win-model m --place-model m --race 2026 0719 05 03 04 11

    # 当日途中から: 待機せず残りレースを即処理(締切超過は自動skip)
    hro-ops run-day ... --no-wait

学習時と同じ特徴スキーマで実行すること(例: no-SED 279 は環境変数 HRO_ABLATE_SED=1 を付ける)。
決済は hro-buyer settle を使う: `hro-buyer settle --results results_YYYYMMDD.jsonl`
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime

from hro_buyer.models import MODE_DRY_RUN, MODE_PAPER
from hro_buyer.postgres import JST

from .race_day import DayConfig, list_day, process_race, run_day


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    load_dotenv()


def _today() -> str:
    return datetime.now(JST).strftime("%Y%m%d")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--win-model", required=True, help="単勝モデル(target=y_win)")
    p.add_argument("--place-model", required=True, help="複勝モデル(target=y_fukusyo)")
    p.add_argument("--date", default=None, help="YYYYMMDD(省略時は当日JST)")
    p.add_argument("--min-er", type=float, default=1.3, help="期待値下限(既定1.3)")
    p.add_argument("--min-prob", type=float, default=0.15, help="確率下限(既定0.15)")
    p.add_argument("--flat-amount", type=int, default=100, help="1点固定額(円, 既定100)")
    p.add_argument("--bet-types", default="place", help="対象券種(カンマ区切り, 既定 place)")
    p.add_argument("--lead-seconds", type=int, default=30, help="発走−これ秒に発注(既定30=T-30s)")
    p.add_argument("--max-odds-age", type=float, default=60.0, help="live鮮度上限(秒, 既定60)")
    p.add_argument("--grace-seconds", type=int, default=180,
                   help="締切をこれ秒超過なら見送り(既定180)")
    p.add_argument("--results", default=None, help="結果JSONL(既定 results_<date>.jsonl)")
    p.add_argument("--mode", choices=(MODE_PAPER, MODE_DRY_RUN), default=MODE_PAPER)
    p.add_argument("--source", choices=("live", "confirmed"), default="live",
                   help="live=ts_sokuho(本番) / confirmed=nl_o*(過去レースでの配管検証用)")
    # --- trio運用(er_cal帯選別・較正・分数Kelly) ---
    p.add_argument("--preset", choices=("trio",), default=None,
                   help="trio運用の推奨値を一括設定(bet-types=trio, min-er1.7, max-er2.0, min-prob0.0, simultaneous)")
    p.add_argument("--max-er", type=float, default=None, help="期待値上限(帯選別。例2.0で[min_er,2.0))")
    p.add_argument("--calib", default=None, help="trio較正JSON(fit-trio-calib出力)。EV前に適用")
    p.add_argument("--simultaneous", action="store_true", help="レース内joint Kelly(trio推奨)")
    p.add_argument("--bankroll", type=int, default=0, help=">0で分数Kelly(0=flat)")
    p.add_argument("--kelly-fraction", type=float, default=0.25, help="フラクショナルケリー係数(既定1/4)")
    p.add_argument("--daily-budget", type=int, default=20_000, help="1日購入上限(Kelly時)")
    p.add_argument("--race-max", type=int, default=10_000, help="1レース購入上限(Kelly時)")
    p.add_argument("--ticket-max", type=int, default=5_000, help="1点最大額(Kelly時)")
    p.add_argument("--max-tickets", type=int, default=3, help="1レース最大点数(Kelly時)")


def _cfg(args) -> DayConfig:
    date = args.date or _today()
    min_er, max_er, min_prob = args.min_er, args.max_er, args.min_prob
    bet_types, simultaneous = args.bet_types, args.simultaneous
    if args.preset == "trio":  # デフォルトのままの項目だけ trio 推奨値に上書き(明示指定は尊重)
        bet_types = "trio"
        if args.min_er == 1.3:   min_er = 1.7
        if args.max_er is None:  max_er = 2.0
        if args.min_prob == 0.15: min_prob = 0.0
        simultaneous = True
    return DayConfig(
        date=date,
        win_model=args.win_model,
        place_model=args.place_model,
        results_path=args.results or f"results_{date}.jsonl",
        min_er=min_er,
        min_prob=min_prob,
        flat_amount=args.flat_amount,
        max_odds_age=args.max_odds_age,
        lead_seconds=args.lead_seconds,
        grace_seconds=args.grace_seconds,
        mode=args.mode,
        bet_types=tuple(x.strip() for x in bet_types.split(",") if x.strip()),
        source=args.source,
        max_er=max_er,
        calib_path=args.calib,
        simultaneous=simultaneous,
        bankroll=args.bankroll,
        kelly_fraction=args.kelly_fraction,
        daily_budget=args.daily_budget,
        race_max_amount=args.race_max,
        ticket_max_amount=args.ticket_max,
        max_tickets_per_race=args.max_tickets,
    )


def _cmd_run_day(args) -> int:
    run_day(_cfg(args), no_wait=args.no_wait)
    return 0


def _cmd_list(args) -> int:
    list_day(_cfg(args))
    return 0


def _cmd_once(args) -> int:
    from hro_backtest import harness

    cfg = _cfg(args)
    win_b, place_b = harness.load_models(cfg.win_model, cfg.place_model)
    process_race(cfg, win_b, place_b, tuple(args.race))
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_env()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="hro-ops", description="開催日の前向きペーパー運用(自動投票はしない)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run-day", help="当日全レースを T-lead に paper 発注(常駐)")
    _add_common(p_run)
    p_run.add_argument("--no-wait", action="store_true",
                       help="待機せず残りレースを即処理(当日途中起動/検証用)")
    p_run.set_defaults(func=_cmd_run_day)

    p_list = sub.add_parser("list", help="当日レースと締切を一覧(発注しない)")
    _add_common(p_list)
    p_list.set_defaults(func=_cmd_list)

    p_once = sub.add_parser("once", help="1レースを今すぐ判断→paper(待機しない)")
    _add_common(p_once)
    p_once.add_argument("--race", nargs=6, required=True,
                        metavar=("YEAR", "MONTHDAY", "JYO", "KAIJI", "NICHIJI", "RACENUM"))
    p_once.set_defaults(func=_cmd_once)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

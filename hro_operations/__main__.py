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
(DB から: `hro-buyer settle --from-db --budget-key YYYYMMDD --write`)

    # live(実弾)。レシピを ipat dry-vote で検証(verified=true)してから。最初は --manual-confirm 推奨
    hro-ops run-day --strategy flow --flow-threshold <v> --mode live --confirm-live \
        --ipat-recipe ~/ipat_recipe.json --max-amount-per-order 1000 --max-amount-per-day 20000 \
        --manual-confirm --ipat-screenshot-dir ~/ipat_shots
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone

# hro_buyer / race_day(=購入・最適化スタック)はトップレベルで import しない。
# こうしないと `hro-ops agent`(Windowsのsync/odds用)が buyer 未導入環境で
# ModuleNotFoundError になる。必要なコマンド内で遅延 import する。
# 下記は hro_buyer.models / hro_buyer.postgres と値を一致させること。
MODE_DRY_RUN = "dry_run"
MODE_PAPER = "paper"
MODE_LIVE = "live"
JST = timezone(timedelta(hours=9))


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    load_dotenv()


def _today() -> str:
    return datetime.now(JST).strftime("%Y%m%d")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--win-model", default="", help="単勝モデル(target=y_win)。--strategy flow では不要")
    p.add_argument("--place-model", default="", help="複勝モデル(target=y_fukusyo)。--strategy flow では不要")
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
    p.add_argument("--mode", choices=(MODE_PAPER, MODE_DRY_RUN, MODE_LIVE), default=MODE_PAPER,
                   help="paper=記録のみ(既定) / live=IPAT 実投票(--confirm-live と上限が必須)")
    # --- live(IPAT 実投票)。多重ゲート: --confirm-live + 1件/1日上限 + レシピ verified(+任意で手動確認) ---
    p.add_argument("--confirm-live", action="store_true", help="live の明示確認(無いと live は起動しない)")
    p.add_argument("--ipat-recipe", default=None,
                   help="IPAT 画面レシピ JSON(hro-buyer ipat show-recipe の雛形を dry-vote で検証したもの)")
    p.add_argument("--ipat-no-headless", action="store_true", help="live: ブラウザを表示して実行")
    p.add_argument("--ipat-screenshot-dir", default=None, help="live: 確認/受付/エラー画面のスクショ保存先")
    p.add_argument("--manual-confirm", action="store_true",
                   help="live: 確認画面ごとに端末で y を求める(半自動。最初はこれを推奨)")
    p.add_argument("--max-amount-per-order", type=int, default=0, help="1件上限(円)。live 必須")
    p.add_argument("--max-amount-per-day", type=int, default=0, help="1日上限(円)。live 必須")
    p.add_argument("--max-amount-per-race", type=int, default=0, help="1レース上限(円)。0=無効")
    p.add_argument("--source", choices=("live", "confirmed", "replay"), default="live",
                   help="live=ts_sokuho(本番) / confirmed=nl_o*(過去レースでの配管検証用)")
    # --- trio運用(er_cal帯選別・較正・分数Kelly) ---
    p.add_argument("--preset", choices=("trio", "trio_wide", "wide"), default=None,
                   help="wide: 2窓OOSで唯一残った条件(wide er>=1.7 & prob>=0.10)・flat推奨【推奨】 / "
                        "trio_wide: wideと同義(trio脚は2窓OOS 0.72/0.52で削除済) / "
                        "trio: 【非推奨・検証用】er[1.7,2.0)帯は2窓OOSで0.72/0.52＝控除率以下")
    p.add_argument("--max-er", type=float, default=None, help="期待値上限(帯選別。例2.0で[min_er,2.0))")
    p.add_argument("--max-odds", type=float, default=None,
                   help="オッズ上限(既定: place=50 / preset trio=2000)。trioは配当が高いので50だと全弾き")
    p.add_argument("--calib", default=None, help="trio較正JSON(fit-trio-calib出力)。EV前に適用")
    p.add_argument("--simultaneous", action="store_true", help="レース内joint Kelly(trio推奨)")
    p.add_argument("--bankroll", type=int, default=0, help=">0で分数Kelly(0=flat)")
    p.add_argument("--kelly-fraction", type=float, default=0.25, help="フラクショナルケリー係数(既定1/4)")
    p.add_argument("--daily-budget", type=int, default=20_000, help="1日購入上限(Kelly時)")
    p.add_argument("--race-max", type=int, default=10_000, help="1レース購入上限(Kelly時)")
    p.add_argument("--ticket-max", type=int, default=5_000, help="1点最大額(Kelly時)")
    p.add_argument("--max-tickets", type=int, default=3, help="1レース最大点数(Kelly時)")
    p.add_argument("--strategy", choices=("model", "flow"), default="model",
                   help="flow: モデルを使わず締切直前の単勝プール資金移動で複勝を選ぶ"
                        "(検証 ROI 1.174 P(ROI<=1)=0.001 8/8ヶ月, docs/2026-09_flow_signal.md)")
    p.add_argument("--flow-threshold", type=float, default=0.0,
                   help="flow スコアの絶対閾値(fit 期間の分位から決めた値)")
    p.add_argument("--flow-lead-seconds", type=int, default=60,
                   help="決定時点=発走−これ秒(既定60。0B41 のスナップショット格子に合わせる)")
    p.add_argument("--flow-minutes", type=int, default=6, help="フロー起点=発走−これ分")
    p.add_argument("--flow-source", choices=("ts", "sokuho"), default="ts",
                   help="ts=公式時系列(0B41, 検証に使った経路) / sokuho=自前10秒ポーリング")
    p.add_argument("--ev-lcb-z", type=float, default=0.0,
                   help="EV下側信頼限界のz(0=従来)。MC二項SEでpを保守化し、推定上振れ組の選別を抑える")


def _cfg(args):
    from .race_day import DayConfig
    date = args.date or _today()
    min_er, max_er, min_prob = args.min_er, args.max_er, args.min_prob
    bet_types, simultaneous = args.bet_types, args.simultaneous
    max_odds = args.max_odds
    plans: tuple = ()
    max_tickets = args.max_tickets
    if args.preset == "trio":  # デフォルトのままの項目だけ trio 推奨値に上書き(明示指定は尊重)
        bet_types = "trio"
        if args.min_er == 1.3:   min_er = 1.7
        if args.max_er is None:  max_er = 2.0
        if args.min_prob == 0.15: min_prob = 0.0
        if max_odds is None:     max_odds = 2000.0   # ★trioは高配当。50だと全弾き→2000
        simultaneous = True
    elif args.preset in ("wide", "trio_wide"):
        # 2窓OOS(2025/2026)で唯一 両窓とも ROI>1 だった条件のみ: wide er>=1.7 & prob>=0.10
        #   wide er1.7/p0.10 : 2025 1.192(n425) / 2026 1.054(n258)
        #   wide er2.0/p0.05 : 2025 1.022(n945) / 2026 1.084(n471)
        # ★trio脚(er[1.7,2.0)帯)は 2025 0.722(n20061) / 2026 0.523(n7043) ＝控除率以下だったため削除。
        #   prob フィルタが本体で、er だけでは wide も全滅する(er1.7/p0.00 は 0.698/0.442)。
        bet_types = "wide"
        simultaneous = True
        if max_odds is None:     max_odds = 2000.0
        plans = (
            {"bet_types": ("wide",), "min_er": 1.7, "max_er": None, "min_prob": 0.10, "max_odds": 2000.0},
        )
        if args.max_tickets == 3:  # flatで資格ある組を広めに買う(点数上限を緩める)
            max_tickets = 30
    if max_odds is None:
        max_odds = 50.0
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
        max_odds=max_odds,
        max_er=max_er,
        calib_path=args.calib,
        simultaneous=simultaneous,
        bankroll=args.bankroll,
        kelly_fraction=args.kelly_fraction,
        daily_budget=args.daily_budget,
        race_max_amount=args.race_max,
        ticket_max_amount=args.ticket_max,
        max_tickets_per_race=max_tickets,
        ev_lcb_z=args.ev_lcb_z,
        plans=plans,
        strategy=args.strategy,
        flow_threshold=args.flow_threshold,
        flow_lead_seconds=args.flow_lead_seconds,
        flow_minutes=args.flow_minutes,
        flow_source=args.flow_source,
        confirm_live=args.confirm_live,
        ipat_recipe=args.ipat_recipe,
        ipat_headless=not args.ipat_no_headless,
        ipat_screenshot_dir=args.ipat_screenshot_dir,
        manual_confirm=args.manual_confirm,
        max_amount_per_order=args.max_amount_per_order,
        max_amount_per_day=args.max_amount_per_day,
        max_amount_per_race=args.max_amount_per_race,
    )


def _cmd_run_day(args) -> int:
    from .race_day import run_day
    run_day(_cfg(args), no_wait=args.no_wait)
    return 0


def _cmd_list(args) -> int:
    from .race_day import list_day
    list_day(_cfg(args))
    return 0


def _cmd_once(args) -> int:
    from hro_backtest import harness

    from .race_day import build_day_executor, process_race
    cfg = _cfg(args)
    win_b = place_b = None
    if cfg.strategy != "flow":
        win_b, place_b = harness.load_models(cfg.win_model, cfg.place_model)
    executor = build_day_executor(cfg)
    try:
        process_race(cfg, win_b, place_b, tuple(args.race), executor)
    finally:
        if executor is not None:
            executor.client.logout()
    return 0


def _cmd_agent(args) -> int:
    from .agent import run_agent
    return run_agent(args.server, interval=args.interval, concurrency=args.concurrency)


def main(argv: list[str] | None = None) -> int:
    _load_env()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="hro-ops", description="開催日ランナー(paper=記録のみ / live=IPAT 実投票は多重ゲート)")
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

    p_agent = sub.add_parser("agent", help="ops_jobキューを処理する常駐エージェント(admin画面から実行される)")
    p_agent.add_argument("--server", required=True, choices=("vm", "windows"),
                         help="このサーバの役割(vm=特徴/day-runner/決済, windows=JV-Link sync/odds)")
    p_agent.add_argument("--interval", type=float, default=5.0, help="キュー確認周期(秒)")
    p_agent.add_argument("--concurrency", type=int, default=3,
                         help="同時実行ジョブ数の上限(既定3。長時間trio_dayの裏でsettle等を回す)")
    p_agent.set_defaults(func=_cmd_agent)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

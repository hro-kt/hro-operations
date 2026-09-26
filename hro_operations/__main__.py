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
    p.add_argument("--deadline-lead-seconds", type=int, default=60,
                   help="発売締切=発走時刻−これ秒(実測: 発走の1分前が締切)。"
                        "0 にすると締切後に投票しようとするので下げないこと")
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
    p.add_argument("--flow-thresholds", default=None,
                   help='リード別の閾値 JSON 例 \'{"120":0.1631,"180":0.08}\'。'
                        "配信遅れで決定時点がレースごとに変わるため、実際に使った"
                        "スナップのリードに対応する閾値で判定する(未設定のリードは見送り)")
    p.add_argument("--flow-lead-seconds", type=int, default=60,
                   help="決定時点=発走−これ秒(既定60。0B41 のスナップショット格子に合わせる)")
    p.add_argument("--flow-minutes", type=int, default=6, help="フロー起点=発走−これ分")
    p.add_argument("--flow-source", choices=("ts", "sokuho", "netkeiba"), default="ts",
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
        deadline_lead_seconds=args.deadline_lead_seconds,
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
        flow_thresholds=_parse_thresholds(args.flow_thresholds),
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


def _parse_thresholds(raw: str | None) -> dict[int, float] | None:
    """--flow-thresholds の JSON を {リード秒: 閾値} に。キーは文字列でも受ける。"""
    if not raw:
        return None
    import json
    try:
        d = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f"--flow-thresholds が JSON ではありません: {e}") from e
    if not isinstance(d, dict) or not d:
        raise SystemExit("--flow-thresholds は {\"リード秒\": 閾値} の形で指定してください")
    return {int(k): float(v) for k, v in d.items()}


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


def _cmd_flow_debug(args) -> int:
    """なぜ flow の発注が出ないのかを切り分ける(発注はしない)。"""
    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .flow_signal import FlowConfig, flow_diagnose

    rid = args.race_id
    if len(rid) != 16 or not rid.isdigit():
        print("--race-id は16桁 YYYYMMDDJJKKNNRR を指定してください"); return 2
    race = (rid[0:4], rid[4:8], rid[8:10], rid[10:12], rid[12:14], rid[14:16])
    cfg = FlowConfig(lead_seconds=args.flow_lead_seconds, flow_minutes=args.flow_minutes,
                     threshold=args.flow_threshold, source=args.flow_source,
                     max_odds=args.max_odds or 0.0)
    db = FeatureDB(load_features_config())
    try:
        d = flow_diagnose(db, race, cfg)
    finally:
        db.close()

    print(f"=== flow 診断 {d['race_id']} (src={cfg.source}) ===")
    post = d["post"]
    if not post:
        print("  ✗ nl_ra にこのレースが無い(当日同期 sync-all を確認)")
        return 1
    print(f"  発走: {post['hasso_time']} ({post['post']})")
    print(f"  決定時点: 発走-{cfg.lead_seconds}秒 / フロー起点: 発走-{cfg.flow_minutes}分")
    snap = d["snapshots"] or {}
    if not snap or not snap.get("rows"):
        print("  ✗ オッズのスナップショットが1本も無い")
        print("    → Windows の fetch-timeseries-odds(0B41)が動いているか、"
              "JVRTOpen エラーで止まっていないかを確認")
        return 1
    print(f"  スナップショット: {snap['snaps']}本 / {snap['horses']}頭 / {snap['rows']}行")
    print(f"    期間: {snap['first_ts']} 〜 {snap['last_ts']}")
    if snap.get("raw_min"):
        print(f"    発表時刻(生): {snap['raw_min']} 〜 {snap['raw_max']}  ※MMDDHHMI の8桁を想定")
    from .flow_signal import snapshot_grid
    db2 = FeatureDB(load_features_config())
    try:
        grid = snapshot_grid(db2, race, cfg)
    finally:
        db2.close()
    if grid:
        leads = ", ".join(f"{int(g['lead_sec'])}s" for g in grid)
        print(f"  発走前スナップの間隔(新しい順): {leads}")
        print("    ※ 決定時点と起点が同じスナップを指すとスコアは 0 になる")

    sc = d["scores"]
    if not sc:
        print("  ✗ スコアを計算できない(決定時点より前、または起点より前のスナップが無い)")
        print("    上の『期間』が決定時点をまたいでいるか確認してください")
        return 1
    print(f"  閾値 {cfg.threshold:+.4f} を超えた馬: {d['n_above']}/{len(sc)}")
    for um, v in sorted(sc.items(), key=lambda kv: -kv[1]["score"]):
        mark = "★" if v["score"] >= cfg.threshold else "  "
        print(f"    {mark} {um}番 score={v['score']:+.4f} 複勝={v['fuku_odds']:.1f} "
              f"単勝={v['tan_odds']:.1f}")
    if d["n_above"] == 0:
        print("  → 発注なしは正常(この条件では買う馬がいない)。"
              "全レースで0なら閾値が高すぎる可能性があります。")
    return 0


_JYO = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
        "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}


def _cmd_flow_coverage(args) -> int:
    """開催日の全レースで、決定時点(T-lead)のオッズが間に合って取れているかを確認する。"""
    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .flow_signal import FlowConfig, flow_coverage

    date = args.date or _today()
    cfg = FlowConfig(lead_seconds=args.flow_lead_seconds, flow_minutes=args.flow_minutes)
    db = FeatureDB(load_features_config())
    try:
        rows = flow_coverage(db, date, cfg)
    finally:
        db.close()
    if not rows:
        print(f"{date}: nl_ra に JRA のレースがありません(当日同期を確認)")
        return 1

    print(f"=== {date} 直前オッズ({cfg.lead_seconds}秒前)の取得状況 ===")
    print("  場    R  発走   スナップ  決定時点との差  起点  取得の余裕")
    ok = late = missing = 0
    for r in rows:
        lead = r["late_lead_sec"]
        margin = r["fetch_margin_sec"]
        if lead is None:
            missing += 1
            mark, lead_s, margin_s = "✗", "なし", "-"
        else:
            lead_s = f"{lead:6.0f}秒前"
            if margin is None:
                mark, margin_s = "?", "不明"
            elif margin >= 0:
                ok += 1
                mark, margin_s = "○", f"{margin:6.0f}秒"
            else:
                late += 1
                mark, margin_s = "△", f"{-margin:6.0f}秒遅い"
        print(f"  {mark} {_JYO.get(r['jyo_cd'], r['jyo_cd'])} {int(r['race_num']):2d}R "
              f"{r['hasso_time']}  {r['snaps']:6d}  {lead_s:>12}  "
              f"{'有' if r['has_early'] else '無'}   {margin_s}")
    total = len(rows)
    print(f"\n  間に合った {ok}/{total} / 取得が遅れた {late} / スナップ無し {missing}")
    margins = [r["fetch_margin_sec"] for r in rows if r["fetch_margin_sec"] is not None]
    if margins and (max(margins) - min(margins)) > 3600:
        print("  ★ 取得の余裕がレース間で大きく違います。1回の一括取得で全レース分をまとめて"
              "入れた可能性が高い(＝当日の逐次取得になっていない)。")
        print("     本番は fetch-timeseries-odds --repeat-seconds 60 を開催中ずっと回すこと。")
    if missing:
        print("  ✗ スナップ無し: Windows の fetch-timeseries-odds(0B41)が動いていない可能性")
    if late:
        print("  △ 取得が遅れた: 取り込み自体は出来ているが**決定時点より後**。"
              "検証はできても当日の発注には使えない。取得周期を短くするか判断を早める")
    return 0 if (ok == total) else 1


def _cmd_flow_lead_scan(args) -> int:
    """決定時点を早めても信号が保たれるかを測る(ROI ではなく信号の保存度)。"""
    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .flow_signal import FlowConfig, lead_scan
    from .race_day import day_races

    date = args.date or _today()
    leads = [int(x) for x in args.leads.split(",") if x.strip()]
    cfg = FlowConfig(flow_minutes=args.flow_minutes, threshold=args.flow_threshold,
                     source=args.flow_source)
    db = FeatureDB(load_features_config())
    try:
        races = [r for r, _h in day_races(db, date)]
        if not races:
            print(f"{date}: 対象レースがありません")
            return 1
        rows = lead_scan(db, races, leads, cfg,
                         ref_source=args.ref_source, ref_lead=args.ref_lead)
    finally:
        db.close()

    print(f"=== {date} 決定時点を早めたときの信号の保存度 ===")
    print(f"  基準: {args.ref_source} の発走{args.ref_lead}秒前 / 対象: {args.flow_source} / "
          f"起点 発走{args.flow_minutes}分前 / 閾値 {args.flow_threshold:+.4f}")
    print("  発走n秒前  レース  順位相関   傾き   基準の選択  今回の選択  重なり")
    for r in rows:
        rho = f"{r['rho']:.3f}" if r["rho"] is not None else "  -  "
        sl = f"{r['slope']:.3f}" if r["slope"] is not None else "  -  "
        ja = f"{r['jaccard']:.2f}" if r["jaccard"] is not None else " - "
        print(f"   {r['lead']:5d}秒  {r['races']:5d}  {rho:>8}  {sl:>6}  "
              f"{r['n_ref']:9d}  {r['n_cur']:9d}  {ja:>6}")
    print("\n  順位相関が高く重なりが大きいほど、早めても同じ馬を選べる。")
    print("  傾きが 1 から離れると絶対閾値をそのままは使えない(閾値の取り直しが要る)。")
    return 0


def _cmd_flow_usable(args) -> int:
    """締切の直前に判断するとき、実際に手元にある最新オッズが何秒前のものかを測る。"""
    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .flow_signal import FlowConfig, usable_snapshot

    date = args.date or _today()
    cfg = FlowConfig(source=args.flow_source)
    db = FeatureDB(load_features_config())
    try:
        rows = usable_snapshot(db, date, cfg, deadline_seconds=args.deadline_seconds,
                               margin_seconds=args.margin_seconds)
    finally:
        db.close()
    if not rows:
        print(f"{date}: 対象レースがありません")
        return 1

    dec = args.deadline_seconds + args.margin_seconds
    print(f"=== {date} 締切{args.margin_seconds}秒前に判断するとき手元にあるオッズ "
          f"({args.flow_source}) ===")
    print(f"  締切=発走{args.deadline_seconds}秒前 / 判断=発走{dec}秒前")
    print("  場    R  発走   手元の最新   検証と同じスナップ(発走"
          f"{args.deadline_seconds}秒前)が届いた余裕")
    ok = 0
    for r in rows:
        lead = r["usable_lead_sec"]
        marg = r["want_margin_sec"]
        lead_s = f"{lead:5.0f}秒前" if lead is not None else "  なし "
        if marg is None:
            marg_s = "届いていない"
        elif marg >= 0:
            ok += 1
            marg_s = f"{marg:5.0f}秒 前に到着 ○"
        else:
            marg_s = f"{-marg:5.0f}秒 遅い ✗"
        print(f"  {_JYO.get(r['jyo_cd'], r['jyo_cd'])} {int(r['race_num']):2d}R "
              f"{r['hasso_time']}  {lead_s}   {marg_s}")
    print(f"\n  検証と同じスナップを判断時刻までに使えた: {ok}/{len(rows)}")
    if ok == len(rows):
        print("  → 検証どおりの設定(発走60秒前のスナップ)で執行できます。")
    elif ok == 0:
        print("  → 締切前には間に合いません。手元の最新(上の列)で信号を作り直す必要があります。")
    print("  ※ observed_at を上書きしない修正(2026-09-20)より**後**に取り込んだ日でのみ有効")
    return 0


def _cmd_flow_threshold(args) -> int:
    """使う設定(信号源・決定時点)と同じ条件で絶対閾値を取り直す。"""
    from datetime import date as _date, timedelta as _td

    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .flow_signal import FlowConfig, threshold_from
    from .race_day import day_races

    d0 = _date(int(args.d_from[:4]), int(args.d_from[4:6]), int(args.d_from[6:8]))
    d1 = _date(int(args.d_to[:4]), int(args.d_to[4:6]), int(args.d_to[6:8]))
    leads = ([int(x) for x in args.leads.split(",") if x.strip()]
             if args.leads else [args.flow_lead_seconds])
    db = FeatureDB(load_features_config())
    try:
        races = []
        d = d0
        while d <= d1:
            races += [r for r, _h in day_races(db, d.strftime("%Y%m%d"))]
            d += _td(days=1)
        if not races:
            print(f"{args.d_from}〜{args.d_to}: 対象レースがありません")
            return 1
        results = []
        for lead in leads:
            cfg = FlowConfig(lead_seconds=lead, flow_minutes=args.flow_minutes,
                             source=args.flow_source)
            results.append((lead, threshold_from(db, races, cfg, args.quantile)))
    finally:
        db.close()

    if len(results) > 1:
        import json
        print(f"=== flow_tan リード別の絶対閾値 ({args.d_from}〜{args.d_to}) ===")
        print(f"  条件: {args.flow_source} / 起点 発走{args.flow_minutes}分前 / 分位 {args.quantile}")
        print("  発走n秒前  レース   本数     閾値   閾値以上")
        table = {}
        for lead, r in results:
            if r["threshold"] is None:
                print(f"  {lead:>7}秒  スコアを作れず(スナップショット不足)")
                continue
            table[lead] = round(r["threshold"], 4)
            print(f"  {lead:>7}秒  {r['races']:>5}  {r['n']:>6,}  {r['threshold']:+.4f}  "
                  f"{r['n_above']:>5,} ({r['n_above'] / r['n']:.1%})"
                  + (f"  窓ズレ除外{r['races_skewed_window']}"
                     if r.get("races_skewed_window") else ""))
        print("\n  → run-day / agent にはこの表をそのまま渡す:")
        print(f"     --flow-thresholds '{json.dumps(table)}'")
        print("  ※ 配信遅れで決定時点はレースごとに変わる。単一の閾値だと、"
              "リードが1段ずれただけでほぼ0件になる")
        return 0
    res = results[0][1]

    if res["threshold"] is None:
        print("スコアを1本も作れませんでした(スナップショット不足)")
        return 1
    print(f"=== flow_tan 絶対閾値 ({args.d_from}〜{args.d_to}) ===")
    band = ""
    if getattr(args, "min_tan_odds", 0) or getattr(args, "max_tan_odds", 0):
        band = (f" / 単勝帯 {args.min_tan_odds or 0:g}"
                f"〜{args.max_tan_odds or float('inf'):g}倍")
    print(f"  条件: {args.flow_source}{band} / 決定時点 発走{args.flow_lead_seconds}秒前 / "
          f"起点 発走{args.flow_minutes}分前 / 分位 {args.quantile}")
    # ★除外0件でも必ず出す。出さないと「除外後に残った数」なのか「そもそもの母数」なのか
    #   区別できず、閾値が汚染されているのかどうかを読み間違える(2026-09-22 に読み間違えた)。
    print(f"  対象: {res['races']} レース / {res['n']:,} 本"
          f" (実効窓ズレで除外 {res.get('races_skewed_window', 0)} レース)")
    print(f"  閾値: {res['threshold']:+.4f}  (>=閾値 {res['n_above']:,} 本 = "
          f"{res['n_above'] / res['n']:.1%})")
    print(f"\n  → hro-ops run-day --strategy flow --flow-threshold {res['threshold']:.4f} "
          f"--flow-source {args.flow_source} --flow-lead-seconds {args.flow_lead_seconds}")
    print("  ※ 本数が想定(約5%)から大きく外れるなら、対象期間が短すぎる可能性があります")
    return 0


def _month_chunks(d_from: str, d_to: str) -> list[tuple[str, str]]:
    """[from,to] を暦月で切る。レースは月を跨がないので合算しても結果は変わらない。"""
    out = []
    y, m = int(d_from[:4]), int(d_from[4:6])
    while True:
        first = f"{y:04d}{m:02d}01"
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        last = f"{ny:04d}{nm:02d}01"
        a = max(first, d_from)
        b = min(f"{y:04d}{m:02d}31", d_to)
        if a <= b:
            out.append((a, b))
        if last > d_to:
            break
        y, m = ny, nm
    return out


def _cmd_flow_backtest(args) -> int:
    """flow_tan(複勝)の期間回収率。払戻は nl_hr の確定複勝で決済する。"""
    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .flow_signal import FlowConfig, backtest

    import time

    from .flow_signal import summarize_bets

    cfg = FlowConfig(lead_seconds=args.flow_lead_seconds, flow_minutes=args.flow_minutes,
                     source=args.flow_source, threshold=args.flow_threshold,
                     min_tan_odds=getattr(args, "min_tan_odds", 0.0),
                     max_tan_odds=getattr(args, "max_tan_odds", 0.0))
    # 月ごとに分割して回す。8ヶ月を1クエリにすると何分かかっているのか分からず、
    # 途中で止めることもできない。合算しても結果は同じ(レースは月を跨がない)。
    months = _month_chunks(args.d_from, args.d_to)
    bets: list = []
    details: list = []          # 月ごとに分割するので明細も引き継ぐ
    races = scored = degen = unsettled = 0
    db = FeatureDB(load_features_config())
    t0 = time.monotonic()
    try:
        for i, (a, b) in enumerate(months, 1):
            part = backtest(db, a, b, cfg, max_odds=args.max_odds,
                            amount=args.amount, with_ci=False)
            bets += part["_bets"]
            details += part.get("details") or []
            races += part["races"]; scored += part["races_scored"]
            degen += part["races_degenerate"]
            unsettled += part.get("races_unsettled", 0)
            st = sum(x[1] for x in bets)
            roi = (sum(x[2] for x in bets) / st) if st else 0.0
            # ★その月**単体**の回収率も出す。累計だけだと「どの月が沈んでいるか」が
            #   見えず、安定して1を超えているのかを判断できない(累積から逆算させない)。
            pst = sum(x[1] for x in part["_bets"])
            proi = (sum(x[2] for x in part["_bets"]) / pst) if pst else 0.0
            print(f"  [{i}/{len(months)}] {a[:6]}  レース{part['races']:>5,}  "
                  f"購入{part['bets']:>5,}  当月 {proi:.4f}  累計 {roi:.4f}  "
                  f"({time.monotonic() - t0:.0f}s)", flush=True)
    finally:
        db.close()
    r = summarize_bets(bets, races=races, races_scored=scored, races_degenerate=degen)
    r["details"] = details
    r["races_unsettled"] = unsettled

    print(f"=== flow_tan 複勝 回収率 ({args.d_from}〜{args.d_to}) ===")
    print(f"  条件: {args.flow_source} / 決定時点 発走{args.flow_lead_seconds}秒前 / "
          f"起点 発走{args.flow_minutes}分前 / 閾値 {args.flow_threshold:+.4f}"
          + (f" / 単勝上限 {args.max_odds}" if args.max_odds else ""))
    print(f"  レース: {r['races']:,} (スコア可 {r['races_scored']:,} / "
          f"測れず {r['races_degenerate']:,}"
          + (f" / **結果未取込 {r['races_unsettled']:,}**" if r.get("races_unsettled") else "")
          + ")")
    if r.get("races_unsettled"):
        print("  ※結果未取込のレースは集計から除外しています(外れとして数えると回収率が"
              "0に張り付く)。払戻は開催後に配信されるので sync-all / reparse で取り込んでください")
    if not r["bets"]:
        print("  購入 0 件。閾値が高すぎるか、スナップショットが足りません")
        return 1
    print(f"  購入: {r['bets']:,} 件 / 的中 {r['hits']:,} ({r['hit_rate']:.1%})"
          + (f" / 返還 {r['refunds']:,}" if r.get("refunds") else ""))
    print(f"  投資 {r['staked']:,}円 → 払戻 {r['returned']:,}円")
    print(f"  ★回収率: {r['roi']:.4f}")
    ci = r["ci"]
    if ci:
        print(f"    95%CI [{ci['lo']:.3f}, {ci['hi']:.3f}]  "
              f"P(回収率<=1) = {ci['p_le_1']:.3f}")
        print("    ※レース単位のブートストラップ(同一レース内の馬は独立でないため)")
    if args.show_bets:
        det = sorted(r.get("details") or [], key=lambda d: (d["rid"], -d["score"]))
        print(f"\n  --- 購入明細 {len(det)} 点 ---")
        print("  レース            馬番  スコア   実測    単勝   複勝   払戻  結果")
        for d in det:
            lead = f"T-{d['lead']}s" if d.get("lead") is not None else "  -  "
            print(f"  {d['rid']}  {d['umaban']:>3}  {d['score']:+.4f}  {lead:>7}  "
                  f"{d['tan']:>6.1f} {d['fuku']:>6.1f} {d['payout']:>6,}  {d['note']}")
        print("  ※実測=判断に使ったスナップが発走の何秒前か。配信遅れで当日これが"
              "手元にあったとは限らない(締切前の到着は flow-usable で確認する)")
    print("  ※払戻は確定複勝(パリミュチュエル)。判断時のオッズでは払われない")
    return 0



from .money_signal import POOLS as _MONEY_POOLS  # noqa: E402


def _money_cfg(args):
    from .money_signal import MoneyConfig

    return MoneyConfig(lead_seconds=args.lead_seconds, flow_minutes=args.flow_minutes,
                       pool=args.pool, threshold=getattr(args, "threshold", 0.0),
                       min_pool_growth=args.min_pool_growth, weight=args.weight)








def _cmd_flow_slice(args) -> int:
    """★flow のエッジがどの帯に偏っているかを見る。

    薄いエッジにモデルの自由度を与える前に、構造があるかを確かめる。
    """
    import time

    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .flow_signal import FlowConfig, backtest
    from .slices import AXES, slice_details

    cfg = FlowConfig(lead_seconds=args.flow_lead_seconds, flow_minutes=args.flow_minutes,
                     threshold=args.flow_threshold, source=args.flow_source,
                     min_tan_odds=getattr(args, "min_tan_odds", 0.0),
                     max_tan_odds=getattr(args, "max_tan_odds", 0.0))
    months = _month_chunks(args.d_from, args.d_to)
    details: list = []
    db = FeatureDB(load_features_config())
    t0 = time.monotonic()
    try:
        for i, (a, b) in enumerate(months, 1):
            part = backtest(db, a, b, cfg, amount=args.amount, with_ci=False)
            details += part.get("details") or []
            print(f"  [{i}/{len(months)}] {a[:6]} 明細{len(details):,}点 "
                  f"({time.monotonic() - t0:.0f}s)", end="\r", flush=True)
    finally:
        db.close()
    if not details:
        print("明細がありません(閾値が高すぎるかデータ不足)")
        return 1

    axes = [x.strip() for x in args.axes.split(",") if x.strip()]
    print(f"\n=== flow スライス ({args.d_from}〜{args.d_to}) ===")
    print(f"  {args.flow_source} / T-{args.flow_lead_seconds}s / 起点{args.flow_minutes}分 "
          f"/ 閾値 {args.flow_threshold:+.4f} / 全体 {len(details):,} 点")
    for ax in axes:
        title = "スコアの大きさ(閾値からの超過)" if ax == "score" else AXES[ax][0]
        print(f"\n  --- {title} ---")
        print(f"  {'帯':<22} {'購入':>6} {'的中率':>7} {'回収率':>8}  95%CI")
        for r in slice_details(details, ax, threshold=args.flow_threshold,
                               min_bets=args.min_bets):
            if not r["bets"]:
                continue
            ci = r.get("ci")
            ci_s = (f"[{ci['lo']:.3f}, {ci['hi']:.3f}]" if ci
                    else f"(本数 {r['bets']} は少なすぎ)")
            print(f"  {r['name']:<22} {r['bets']:>6} {r['hit_rate']:>7.1%} "
                  f"{r['roi']:>8.4f}  {ci_s}")
    print("\n  ※本数と CI を必ず一緒に見ること。片方だけ見て『この帯は強い』と")
    print("    決めるのが、最も典型的な自滅の仕方。")
    print("  ※帯を選ぶこと自体が in-sample の選択になる。良い帯が見つかったら")
    print("    **必ず別期間で確かめる**(--from/--to を分けて2回流す)。")
    return 0


def _cmd_flow_decompose(args) -> int:
    """★flow の利益が DM で説明できる部分から来ているのか、残差から来ているのかを測る。

    DM由来なら T-360s の時点で同じ判断ができ、締切直前の綱渡りをやめられる。
    残差由来なら、終盤の金は DM に無い情報で動いていると確定する。
    """
    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .mining import MiningConfig, decompose

    cfg = MiningConfig(lead_seconds=args.lead_seconds, flow_minutes=args.flow_minutes,
                       source=args.flow_source)
    db = FeatureDB(load_features_config())
    try:
        r = decompose(db, args.d_from, args.d_to, cfg,
                      quantile=args.quantile, amount=args.amount)
    finally:
        db.close()
    if not r["races_used"]:
        print("対象レースがありません")
        return 1
    print(f"=== flow の分解 ({args.d_from}〜{args.d_to}) ===")
    print(f"  {args.flow_source} / 決定 発走{args.lead_seconds}秒前 / "
          f"起点 発走{args.flow_minutes}分前 / 分位 {args.quantile}")
    print(f"  対象 {r['races_seen']:,} → 使用 {r['races_used']:,} レース"
          f" (除外 {r['skipped']:,} / 未確定 {r['unsettled']:,})")
    print(f"\n  {'内訳':<28} {'購入':>5} {'的中率':>7} {'回収率':>8}  95%CI            P(<=1)")
    for k in ("flow", "dm_part", "resid", "dm_z"):
        x = r[k]
        if not x["bets"]:
            print(f"  {x['label']:<28} {'0':>5}")
            continue
        ci = x.get("ci") or {}
        print(f"  {x['label']:<28} {x['bets']:>5} {x['hit_rate']:>7.1%} "
              f"{x['roi']:>8.4f}  [{ci.get('lo', 0):.3f}, {ci.get('hi', 0):.3f}]"
              f"   {ci.get('p_le_1', 0):.3f}")
    print("\n  読み方:")
    print("   DMで説明できる分が勝つ → T-360s の時点で同じ判断ができる。")
    print("                            **締切直前の綱渡りをやめられる**")
    print("   残差が勝つ             → 終盤の金は DM に無い情報で動いている。")
    print("                            追従を速くする以外に道は無い")
    print("   ※同じレース・同じ分位・同じ複勝決済。違うのは**使った成分だけ**。")
    return 0


def _cmd_mining_eval(args) -> int:
    """DM/TM が (1) 着順を当てるか (2) 終盤のフローを説明するか を同一レースで測る。"""
    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .mining import MiningConfig, evaluate

    cfg = MiningConfig(lead_seconds=args.lead_seconds, flow_minutes=args.flow_minutes,
                       source=args.flow_source)
    db = FeatureDB(load_features_config())
    try:
        r = evaluate(db, args.d_from, args.d_to, cfg, amount=args.amount)
    finally:
        db.close()
    if not r["races_used"]:
        print("対象レースがありません(DM/TM と時系列オッズの両方が要ります)")
        return 1
    rho = r["rho"]

    def _f(v):
        return "  --  " if v is None else f"{v:+.3f}"

    print(f"=== DM/TM 評価 ({args.d_from}〜{args.d_to}) ===")
    print(f"  対象 {r['races_seen']:,} → 使用 {r['races_used']:,} レース"
          f"(DM/TM・着順・時系列が揃ったもの)")
    print("\n  --- 順位相関(レース内, 平均) ---")
    print(f"  DM   ↔ 着順     {_f(rho['dm_finish'])}   ★これが市場を明確に上回るなら"
          "**結果の漏れ**を疑う")
    print(f"  TM   ↔ 着順     {_f(rho['tm_finish'])}")
    print(f"  市場 ↔ 着順     {_f(rho['mkt_finish'])}   (発走{args.lead_seconds}秒前の単勝オッズ)")
    print(f"\n  DM   ↔ flow     {_f(rho['dm_flow'])}   ★終盤の金が DM の方向へ動いているか")
    print(f"  TM   ↔ flow     {_f(rho['tm_flow'])}")
    print(f"  DM   ↔ 市場     {_f(rho['dm_mkt'])}   (高いほど市場に織り込み済み)")

    b = r["dm_top1"]
    print(f"\n  --- DM 最上位(予想タイム最小)の複勝を1点買い ---")
    if b["bets"]:
        ci = b.get("ci") or {}
        print(f"  購入 {b['bets']:,} / 的中 {b['hits']:,} ({b['hit_rate']:.1%}) "
              f"回収率 {b['roi']:.4f}  [{ci.get('lo', 0):.3f}, {ci.get('hi', 0):.3f}]"
              f"  P(<=1)={ci.get('p_le_1', 0):.3f}")
    print("\n  読み方:")
    print("   DM↔flow が高い   → 終盤の金は DM が見ているものを見ている。")
    print("                      同じ判断を**数分早くできる**(執行が楽になる)")
    print("   DM↔flow が低い   → 終盤の金は DM に無い情報で動いている。")
    print("                      追従を速くする以外に道は無い")
    print("   ★nl_dm は RACE蓄積版(make_date がレース日の数日後)。中身は事前予想だが、")
    print("     **前日版か直前版かは区別できない**。実戦で使うなら速報系の取り込みが要る。")
    return 0


def _cmd_flow_horizons(args) -> int:
    """★同一レースでホライズン別＋組み合わせを比較する。

    別々に走らせた数字を並べると、信号の差とレース構成の差が混ざる。
    """
    import sys

    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .horizons import HorizonConfig, evaluate

    origins = [int(x) for x in args.origins.split(",") if x.strip()]
    cfg = HorizonConfig(source=args.flow_source, lead_seconds=args.lead_seconds,
                        origins=origins, quantile=args.quantile)

    def _p(i, n):
        print(f"  {i}/{n} レース...", end="\r", file=sys.stderr, flush=True)

    db = FeatureDB(load_features_config())
    try:
        r = evaluate(db, args.d_from, args.d_to, cfg, amount=args.amount, progress=_p)
    finally:
        db.close()

    print(f"\n=== ホライズン比較 ({args.d_from}〜{args.d_to}) ===")
    print(f"  {args.flow_source} / 決定時点 発走{args.lead_seconds}秒前 / 分位 {args.quantile}")
    print(f"  対象 {r['races_seen']:,} → **全ホライズンで測れて決済済み "
          f"{r['races_used']:,} レース** "
          f"(測れず {r['no_score']:,} / 未確定 {r['unsettled']:,})")
    if not r["races_used"]:
        print("  共通のレースがありません")
        return 1

    def _line(x, thr=None):
        if not x["bets"]:
            print(f"  {x['label']:<28} {'0':>5}")
            return
        ci = x.get("ci") or {}
        t = f"{thr:+.4f}" if thr is not None else "     -"
        print(f"  {x['label']:<28} {x['bets']:>5} {x['hit_rate']:>7.1%} "
              f"{x['roi']:>8.4f}  [{ci.get('lo', 0):.3f}, {ci.get('hi', 0):.3f}]"
              f"   {ci.get('p_le_1', 0):.3f}  {t}")

    print(f"\n  {'設定':<28} {'購入':>5} {'的中率':>7} {'回収率':>8}  95%CI"
          f"            P(<=1)  閾値")
    for cut in origins:
        _line(r["per_horizon"][cut], r["thresholds"][cut])
    print("  " + "-" * 76)
    for k in ("all", "any", "combo"):
        _line(r[k])
    print("\n  ※同じレース・同じ分位・同じ複勝決済。違うのは**使った価格経路だけ**。")
    print("  ※AND は本数が減るぶん CI が広がる。本数と回収率を一緒に見ること。")
    return 0


def _cmd_money_vs_flow(args) -> int:
    """★同一レースで馬連の板と単勝シェアを戦わせる。

    別々のコマンドの数字を並べると、信号の差とレース構成の差が混ざる。
    """
    import sys

    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .flow_signal import FlowConfig
    from .money_signal import head_to_head

    def _p(i, n):
        print(f"  {i}/{n} レース...", end="\r", file=sys.stderr, flush=True)

    fcfg = FlowConfig(lead_seconds=args.lead_seconds, flow_minutes=args.flow_minutes,
                      source=args.flow_source)
    db = FeatureDB(load_features_config())
    try:
        r = head_to_head(db, args.d_from, args.d_to, _money_cfg(args), fcfg,
                         quantile=args.quantile, amount=args.amount, progress=_p)
    finally:
        db.close()
    print(f"\n=== 同一レース対決 ({args.d_from}〜{args.d_to}) ===")
    print(f"  決定時点 発走{args.lead_seconds}秒前 / 起点 発走{args.flow_minutes}分前 / "
          f"分位 {args.quantile}")
    print(f"  対象 {r['races_seen']} レース → **両方がスコアを作れて決済済み "
          f"{r['races_used']} レース**")
    print(f"    (馬連のみ {r['money_only']} / flowのみ {r['flow_only']} / "
          f"どちらも不可 {r['neither']} / 未確定 {r['unsettled']})")
    if not r["races_used"]:
        print("  共通のレースがありません")
        return 1
    print(f"\n  {'信号':<28} {'購入':>5} {'的中率':>7} {'回収率':>8}  95%CI            P(<=1)")
    for k in ("money", "flow"):
        x = r[k]
        if not x["bets"]:
            print(f"  {x['label']:<28} {'0':>5}  (閾値 {x['threshold']})")
            continue
        ci = x.get("ci") or {}
        print(f"  {x['label']:<28} {x['bets']:>5} {x['hit_rate']:>7.1%} "
              f"{x['roi']:>8.4f}  [{ci.get('lo', 0):.3f}, {ci.get('hi', 0):.3f}]"
              f"   {ci.get('p_le_1', 0):.3f}")
    print(f"\n  --- 重なり: 両方 {r['overlap']} / どちらか {r['union']} ---")
    for k in ("both", "either", "money_not_flow", "flow_not_money"):
        x = r[k]
        if not x["bets"]:
            print(f"  {x['label']:<28} {'0':>5}")
            continue
        ci = x.get("ci") or {}
        print(f"  {x['label']:<28} {x['bets']:>5} {x['hit_rate']:>7.1%} "
              f"{x['roi']:>8.4f}  [{ci.get('lo', 0):.3f}, {ci.get('hi', 0):.3f}]"
              f"   {ci.get('p_le_1', 0):.3f}")
    print("\n  ※同じレース・同じ分位・同じ複勝決済。違うのは**信号だけ**。")
    print("  ※AND は本数が減るぶん CI が広がる。本数と回収率を一緒に見ること。")
    return 0


def _cmd_money_grid(args) -> int:
    """発走直前のスナップショット格子。どのリードが**そもそも測れるか**を見る。"""
    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .money_signal import snapshot_grid

    db = FeatureDB(load_features_config())
    try:
        rows = snapshot_grid(db, args.d_from, args.d_to, args.pool, args.max_lead)
    finally:
        db.close()
    if not rows:
        print("スナップショットがありません")
        return 1
    top = max(r["races"] for r in rows)
    print(f"=== 発走直前の格子 {args.pool} ({args.d_from}〜{args.d_to}) ===")
    print("  T-秒   レース数")
    for r in rows:
        bar = "#" * int(30 * r["races"] / top)
        print(f"  {r['lead_sec']:>5}  {r['races']:>6}  {bar}")
    print("\n  ※ 決定時点 T-L と起点 T-F の**両方**に行が要る。間が空いている区間を"
          "\n    指定すると late と early が同じスナップを指し、そのレースは丸ごと落ちる。")
    return 0


def _cmd_money_threshold(args) -> int:
    """金額フローの絶対閾値。flow-threshold と同じ規則(fit 期間の上側分位)。"""
    from datetime import date as _date, timedelta as _td

    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .money_signal import threshold_from
    from .race_day import day_races

    d0 = _date(int(args.d_from[:4]), int(args.d_from[4:6]), int(args.d_from[6:8]))
    d1 = _date(int(args.d_to[:4]), int(args.d_to[4:6]), int(args.d_to[6:8]))
    db = FeatureDB(load_features_config())
    try:
        races, d = [], d0
        while d <= d1:
            races += [r for r, _h in day_races(db, d.strftime("%Y%m%d"))]
            d += _td(days=1)
        if not races:
            print(f"{args.d_from}〜{args.d_to}: 対象レースがありません")
            return 1
        res = threshold_from(db, races, _money_cfg(args), args.quantile)
    finally:
        db.close()
    if res["threshold"] is None:
        print("スコアを1本も作れませんでした(スナップショット不足)")
        return 1
    print(f"=== late money 絶対閾値 ({args.d_from}〜{args.d_to}) ===")
    print(f"  条件: {args.pool}/{args.weight} / 決定時点 発走{args.lead_seconds}秒前 / "
          f"起点 発走{args.flow_minutes}分前 / 分位 {args.quantile} / "
          f"プール増分下限 {args.min_pool_growth:.1%}")
    print(f"  対象: {res['races']} レース / {res['n']:,} 本"
          f" (実効窓ズレで除外 {res.get('races_skewed_window', 0)} レース)")
    print(f"  閾値: {res['threshold']:+.4f}  (>=閾値 {res['n_above']:,} 本 = "
          f"{res['n_above'] / res['n']:.1%})")
    print(f"\n  → hro-ops money-backtest --from {args.d_from} --to {args.d_to} "
          f"--pool {args.pool} --weight {args.weight} --lead-seconds {args.lead_seconds} "
          f"--flow-minutes {args.flow_minutes} --threshold {res['threshold']:.4f}")
    return 0


def _cmd_money_backtest(args) -> int:
    """金額フローで複勝を買った場合の期間回収率。

    ★券種は複勝に固定。信号源を変えた効果だけを見るため、決済まで flow_tan と同じにする。
    """
    import sys

    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .money_signal import backtest

    def _p(i, n):
        print(f"  {i}/{n} レース...", end="\r", file=sys.stderr, flush=True)

    db = FeatureDB(load_features_config())
    try:
        rep = backtest(db, args.d_from, args.d_to, _money_cfg(args),
                       amount=args.amount, progress=_p)
    finally:
        db.close()
    print(f"\n=== late money 複勝 回収率 ({args.d_from}〜{args.d_to}) ===")
    print(f"  条件: {args.pool}/{args.weight} / 決定時点 発走{args.lead_seconds}秒前 / "
          f"起点 発走{args.flow_minutes}分前 / 閾値 {args.threshold:+.4f} / "
          f"プール増分下限 {args.min_pool_growth:.1%}")
    print(f"  レース: {rep['races']} "
          f"(スコア可 {rep['races_scored']} / 窓ズレ {rep.get('races_skewed_window', 0)}"
          f" / 結果未取込 {rep.get('races_unsettled', 0)})")
    if not rep["bets"]:
        print("  購入 0 件。閾値が高すぎるか、スナップショットが足りません")
        return 0
    print(f"  購入: {rep['bets']:,} 件 / 的中 {rep['hits']:,} ({rep['hit_rate']:.1%})"
          + (f" / 返還 {rep['refunds']}" if rep.get("refunds") else ""))
    print(f"  投資 {rep['staked']:,}円 → 払戻 {rep['returned']:,}円")
    print(f"  ★回収率: {rep['roi']:.4f}")
    ci = rep.get("ci")
    if ci:
        print(f"    95%CI [{ci['lo']:.3f}, {ci['hi']:.3f}]  "
              f"P(回収率<=1) = {ci['p_le_1']:.3f}")
        print("    ※レース単位のブートストラップ(同一レース内の馬は独立でないため)")
    if args.show_bets and rep.get("details"):
        print(f"\n  --- 購入明細 {len(rep['details'])} 点 ---")
        print("  レース            馬番  スコア   T-秒  プール増  払戻  結果")
        for d in rep["details"]:
            print(f"  {d['rid']}   {d['umaban']}  {d['score']:+.4f}  "
                  f"{str(d['lead']):>5}  {d['growth']:7.1%}  {d['payout']:>5}  {d['note']}")
    return 0


def _cmd_netkeiba_compare(args) -> int:
    """netkeiba の秒単位の動きが本物か(公式の分更新の補間でないか)を判定する。"""
    from hro_features.config import load_config as load_features_config
    from hro_features.db import FeatureDB

    from .flow_signal import netkeiba_compare

    db = FeatureDB(load_features_config())
    try:
        rows = netkeiba_compare(db, args.date)
    finally:
        db.close()
    if not rows:
        print(f"{args.date}: 突き合わせられる行がありません"
              "(netkeiba 収集と速報ポーリングの両方が同じ分で必要)")
        return 1

    n = sum(r["n"] for r in rows)
    agree = sum(r["agree"] for r in rows)
    multi = sum(1 for r in rows if (r["nk_snaps"] or 0) > 1)
    moved = sum(1 for r in rows if (r["nk_pairs"] or 0) > (r["horses"] or 0))
    snaps = sum(r["nk_snaps"] or 0 for r in rows)
    print(f"=== {args.date} netkeiba × 公式速報 ===")
    print(f"  突合: {n:,} 点 / 同じ分で値が一致 {agree:,} ({agree / n:.1%})")
    print(f"  公式の発表分: {len(rows):,}  netkeiba のスナップ: {snaps:,} "
          f"(1分あたり {snaps / len(rows):.2f} 点)")
    print(f"  1分に2点以上あった分: {multi:,} ({multi / len(rows):.1%})")
    print(f"  ★馬ごとに値が動いた分: {moved:,} ({moved / len(rows):.1%}) "
          f"← これが『公式より細かい』の実体")
    print("\n  場  R  発表時刻  突合 nk点数 頭数 馬×値 公式値数  一致   最初〜最後")
    for r in rows[:40]:
        print(f"  {r['jyo_cd']} {r['race_num']}  {r['minute_key']}  {r['n']:>4} "
              f"{r['nk_snaps']:>5} {r['horses']:>4} {r['nk_pairs']:>5} "
              f"{r['jv_values']:>7}  {r['agree']:>4}   {r['first_at']}〜{r['last_at']}")
    print("\n  読み方:")
    print("   馬×値 > 頭数           → 分の途中で値が動いている(netkeiba の方が細かい)")
    print("   馬×値 = 頭数           → 分内は1値のみ(公式と同じ粒度。足す意味なし)")
    print("   ★一致率は netkeiba が1分に2点あると**上限が約50%**になる。")
    print("     片方だけが公式の発表時刻と重なるため。低い=外れ、ではない。")
    print("   一致が極端に低い(<20%) → 別物を見ている。補間か予想オッズを掴んでいる疑い")
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

    p_fd = sub.add_parser("flow-debug", help="flow の発注が出ない理由を切り分ける(発注しない)")
    p_fd.add_argument("--race-id", required=True, help="16桁 YYYYMMDDJJKKNNRR")
    p_fd.add_argument("--flow-threshold", type=float, default=0.0)
    p_fd.add_argument("--flow-lead-seconds", type=int, default=60)
    p_fd.add_argument("--flow-minutes", type=int, default=6)
    p_fd.add_argument("--flow-source", choices=("ts", "sokuho", "netkeiba"), default="ts")
    p_fd.add_argument("--max-odds", type=float, default=None)
    p_fd.set_defaults(func=_cmd_flow_debug)

    p_fc = sub.add_parser("flow-coverage",
                          help="直前オッズが締切に間に合って取れているかを開催日単位で確認")
    p_fc.add_argument("--date", default=None, help="YYYYMMDD(既定 当日)")
    p_fc.add_argument("--flow-lead-seconds", type=int, default=60)
    p_fc.add_argument("--flow-minutes", type=int, default=6)
    p_fc.set_defaults(func=_cmd_flow_coverage)

    p_ls = sub.add_parser("flow-lead-scan",
                          help="決定時点を早めても信号が保たれるかを測る(締切前に買えるか)")
    p_ls.add_argument("--date", default=None, help="YYYYMMDD(既定 当日)")
    p_ls.add_argument("--leads", default="60,75,90,120,180", help="試す決定時点(秒)をカンマ区切り")
    p_ls.add_argument("--flow-minutes", type=int, default=6)
    p_ls.add_argument("--flow-threshold", type=float, default=0.2802)
    p_ls.add_argument("--flow-source", choices=("ts", "sokuho", "netkeiba"), default="sokuho",
                      help="早い時点を測る側。速報(10秒ポーリング)なら締切前の任意時刻が取れる")
    p_ls.add_argument("--ref-source", choices=("ts", "sokuho", "netkeiba"), default="ts",
                      help="基準の側(検証で使った公式時系列)")
    p_ls.add_argument("--ref-lead", type=int, default=60, help="基準の決定時点(秒)")
    p_ls.set_defaults(func=_cmd_flow_lead_scan)

    p_us = sub.add_parser("flow-usable",
                          help="締切直前に判断するとき手元にある最新オッズが何秒前のものかを測る")
    p_us.add_argument("--date", default=None, help="YYYYMMDD(既定 当日)")
    p_us.add_argument("--deadline-seconds", type=int, default=60,
                      help="発売締切=発走−これ秒(実測60)")
    p_us.add_argument("--margin-seconds", type=int, default=10,
                      help="締切のこれ秒前に判断する(投票所要は実測2秒程度)")
    p_us.add_argument("--flow-source", choices=("ts", "sokuho", "netkeiba"), default="sokuho")
    p_us.set_defaults(func=_cmd_flow_usable)

    p_th = sub.add_parser("flow-threshold",
                          help="使う設定(信号源・決定時点)と同じ条件で絶対閾値を取り直す")
    p_th.add_argument("--from", dest="d_from", required=True, help="YYYYMMDD")
    p_th.add_argument("--to", dest="d_to", required=True, help="YYYYMMDD")
    p_th.add_argument("--quantile", type=float, default=0.95)
    p_th.add_argument("--flow-lead-seconds", type=int, default=120)
    p_th.add_argument("--leads", default=None,
                      help="複数のリードをまとめて取る(カンマ区切り, 例 120,180,240)。"
                           "配信遅れで決定時点がレースごとに変わるため、リード別に閾値が要る")
    p_th.add_argument("--flow-minutes", type=int, default=6)
    p_th.add_argument("--flow-source", choices=("ts", "sokuho", "netkeiba"), default="sokuho")
    p_th.set_defaults(func=_cmd_flow_threshold)

    p_bt = sub.add_parser("flow-backtest",
                          help="flow_tan(複勝)の期間回収率。モデルも候補CSVも使わない")
    p_bt.add_argument("--from", dest="d_from", required=True, help="YYYYMMDD")
    p_bt.add_argument("--to", dest="d_to", required=True, help="YYYYMMDD")
    p_bt.add_argument("--flow-threshold", type=float, required=True,
                      help="この設定で flow-threshold を取り直した値を渡すこと")
    p_bt.add_argument("--flow-lead-seconds", type=int, default=360)
    p_bt.add_argument("--flow-minutes", type=int, default=11)
    p_bt.add_argument("--flow-source", choices=("ts", "sokuho", "netkeiba"), default="ts")
    p_bt.add_argument("--max-odds", type=float, default=None, help="単勝オッズ上限(既定 無し)")
    p_bt.add_argument("--min-tan-odds", type=float, default=0.0,
                      help="決定時点の単勝オッズ下限。回収率が帯で大きく違う")
    p_bt.add_argument("--max-tan-odds", type=float, default=0.0, help="同 上限")
    p_bt.add_argument("--amount", type=int, default=100)
    p_bt.add_argument("--show-bets", action="store_true",
                      help="購入を1点ずつ表示する(本数が少ない日の目視確認用)")
    p_bt.set_defaults(func=_cmd_flow_backtest)

    p_fs = sub.add_parser("flow-slice",
                          help="★flow のエッジがどの帯に偏っているかを見る")
    p_fs.add_argument("--from", dest="d_from", required=True)
    p_fs.add_argument("--to", dest="d_to", required=True)
    p_fs.add_argument("--flow-threshold", type=float, required=True)
    p_fs.add_argument("--flow-lead-seconds", type=int, default=60)
    p_fs.add_argument("--flow-minutes", type=int, default=6)
    p_fs.add_argument("--flow-source", choices=("ts", "sokuho", "netkeiba"), default="ts")
    p_fs.add_argument("--axes", default="tan,zogen,ninki,track",
                      help="軸(カンマ区切り): tan,fuku,n,jyo,month,lead,score,"
                           "zogen(馬体重増減),ninki(人気),waku(枠),kyori,track(芝ダ)")
    p_fs.add_argument("--min-bets", type=int, default=30, help="CI を出す最低本数")
    p_fs.add_argument("--min-tan-odds", type=float, default=0.0)
    p_fs.add_argument("--max-tan-odds", type=float, default=0.0)
    p_fs.add_argument("--amount", type=int, default=100)
    p_fs.set_defaults(func=_cmd_flow_slice)

    p_fd2 = sub.add_parser("flow-decompose",
                           help="★flow の利益が DM由来か残差由来かを分解して測る")
    p_fd2.add_argument("--from", dest="d_from", required=True)
    p_fd2.add_argument("--to", dest="d_to", required=True)
    p_fd2.add_argument("--flow-source", choices=("ts", "sokuho"), default="ts")
    p_fd2.add_argument("--lead-seconds", type=int, default=60)
    p_fd2.add_argument("--flow-minutes", type=int, default=6)
    p_fd2.add_argument("--quantile", type=float, default=0.95)
    p_fd2.add_argument("--amount", type=int, default=100)
    p_fd2.set_defaults(func=_cmd_flow_decompose)

    p_me = sub.add_parser("mining-eval",
                          help="JRA-VAN の DM/TM 予想を評価(着順を当てるか/フローを説明するか)")
    p_me.add_argument("--from", dest="d_from", required=True)
    p_me.add_argument("--to", dest="d_to", required=True)
    p_me.add_argument("--flow-source", choices=("ts", "sokuho"), default="ts")
    p_me.add_argument("--lead-seconds", type=int, default=60)
    p_me.add_argument("--flow-minutes", type=int, default=6)
    p_me.add_argument("--amount", type=int, default=100)
    p_me.set_defaults(func=_cmd_mining_eval)

    p_fh = sub.add_parser("flow-horizons",
                          help="★同一レースでホライズン別＋組み合わせを比較する")
    p_fh.add_argument("--from", dest="d_from", required=True)
    p_fh.add_argument("--to", dest="d_to", required=True)
    p_fh.add_argument("--flow-source", choices=("ts", "sokuho", "netkeiba"), default="ts")
    p_fh.add_argument("--lead-seconds", type=int, default=60, help="決定時点(発走-これ秒)")
    p_fh.add_argument("--origins", default="120,180,360,600,900",
                      help="起点(発走-これ秒)をカンマ区切りで。ts は発走近傍が 0/60/360 秒"
                           "しか無いので 120/180 は 360 に落ちる点に注意")
    p_fh.add_argument("--quantile", type=float, default=0.95)
    p_fh.add_argument("--amount", type=int, default=100)
    p_fh.set_defaults(func=_cmd_flow_horizons)

    p_mv = sub.add_parser("money-vs-flow",
                          help="★同一レースで馬連の板と単勝シェアを比べる")
    p_mv.add_argument("--from", dest="d_from", required=True)
    p_mv.add_argument("--to", dest="d_to", required=True)
    p_mv.add_argument("--pool", choices=tuple(_MONEY_POOLS), default="umaren")
    p_mv.add_argument("--weight", choices=("money", "share"), default="share")
    p_mv.add_argument("--flow-source", choices=("ts", "sokuho", "netkeiba"), default="ts")
    p_mv.add_argument("--lead-seconds", type=int, default=60)
    p_mv.add_argument("--flow-minutes", type=int, default=6)
    p_mv.add_argument("--min-pool-growth", type=float, default=0.005)
    p_mv.add_argument("--quantile", type=float, default=0.95)
    p_mv.add_argument("--amount", type=int, default=100)
    p_mv.set_defaults(func=_cmd_money_vs_flow)

    p_mg = sub.add_parser("money-grid",
                          help="発走直前のスナップショット格子(どのリードが測れるか)")
    p_mg.add_argument("--from", dest="d_from", required=True)
    p_mg.add_argument("--to", dest="d_to", required=True)
    p_mg.add_argument("--pool", choices=tuple(_MONEY_POOLS), default="umaren")
    p_mg.add_argument("--max-lead", type=int, default=900)
    p_mg.set_defaults(func=_cmd_money_grid)

    p_mt = sub.add_parser("money-threshold",
                          help="late money(金額フロー)の絶対閾値を取る")
    p_mb = sub.add_parser("money-backtest",
                          help="late money で複勝を買った場合の期間回収率")
    for q in (p_mt, p_mb):
        q.add_argument("--from", dest="d_from", required=True, help="YYYYMMDD")
        q.add_argument("--to", dest="d_to", required=True, help="YYYYMMDD")
        q.add_argument("--pool", choices=tuple(_MONEY_POOLS), default="umaren",
                       help="金の動きを読むプール。umaren=ts_o2(0B42で過去分あり)")
        q.add_argument("--lead-seconds", type=int, default=60)
        q.add_argument("--flow-minutes", type=int, default=6)
        q.add_argument("--weight", choices=("money", "share"), default="money",
                       help="money=入ってきた金額の配分(票数が要る) / "
                            "share=シェアの変化(オッズだけで計算でき netkeiba から取れる)")
        q.add_argument("--min-pool-growth", type=float, default=0.005,
                       help="窓の間にプールがこの割合以上増えたレースだけ使う"
                            "(増えていない窓の ΔM は雑音)")
    p_mt.add_argument("--quantile", type=float, default=0.95)
    p_mt.set_defaults(func=_cmd_money_threshold)
    p_mb.add_argument("--threshold", type=float, required=True,
                      help="この設定で money-threshold を取り直した値を渡すこと")
    p_mb.add_argument("--amount", type=int, default=100)
    p_mb.add_argument("--show-bets", action="store_true")
    p_mb.set_defaults(func=_cmd_money_backtest)

    p_nk = sub.add_parser("netkeiba-compare",
                          help="netkeiba と公式速報を突き合わせ、秒単位の動きが本物か見る")
    p_nk.add_argument("--date", required=True, help="YYYYMMDD")
    p_nk.set_defaults(func=_cmd_netkeiba_compare)

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

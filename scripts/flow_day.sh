#!/usr/bin/env bash
# =============================================================================
# flow 戦略の開催日ランナー(VM側)。モデルも特徴量も使わない。
#   単勝プールの締切直前の資金移動(flow_tan)で複勝を選ぶ。検証: docs/2026-09_flow_signal.md
#
# 前提(Windows/JV-Link機で常駐): poetry run hro-synchronizer poll-odds   ※JV-Linkは1台1プロセス
# 環境変数(agent が UI の設定から渡す):
#   DATE / FLOW_THRESHOLD / FLOW_THRESHOLDS(リード別JSON) / FLOW_SOURCE(sokuho|ts)
#   FLOW_LEAD(120) / FLOW_MIN(6)
#     ★発表時刻は分刻みなので使えるリードは60秒の倍数。締切30秒前(発走−90s)に投票を
#       始めるなら、その時点の最新スナップは 発走−120s。ts(0B41)は発走近傍が 0/60/360秒
#       しか無く 120 指定でも 360 に落ちるため、既定は sokuho(自前10秒ポーリング)。
#   LEAD_SECONDS(投票を開始する時刻=発走−これ秒)。運用上の基準は「締切の何秒前か」で、
#     締切=発走−60秒なので 締切30秒前=90 / 締切15秒前=75。agent が UI から逆算して渡す。
#   DEADLINE_LEAD(締切=発走−これ秒。IPAT実測60)
#   FLAT_AMOUNT(100) / MODE(paper|live) / NOWAIT / DAILY_BUDGET
#   live のみ: CONFIRM_LIVE=1 / MAX_PER_ORDER / MAX_PER_DAY / RECIPE(既定 ~/ipat_recipe.json)
# =============================================================================
set -euo pipefail
OPS="${OPS:-$HOME/hro/hro-operations}"
DATE="${DATE:-$(TZ=Asia/Tokyo date +%Y%m%d)}"
MODE="${MODE:-paper}"
ARGS=(--date "$DATE" --strategy flow
      --flow-threshold "${FLOW_THRESHOLD:-0.2802}" --flow-source "${FLOW_SOURCE:-sokuho}"
      --flow-lead-seconds "${FLOW_LEAD:-120}" --flow-minutes "${FLOW_MIN:-6}"
      --flat-amount "${FLAT_AMOUNT:-100}" --lead-seconds "${LEAD_SECONDS:-70}"
      --deadline-lead-seconds "${DEADLINE_LEAD:-60}"
      --mode "$MODE")
# リード別の閾値。実測リードに対応する値が無いレースは run-day 側が見送る。
# 「T-120s が間に合ったレースだけ買う」運用はこれで実現する。
[ -n "${FLOW_THRESHOLDS:-}" ] && ARGS+=(--flow-thresholds "$FLOW_THRESHOLDS")
[ -n "${NOWAIT:-}" ] && ARGS+=(--no-wait)
[ -n "${DAILY_BUDGET:-}" ] && ARGS+=(--daily-budget "$DAILY_BUDGET")
if [ "$MODE" = "live" ]; then
  [ "${CONFIRM_LIVE:-}" = "1" ] || { echo "ERROR: live には CONFIRM_LIVE=1 が必要"; exit 2; }
  RECIPE="${RECIPE:-$HOME/ipat_recipe.json}"
  [ -f "$RECIPE" ] || { echo "ERROR: レシピが無い: $RECIPE (hro-buyer ipat show-recipe → dry-vote → verified)"; exit 2; }
  ARGS+=(--confirm-live --ipat-recipe "$RECIPE"
         --max-amount-per-order "${MAX_PER_ORDER:?}" --max-amount-per-day "${MAX_PER_DAY:?}"
         --ipat-screenshot-dir "$HOME/ipat_shots")
fi
ACT="${LEAD_SECONDS:-70}"; DL="${DEADLINE_LEAD:-60}"
echo "=== flow day-runner date=$DATE mode=$MODE thr=${FLOW_THRESHOLD:-0.2802} src=${FLOW_SOURCE:-sokuho} 判断=T-${FLOW_LEAD:-120}s 起点=T-${FLOW_MIN:-6}m 投票開始=T-${ACT}s(締切T-${DL}sの$((ACT-DL))秒前) ==="
cd "$OPS"
exec poetry run hro-ops run-day "${ARGS[@]}"

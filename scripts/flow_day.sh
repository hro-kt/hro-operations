#!/usr/bin/env bash
# =============================================================================
# flow 戦略の開催日ランナー(VM側)。モデルも特徴量も使わない。
#   単勝プールの締切直前の資金移動(flow_tan)で複勝を選ぶ。検証: docs/2026-09_flow_signal.md
#
# 前提(Windows/JV-Link機で常駐): poetry run hro-synchronizer poll-odds   ※JV-Linkは1台1プロセス
# 環境変数(agent が UI の設定から渡す):
#   DATE / FLOW_THRESHOLD(既定0.2802) / FLOW_SOURCE(ts|sokuho) / FLOW_LEAD(60) / FLOW_MIN(6)
#   LEAD_SECONDS(判断を実行する時刻=発走−これ秒。締切=発走−60秒より前に投票を終える必要がある)
#   FLAT_AMOUNT(100) / MODE(paper|live) / NOWAIT / DAILY_BUDGET
#   live のみ: CONFIRM_LIVE=1 / MAX_PER_ORDER / MAX_PER_DAY / RECIPE(既定 ~/ipat_recipe.json)
# =============================================================================
set -euo pipefail
OPS="${OPS:-$HOME/hro/hro-operations}"
DATE="${DATE:-$(TZ=Asia/Tokyo date +%Y%m%d)}"
MODE="${MODE:-paper}"
ARGS=(--date "$DATE" --strategy flow
      --flow-threshold "${FLOW_THRESHOLD:-0.2802}" --flow-source "${FLOW_SOURCE:-ts}"
      --flow-lead-seconds "${FLOW_LEAD:-60}" --flow-minutes "${FLOW_MIN:-6}"
      --flat-amount "${FLAT_AMOUNT:-100}" --lead-seconds "${LEAD_SECONDS:-30}"
      --mode "$MODE")
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
echo "=== flow day-runner date=$DATE mode=$MODE thr=${FLOW_THRESHOLD:-0.2802} src=${FLOW_SOURCE:-ts} T-${FLOW_LEAD:-60}s/T-${FLOW_MIN:-6}m act=T-${LEAD_SECONDS:-30}s ==="
cd "$OPS"
exec poetry run hro-ops run-day "${ARGS[@]}"

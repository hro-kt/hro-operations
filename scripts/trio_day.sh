#!/usr/bin/env bash
# =============================================================================
# 開催日(VM側)を1コマンドで立ち上げる:
#   [1] component MV を前日まで refresh(as-of履歴)
#   [2] 直前TYB poll をバックグラウンド常駐(発走前にpadokuが埋まる)
#   [3] paper day-runner を起動(trio × er_cal[1.7,2.0) × 較正 × 分数Kelly)
#
# ★別途 Windows(JV-Link機)で先に/並行して(このスクリプトの前提):
#     # 前日までの結果を取得
#     poetry run hro-synchronizer sync-all
#     # 当日の出走表 + オッズ(三連複含む 0B30)を常駐取得
#     ODDS_SPEC=0B30 poetry run hro-synchronizer --date <YYYYMMDD> run
#
# 使い方:  bash trio_day.sh
#   環境変数で上書き可: DATE(既定=今日JST) / MODELS / BANKROLL
# 停止: Ctrl-C で day-runner 停止 → 表示される PID で TYB poll を kill。
# =============================================================================
set -euo pipefail

FEAT="${FEAT:-$HOME/hro/hro-features}"
OPS="${OPS:-$HOME/hro/hro-operations}"
SYNC="${SYNC:-$HOME/hro/hro-synchronizer}"
MODELS="${MODELS:-$HOME/models}"
DATE="${DATE:-$(TZ=Asia/Tokyo date +%Y%m%d)}"
BANKROLL="${BANKROLL:-100000}"
# 日次予算。admin UI で日別設定があれば agent が渡す。空なら run-day 既定(HRO_DAILY_BUDGET)。
DAILY_BUDGET="${DAILY_BUDGET:-}"

# ★productionize.sh と同一でなければ schema-hash 不一致で fail-fast する。必ず一致させる。
export PROD="HRO_ABLATE_SED=1 HRO_ABLATE_RACESTRUCT=1 HRO_ABLATE_SEASON=1 HRO_ABLATE_TRIP=1 HRO_ABLATE_GROUNDLOSS=1 HRO_ABLATE_TYBODDS=1 HRO_ABLATE_PACE=1 HRO_ABLATE_TRAJ=1 HRO_ABLATE_SOS=1 HRO_ABLATE_PEDCOND=1 HRO_ABLATE_FIELDSHAPE=1"

for f in win_prod.joblib place_prod.joblib trio_calib.json; do
  [ -f "$MODELS/$f" ] || { echo "ERROR: $MODELS/$f が無い。先に productionize.sh を実行"; exit 1; }
done

echo "=== [1/3] component MV refresh (前日までのas-of履歴) ==="
cd "$FEAT" && poetry run hro-features refresh

echo "=== [2/3] 直前TYB poll をバックグラウンド起動 (date=$DATE) ==="
cd "$SYNC"
nohup poetry run python -m hro_synchronizer.jrdb_tyb_loader poll --date "$DATE" --interval 180 \
  > "$HOME/tyb_poll_$DATE.log" 2>&1 &
TYB_PID=$!
echo "  TYB poll PID=$TYB_PID  (log: ~/tyb_poll_$DATE.log)  停止は: kill $TYB_PID"

BUDGET_ARGS=()
[ -n "$DAILY_BUDGET" ] && BUDGET_ARGS=(--daily-budget "$DAILY_BUDGET")
echo "=== [3/3] paper day-runner 起動 (trio, er_cal[1.7,2.0), 較正, 分数Kelly bankroll=$BANKROLL, 日次予算=${DAILY_BUDGET:-既定}) ==="
cd "$OPS"
trap 'echo; echo "day-runner停止。TYB pollも止めます (kill $TYB_PID)"; kill '"$TYB_PID"' 2>/dev/null || true' EXIT
env $PROD poetry run hro-ops run-day \
  --date "$DATE" \
  --win-model "$MODELS/win_prod.joblib" --place-model "$MODELS/place_prod.joblib" \
  --preset trio --calib "$MODELS/trio_calib.json" \
  --bankroll "$BANKROLL" --kelly-fraction 0.25 --race-max 3000 --ticket-max 1000 --max-tickets 5 \
  "${BUDGET_ARGS[@]}"

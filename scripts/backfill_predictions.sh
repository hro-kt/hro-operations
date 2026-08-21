#!/usr/bin/env bash
# =============================================================================
# 予測ログのバックフィル(MLOps監視の土台): 過去JRAをモデルで採点し prediction_log へ。
#
# ★productionize.sh / trio_day.sh と同一の ablation env を必ず export する。
#   これが無いと現特徴schema(=353列)と本番モデル(=ablated)の schema-hash が食い違い
#   load_models が fail-fast する。学習時と同一 env でのみ正しく採点できる。
#
# 使い方(env で指定): FROM=YYYYMMDD TO=YYYYMMDD [LIMIT=N] [SOURCE=backfill] bash backfill_predictions.sh
# =============================================================================
set -euo pipefail

BT="${BT:-$HOME/hro/hro-backtest}"
MODELS="${MODELS:-$HOME/models}"
FROM="${FROM:?FROM(YYYYMMDD) が必要}"
TO="${TO:?TO(YYYYMMDD) が必要}"
SOURCE="${SOURCE:-backfill}"

# ★productionize/trio_day と完全一致させること(schema-hash 一致が必須)
export HRO_ABLATE_SED=1 HRO_ABLATE_RACESTRUCT=1 HRO_ABLATE_SEASON=1 HRO_ABLATE_TRIP=1 \
       HRO_ABLATE_GROUNDLOSS=1 HRO_ABLATE_TYBODDS=1 HRO_ABLATE_PACE=1 HRO_ABLATE_TRAJ=1 \
       HRO_ABLATE_SOS=1 HRO_ABLATE_PEDCOND=1 HRO_ABLATE_FIELDSHAPE=1

for f in win_prod.joblib place_prod.joblib; do
  [ -f "$MODELS/$f" ] || { echo "ERROR: $MODELS/$f が無い。先に productionize.sh"; exit 1; }
done

LIMIT_ARGS=()
[ -n "${LIMIT:-}" ] && LIMIT_ARGS=(--limit "$LIMIT")

echo "=== backfill-predictions $FROM..$TO source=$SOURCE ==="
cd "$BT"
poetry run hro-backtest backfill-predictions \
  --win-model "$MODELS/win_prod.joblib" --place-model "$MODELS/place_prod.joblib" \
  --from "$FROM" --to "$TO" --source "$SOURCE" "${LIMIT_ARGS[@]}"
echo "完了: prediction_log を更新しました。admin「モデル監視」で確認できます。"

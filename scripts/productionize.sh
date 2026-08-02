#!/usr/bin/env bash
# =============================================================================
# 現最良モデル(c_pure_h相当)を全期間で学習し、trio較正JSONを生成する。
# 一度実行すればOK。データが増えたら定期的に再実行して差し替える(モデル/較正の鮮度維持)。
# VM(DB接続あり)で実行。hro-predictor/hro-backtest は hro-backtest の poetry env に入っている。
#
# 使い方:  bash productionize.sh
#   環境変数で上書き可: MODELS(出力先) / VALID_FROM / TEST_FROM / CAL_FROM / CAL_TO
# =============================================================================
set -euo pipefail

BT="${BT:-$HOME/hro/hro-backtest}"
MODELS="${MODELS:-$HOME/models}"
VALID_FROM="${VALID_FROM:-20260101}"
TEST_FROM="${TEST_FROM:-20260401}"
CAL_FROM="${CAL_FROM:-20230101}"
CAL_TO="${CAL_TO:-20260607}"

# ★検証済み最良構成 c_pure_h = keyrace + TYB出来 + パドック履歴 + 応答(chokuzen無)。
#   未検証/中立だった新群(pace/traj/sos/pedcond/fieldshape)は ablate。学習=予測=較正で同一必須。
export PROD="HRO_ABLATE_SED=1 HRO_ABLATE_RACESTRUCT=1 HRO_ABLATE_SEASON=1 HRO_ABLATE_TRIP=1 HRO_ABLATE_GROUNDLOSS=1 HRO_ABLATE_TYBODDS=1 HRO_ABLATE_PACE=1 HRO_ABLATE_TRAJ=1 HRO_ABLATE_SOS=1 HRO_ABLATE_PEDCOND=1 HRO_ABLATE_FIELDSHAPE=1"

mkdir -p "$MODELS"
cd "$BT"

echo "[1/3] 単勝(y_win)モデル学習 -> $MODELS/win_prod.joblib"
env $PROD poetry run hro-predictor train --target y_win \
  --train-from 20150101 --valid-from "$VALID_FROM" --test-from "$TEST_FROM" \
  --out "$MODELS/win_prod.joblib"

echo "[2/3] 複勝(y_fukusyo)モデル学習 -> $MODELS/place_prod.joblib"
env $PROD poetry run hro-predictor train --target y_fukusyo \
  --train-from 20150101 --valid-from "$VALID_FROM" --test-from "$TEST_FROM" \
  --out "$MODELS/place_prod.joblib"

echo "[3/3] trio較正生成 -> $MODELS/trio_calib.json  (cal $CAL_FROM..$CAL_TO)"
env $PROD poetry run hro-backtest fit-trio-calib \
  --cal-from "$CAL_FROM" --cal-to "$CAL_TO" \
  --win-model "$MODELS/win_prod.joblib" --place-model "$MODELS/place_prod.joblib" \
  --out "$MODELS/trio_calib.json"

echo "完了: $MODELS/{win_prod.joblib, place_prod.joblib, trio_calib.json}"
echo "※この PROD env を trio_day.sh も使う(schema-hash一致のため)。構成を変えたら両方直すこと。"

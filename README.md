# hro-operations

**開催日オーケストレーション**。各サーバーで所定のコマンドを起動すれば、その開催日の
**前向きペーパー運用**（検証済みフラット複勝戦略 `min_er>=1.3 & min_prob>=0.15`, ¥100）が回る。
自動投票はしない（paper）。

```
Windows(JVLink機)                       Linux(計算機)
  poll-odds ──書──> ts_sokuho_o1.. (live)   hro-ops run-day
                        │                      ├─ 各レース T-30s に
   共有 PostgreSQL <────┘                      │   能力値→PL→flat¥100 place 選別(live odds)
                        ┌───────────────────── │   → paper 購入(締切/発売/実行直前ガード)
   nl_ra/nl_o*/nl_hr <──┘                      └─ 結果 results_YYYYMMDD.jsonl に追記
```

役割分担:
- **Windows**: live オッズ供給（`hro-synchronizer poll-odds`）＋当日データ同期（`sync-all`）。
- **Linux**: 発注判断＋paper 実行（`hro-ops run-day`）＋決済（`hro-buyer settle`）。
- 両者は**同一 PostgreSQL を共有**。プロセス間通信は DB 経由。

## セットアップ

```bash
cd hro-operations
poetry install -E postgres          # psycopg + 依存(backtest/buyer とその推移的依存)
```

`.env`（または環境変数）に共有 DB の接続情報。学習時と同じ特徴スキーマで動かすため、
no-SED 279 モデルなら **`HRO_ABLATE_SED=1` を付けて実行**（不一致なら load_models が fail-fast）。

## 開催日の回し方

### ① Windows（朝、JVLink 機で）
```powershell
# hro-synchronizer のディレクトリで
powershell -ExecutionPolicy Bypass -File ..\hro-operations\windows\start_race_day.ps1
```
→ `sync-all`（nl_ra に発走時刻）→ `poll-odds`（速報オッズ常駐）。**最終レース締切まで動かし続ける**。

### ②-a Linux（朝、feat_matrix を当日データで更新）
`hro-ops` が予測する前に、当日の出走馬が feat_matrix に載っている必要がある。
synchronizer 同期後に **features の MV を refresh**（既存手順）。※コマンドはプロジェクトの
features 運用手順に従う（例: `poetry run hro-features refresh` 等、環境に合わせて）。

### ②-b Linux（当日、発注ランナーを常駐起動）
```bash
cd hro-operations
export HRO_ABLATE_SED=1
D=/path/to/hro-predictor/models
# 段取り確認(発注しない): 当日レースと締切一覧
poetry run hro-ops list --win-model $D/win_prod.joblib --place-model $D/place_prod.joblib
# 本番(常駐): 各レース T-30s に flat¥100 place を paper 発注
poetry run hro-ops run-day --win-model $D/win_prod.joblib --place-model $D/place_prod.joblib
```
`run-day` は当日全レースを発走時刻順に処理し、各レースの **発走−30秒**まで待機して発注判断する常駐ループ。
1レースの失敗は握りつぶして継続。結果は `results_YYYYMMDD.jsonl` に追記。

### ③ Linux（開催後、決済）
```bash
poetry run hro-buyer settle --results results_20260719.jsonl          # 損益/ROI 表示
poetry run hro-buyer settle --results results_20260719.jsonl --write  # bet_settlements へ記録
```
`nl_hr`（払戻）と突合。idempotency_key 重複は自動排除。累積は hro-admin の損益/ROI パネルで確認。

## 検証・運用の小技

- **live スモークテスト**（poll-odds 稼働中に1レースだけ確認）:
  ```bash
  poetry run hro-ops once --win-model $D/win_prod.joblib --place-model $D/place_prod.joblib \
    --race 2026 0719 05 03 04 11
  ```
  待機せず即判断。`発注なし` なら live odds 未取得か +EV 複勝なし。
- **当日途中から起動**: `run-day --no-wait` で待機せず残りを即処理（締切超過は自動 skip）。
- **鮮度**: `--max-odds-age`（既定60s）。poll-odds は10s周期なので ts_sokuho は常に新しい。
- **常駐化**: 本番は systemd(Linux)/タスクスケジューラ(Windows)でサービス化し、
  将来 hro-admin から arm/disarm できるよう DB 制御テーブル化する拡張余地あり（設計メモ参照）。

## 主なオプション（`hro-ops run-day`）

| オプション | 既定 | 意味 |
| --- | --- | --- |
| `--date` | 当日(JST) | 対象開催日 YYYYMMDD |
| `--min-er` / `--min-prob` | 1.3 / 0.15 | 複勝の期待値/確率下限（検証済み） |
| `--flat-amount` | 100 | 1点固定額（円） |
| `--bet-types` | place | 対象券種 |
| `--lead-seconds` | 30 | 発走−これ秒に発注（T-30s） |
| `--max-odds-age` | 60 | live 鮮度上限（秒） |
| `--mode` | paper | paper（実行直前ガード有）/ dry_run |

**実投票（live）は本 MVP では未実装**。hro-buyer の live executor（IpatExecutor）を実装し、
明示的 arm＋上限を入れてから移行する。
# hro-operations

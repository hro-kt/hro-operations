# 開催日 Windows(JVLink機) 起動スクリプト。
#
# これを開催日の朝に実行しておけば、その日の live オッズ供給が回る。
#   1) 当日の JV-Data を同期(nl_ra に発走時刻 hasso_time、出走馬 nl_se 等が入る)
#   2) 速報オッズを10秒周期でポーリング(ts_sokuho_o1.. へ。締切まで開けっ放し)
#
# 使い方(hro-synchronizer のディレクトリで):
#   powershell -ExecutionPolicy Bypass -File ..\hro-operations\windows\start_race_day.ps1
#   # 別日を対象にするとき:  $env:HRO_OPS_DATE="20260719"; 同上
#
# 注意:
#   - poll-odds は常駐(Ctrl+C で停止)。最終レース締切後まで動かし続ける。
#   - Linux 側で feat_matrix を当日データで更新してから hro-ops を回すこと(README 参照)。

$ErrorActionPreference = "Stop"
$dateArg = @()
if ($env:HRO_OPS_DATE) { $dateArg = @("--date", $env:HRO_OPS_DATE) }

Write-Host "=== [1/2] sync-all (当日の RA/SE 等を取込) ==="
poetry run hro-synchronizer @dateArg sync-all

Write-Host "=== [2/2] poll-odds (速報オッズ10s周期。締切まで常駐。Ctrl+Cで停止) ==="
poetry run hro-synchronizer @dateArg poll-odds

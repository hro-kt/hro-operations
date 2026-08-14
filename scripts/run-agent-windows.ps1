# =============================================================================
# hro-ops Windows agent (resident) - PowerShell 版
# タスクスケジューラの「ログオン時」で起動する。JVLink は対話デスクトップ必須のため
# session-0 サービスにはしない。値は自分の環境に合わせて編集。パスワードを含むので ACL 注意。
#
# 手動起動:   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-agent-windows.ps1
# 単一引用符 '...' は変数展開されず、& > | 等をそのまま扱えるのでパスワード向き。
# =============================================================================

$ErrorActionPreference = 'Stop'

# --- UTF-8 コンソール(日本語ログの文字化け防止) ---
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# --- リポジトリ基点 ---
$env:HRO_HOME = 'C:\hro'

# --- DB / JRDB 認証 ---
# (別案: これらを C:\hro\hro-operations\.env に書けば、cd 後に hro_operations が自動ロードします。
#        その場合は下の $env: 代入を消してください。override=False なので env が優先されるため。)
$env:POSTGRES_HOST     = 'hro-db-prod1.postgres.database.azure.com'
$env:POSTGRES_PORT     = '5432'
$env:POSTGRES_DATABASE = 'hro'
$env:POSTGRES_USER     = 'hrouser'
$env:POSTGRES_PASSWORD = 'CHANGE_ME'
$env:POSTGRES_SSLMODE  = 'require'
$env:JRDB_USER         = 'CHANGE_ME'
$env:JRDB_PWD          = 'CHANGE_ME'
$env:SYNC_LOOKBACK_DAYS = '7'

# --- venv の python を自動判定(python.exe が無い環境は python3.exe) ---
$scripts = Join-Path $env:HRO_HOME 'hro-operations\.venv\Scripts'
$pyexe = Join-Path $scripts 'python.exe'
if (-not (Test-Path $pyexe)) { $pyexe = Join-Path $scripts 'python3.exe' }
if (-not (Test-Path $pyexe)) {
    Write-Host "[ERROR] venv python not found under $scripts"
    Write-Host "Create it: cd C:\hro\hro-operations; py -m venv .venv; .venv\Scripts\pip install `"psycopg[binary]`""
    Read-Host 'Enter を押すと終了'
    exit 1
}
Write-Host "Using PYEXE = $pyexe"

Set-Location (Join-Path $env:HRO_HOME 'hro-operations')

# --- 常駐ループ(agent が落ちても再起動) ---
while ($true) {
    & $pyexe -m hro_operations agent --server windows --interval 5
    Write-Host 'agent exited. restarting in 10s...'
    Start-Sleep -Seconds 10
}

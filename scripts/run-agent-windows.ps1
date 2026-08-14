# =============================================================================
# hro-ops Windows agent (resident) - PowerShell launcher.
# Start it from Task Scheduler "at logon". JVLink needs an INTERACTIVE desktop
# session, so do NOT run it as a session-0 service.
# ASCII-only on purpose: Windows PowerShell 5.1 reads a BOM-less .ps1 as the
# system code page (CP932 on Japanese Windows), which corrupts non-ASCII text
# and breaks parsing. Keep this file ASCII.
# Edit the CHANGE_ME values. This file holds a password -> restrict its ACL.
#
# Manual run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-agent-windows.ps1
# Single quotes '...' are literal (no expansion), so & > | in passwords are safe.
# =============================================================================

$ErrorActionPreference = 'Stop'

# --- UTF-8 console so Japanese log output is not garbled ---
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# --- Repo base ---
$env:HRO_HOME = 'C:\hro'

# --- DB / JRDB credentials ---
# (Alternative: put these in C:\hro\hro-operations\.env and delete the lines
#  below; hro_operations auto-loads .env after cd. env wins over .env, so if you
#  keep both, the values here take precedence.)
$env:POSTGRES_HOST      = 'hro-db-prod1.postgres.database.azure.com'
$env:POSTGRES_PORT      = '5432'
$env:POSTGRES_DATABASE  = 'hro'
$env:POSTGRES_USER      = 'hrouser'
$env:POSTGRES_PASSWORD  = 'CHANGE_ME'
$env:POSTGRES_SSLMODE   = 'require'
$env:JRDB_USER          = 'CHANGE_ME'
$env:JRDB_PWD           = 'CHANGE_ME'
$env:SYNC_LOOKBACK_DAYS = '7'

# --- Auto-detect venv python (python.exe or python3.exe) ---
$scripts = Join-Path $env:HRO_HOME 'hro-operations\.venv\Scripts'
$pyexe = Join-Path $scripts 'python.exe'
if (-not (Test-Path $pyexe)) { $pyexe = Join-Path $scripts 'python3.exe' }
if (-not (Test-Path $pyexe)) {
    Write-Host "[ERROR] venv python not found under $scripts"
    Write-Host 'Create it: cd C:\hro\hro-operations; py -m venv .venv; .venv\Scripts\pip install psycopg[binary]'
    Read-Host 'Press Enter to exit'
    exit 1
}
Write-Host "Using PYEXE = $pyexe"

Set-Location (Join-Path $env:HRO_HOME 'hro-operations')

# --- Resident loop (restart the agent if it exits) ---
while ($true) {
    & $pyexe -m hro_operations agent --server windows --interval 5
    Write-Host 'agent exited. restarting in 10s...'
    Start-Sleep -Seconds 10
}

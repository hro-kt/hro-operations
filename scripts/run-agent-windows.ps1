# =============================================================================
# hro-ops Windows agent (resident) - PowerShell launcher.
# Start it from Task Scheduler "at logon". JVLink needs an INTERACTIVE desktop
# session, so do NOT run it as a session-0 service.
# ASCII-only on purpose: Windows PowerShell 5.1 reads a BOM-less .ps1 as the
# system code page (CP932 on Japanese Windows), which corrupts non-ASCII text
# and breaks parsing. Keep this file ASCII.
# DO NOT put machine settings in this file: it is tracked in git and a pull
# overwrites them. Use <HRO_HOME>\hro-operations\.env instead.
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
# Prefer an already-set HRO_HOME (machine env var, or the caller's shell), so a
# `git pull` of this file never clobbers this machine's layout.
#   setx /M HRO_HOME "C:\Users\kyosuke.tomoda\Documents\hro"
if (-not $env:HRO_HOME) { $env:HRO_HOME = 'C:\hro' }

# --- DB / JRDB credentials ---
# Put the real values in <HRO_HOME>\hro-operations\.env (untracked), NOT here.
# Only fill gaps below. Never overwrite a value that is already set, otherwise a
# `git pull` of this file silently replaces the real password with CHANGE_ME
# (that happened 2026-10-24).
function Set-IfEmpty([string]$name, [string]$value) {
    if (-not $value -or $value -eq 'CHANGE_ME') { return }
    if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name))) {
        Set-Item -Path "env:$name" -Value $value
    }
}
Set-IfEmpty 'POSTGRES_HOST'      'hro-db-prod1.postgres.database.azure.com'
Set-IfEmpty 'POSTGRES_PORT'      '5432'
Set-IfEmpty 'POSTGRES_DATABASE'  'hro'
Set-IfEmpty 'POSTGRES_USER'      'hrouser'
Set-IfEmpty 'POSTGRES_SSLMODE'   'require'
Set-IfEmpty 'SYNC_LOOKBACK_DAYS' '7'

# --- Auto-detect venv python (python.exe or python3.exe) ---
$scripts = Join-Path $env:HRO_HOME 'hro-operations\.venv\Scripts'
$pyexe = Join-Path $scripts 'python.exe'
if (-not (Test-Path $pyexe)) { $pyexe = Join-Path $scripts 'python3.exe' }
if (-not (Test-Path $pyexe)) {
    Write-Host "[ERROR] venv python not found under $scripts"
    Write-Host "       HRO_HOME = $env:HRO_HOME"
    Write-Host '       If that path is wrong, HRO_HOME is unset on this machine and the'
    Write-Host '       script fell back to the default. Set it once, machine-wide:'
    Write-Host '         setx /M HRO_HOME "C:\Users\<you>\Documents\hro"'
    Write-Host '       (open a NEW shell afterwards; setx does not affect the current one)'
    Write-Host "       Or create the venv: cd $env:HRO_HOME\hro-operations; py -m venv .venv" 
    Read-Host 'Press Enter to exit'
    exit 1
}
Write-Host "Using PYEXE = $pyexe"

Set-Location (Join-Path $env:HRO_HOME 'hro-operations')

# --- Refuse to start a second agent ---------------------------------------
# With auto-logon, the "at logon" task fires on the console logon AND again
# when you connect by RDP (same user, different session). Two agents claim
# jobs from the same queue and fight: ops_agent has one row per server, so the
# heartbeat flaps between them and `takeovers` climbs. Worse, two flow runners
# can buy the same race twice (seen 2026-10-17 with two orchestrators).
# Task Scheduler's "Do not start a new instance" also covers this; keep both.
$me = $PID
$others = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='python3.exe'" |
            Where-Object { $_.ProcessId -ne $me -and
                           $_.CommandLine -match 'hro_operations.*agent.*--server\s+windows' })
if ($others.Count -gt 0) {
    Write-Host "[SKIP] agent already running (PID $($others[0].ProcessId)). Not starting a second one."
    Write-Host '       Two agents claim the same jobs and can buy the same race twice.'
    Write-Host '       If that process is dead, kill it and re-run this task.'
    Start-Sleep -Seconds 5
    exit 0
}

# --- Popup guard ----------------------------------------------------------
# JV-Link raises a modal "setup" dialog (JVOpen option 3/4). Until somebody
# clicks it, sync-all does not move, so an unattended race day stalls there.
# This watcher logs every window that appears and acts ONLY on the vetted rules
# in popup_guard.DEFAULT_RULES, plus whatever is in
# <HRO_HOME>\hro-operations\popup_rules.json (that file replaces the defaults).
# It starts in RECORD-ONLY mode on purpose: clicking a window you have not seen
# is how you lose a race day. Read the log first, then set HRO_POPUP_CLOSE=1.
$popupLog = Join-Path $env:HRO_HOME 'hro-operations\logs\popups.log'
New-Item -ItemType Directory -Force -Path (Split-Path $popupLog) | Out-Null
$popupRunning = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='python3.exe'" |
                  Where-Object { $_.ProcessId -ne $me -and $_.CommandLine -match 'popups\s+watch' })
if ($popupRunning.Count -gt 0) {
    Write-Host "[SKIP] popup guard already running (PID $($popupRunning[0].ProcessId))."
} else {
    $popupArgs = @('-m', 'hro_operations', 'popups', 'watch', '--log', $popupLog)
    if ($env:HRO_POPUP_CLOSE -eq '1') {
        $popupArgs += '--close'
        Write-Host 'Popup guard: CLOSING windows that match popup_rules.json'
    } else {
        Write-Host 'Popup guard: record-only. Set HRO_POPUP_CLOSE=1 once the rules are written.'
    }
    Write-Host "Popup guard log: $popupLog"
    Start-Process -FilePath $pyexe -ArgumentList $popupArgs -WindowStyle Minimized | Out-Null
}

# --- Resident loop (restart the agent if it exits) ---
while ($true) {
    & $pyexe -m hro_operations agent --server windows --interval 5
    Write-Host 'agent exited. restarting in 10s...'
    Start-Sleep -Seconds 10
}

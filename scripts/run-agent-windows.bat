@echo off
rem ==========================================================================
rem hro-ops Windows agent (resident). Launch via Task Scheduler "at logon".
rem JVLink needs an INTERACTIVE desktop session -> do NOT run as a session-0
rem service. Keep this file ASCII-only to avoid console mojibake.
rem Edit the CHANGE_ME values. This file holds a password -> restrict its ACL.
rem ==========================================================================

rem --- Force UTF-8 console so Japanese log output is not garbled ---
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem --- DB (managed PG reachable over VNet) ---
set POSTGRES_HOST=hro-db-prod1.postgres.database.azure.com
set POSTGRES_PORT=5432
set POSTGRES_DATABASE=hro
set POSTGRES_USER=hrouser
set POSTGRES_PASSWORD=CHANGE_ME
set POSTGRES_SSLMODE=require

rem --- repo base / JRDB credentials ---
set HRO_HOME=C:\hro
set JRDB_USER=CHANGE_ME
set JRDB_PWD=CHANGE_ME

rem --- smart-diff sync lookback days (optional) ---
set SYNC_LOOKBACK_DAYS=7

rem --- Python from the venv (no poetry needed at RUNTIME) ---
rem One-time setup (siblings ../hro-backtest, ../hro-buyer must be present):
rem   poetry config virtualenvs.in-project true  &&  poetry install
rem Otherwise find it once with:  poetry env info --path  (+ \Scripts\python.exe)
rem Auto-detect the venv interpreter (some venvs ship only python3.exe).
set PYEXE=%HRO_HOME%\hro-operations\.venv\Scripts\python.exe
if not exist "%PYEXE%" set PYEXE=%HRO_HOME%\hro-operations\.venv\Scripts\python3.exe
if not exist "%PYEXE%" (
  echo [ERROR] venv python not found under %HRO_HOME%\hro-operations\.venv\Scripts\
  echo Set PYEXE manually. Find the venv with: poetry env info --path
  pause
  exit /b 1
)
echo Using PYEXE=[%PYEXE%]

cd /d %HRO_HOME%\hro-operations
:loop
"%PYEXE%" -m hro_operations agent --server windows --interval 5
echo agent exited. restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop

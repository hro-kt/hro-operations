@echo off
setlocal EnableExtensions
rem ==========================================================================
rem hro-ops Windows agent (resident). Launch via Task Scheduler "at logon".
rem JVLink needs an INTERACTIVE desktop session -> do NOT run as a session-0
rem service. Keep this file ASCII-only to avoid console mojibake.
rem
rem IMPORTANT: every `set` uses the quoted form  set "VAR=value"  so that
rem special characters in passwords ( & ^ < > | % ( ) ) do NOT break parsing.
rem This file holds secrets -> restrict its ACL.
rem ==========================================================================

rem --- Force UTF-8 console so Japanese log output is not garbled ---
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem --- machine settings -------------------------------------------------
rem DO NOT put real values here: this file is tracked in git and a pull
rem overwrites them (that happened 2026-10-24: HRO_HOME and the DB password
rem were silently reset, and the agent would not start).
rem   Put real values in %HRO_HOME%\hro-operations\.env, and set the base once:
rem   setx /M HRO_HOME "C:\Users\kyosuke.tomoda\Documents\hro"
rem Anything already set in the environment is kept; below are only fallbacks.
if not defined HRO_HOME set "HRO_HOME=C:\hro"
if not defined POSTGRES_HOST set "POSTGRES_HOST=hro-db-prod1.postgres.database.azure.com"
if not defined POSTGRES_PORT set "POSTGRES_PORT=5432"
if not defined POSTGRES_DATABASE set "POSTGRES_DATABASE=hro"
if not defined POSTGRES_USER set "POSTGRES_USER=hrouser"
if not defined POSTGRES_SSLMODE set "POSTGRES_SSLMODE=require"

rem --- smart-diff sync lookback days (optional) ---
if not defined SYNC_LOOKBACK_DAYS set "SYNC_LOOKBACK_DAYS=7"

rem --- venv interpreter (some venvs ship only python3.exe). Respect a pre-set PYEXE. ---
if not defined PYEXE set "PYEXE=%HRO_HOME%\hro-operations\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=%HRO_HOME%\hro-operations\.venv\Scripts\python3.exe"
echo [debug] HRO_HOME=[%HRO_HOME%]
echo [debug] PYEXE=[%PYEXE%]
if not exist "%PYEXE%" (
  echo [ERROR] venv python not found under %HRO_HOME%\hro-operations\.venv\Scripts\
  echo        HRO_HOME is [%HRO_HOME%]. If that path is wrong, set it once:
  echo          setx /M HRO_HOME "C:\Users\<you>\Documents\hro"
  echo        then open a NEW shell.
  echo Find the venv with: poetry env info --path
  pause
  exit /b 1
)

cd /d "%HRO_HOME%\hro-operations"
:loop
"%PYEXE%" -m hro_operations agent --server windows --interval 5
echo agent exited. restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop

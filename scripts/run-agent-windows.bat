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

cd /d %HRO_HOME%\hro-operations
:loop
poetry run hro-ops agent --server windows --interval 5
echo agent exited. restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop

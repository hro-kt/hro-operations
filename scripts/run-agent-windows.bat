@echo off
rem ==========================================================================
rem hro-ops Windows エージェント常駐用ラッパ。
rem タスクスケジューラの「ログオン時」トリガでこの .bat を起動する。
rem (JVLink はインタラクティブなデスクトップセッションが必要なため、
rem  「ユーザーがログオンしている時のみ実行」= session 0 のサービスにはしない)
rem 値は自分の環境に合わせて編集。パスワードを含むのでファイル権限に注意。
rem ==========================================================================

rem --- DB(VNet内マネージドPGへ到達できる値) ---
set POSTGRES_HOST=hro-db-prod1.postgres.database.azure.com
set POSTGRES_PORT=5432
set POSTGRES_DATABASE=hro
set POSTGRES_USER=hrouser
set POSTGRES_PASSWORD=＜PGパスワード＞
set POSTGRES_SSLMODE=require

rem --- 各リポジトリの基点 / JRDB 取得 ---
set HRO_HOME=C:\hro
set JRDB_USER=＜JRDB会員ID＞
set JRDB_PWD=＜JRDBパスワード＞

rem --- スマート差分 sync の遡り日数(任意) ---
set SYNC_LOOKBACK_DAYS=7

cd /d %HRO_HOME%\hro-operations
:loop
poetry run hro-ops agent --server windows --interval 5
echo agent が終了しました。10秒後に再起動します...
timeout /t 10 /nobreak >nul
goto loop

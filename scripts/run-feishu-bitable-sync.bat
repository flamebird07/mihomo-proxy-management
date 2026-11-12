@echo off
rem Daily Feishu sync (task MihomoFeishuBitableSync): full run, speed test on,
rem writes 表1/表2/表3. Do not add --no-speedtest here.
rem
rem Run from this script's parent (the repository root) instead of a hard-coded
rem path, so the same file works in the offline copy and in production.
cd /d "%~dp0.."
if errorlevel 1 exit /b 1

rem stdout and stderr go to a file of their own. cmd keeps the redirect handle
rem open for the whole run, and that handle blocks sync-feishu-bitable.py's
rem Logger from opening its own log file (PermissionError, errno 13), which
rem silenced every timestamped line from 2026-11-04 17:59. Never point this
rem redirect at the Logger's file (DEFAULT_LOG_FILE in scripts/sync-feishu-bitable.py).
if not defined BILL_LOG_DIR set "BILL_LOG_DIR=C:\mihomo\logs"

rem Save every stage's exit code. A failure in either stage must make the whole
rem run non-zero even when a later stage succeeds.
set "sync_exit=0"
set "switch_exit=0"

python scripts\sync-feishu-bitable.py >> "%BILL_LOG_DIR%\feishu-bitable-sync.stdout.log" 2>&1
set "sync_exit=%ERRORLEVEL%"

python scripts\proxy-auto-switch.py >> "%BILL_LOG_DIR%\proxy-auto-switch-stdout.log" 2>&1
set "switch_exit=%ERRORLEVEL%"

echo SYNC_RESULT exit=%sync_exit%
echo SWITCH_RESULT exit=%switch_exit%

if not "%sync_exit%"=="0" exit /b %sync_exit%
if not "%switch_exit%"=="0" exit /b %switch_exit%
exit /b 0

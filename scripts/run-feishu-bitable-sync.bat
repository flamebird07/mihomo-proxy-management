@echo off
rem Daily Feishu sync (task MihomoFeishuBitableSync): full run, speed test on,
rem writes 表1/表2/表3. Do not add --no-speedtest here.
cd /d C:\mihomo-proxy-management
rem stdout and stderr go to a file of their own. cmd keeps the redirect handle open
rem for the whole run, and that handle blocks sync-feishu-bitable.py's Logger from
rem opening its own log file (PermissionError, errno 13), which silenced every
rem timestamped line from 2026-11-04 17:59. Never point this redirect at the
rem Logger's file (DEFAULT_LOG_FILE in scripts/sync-feishu-bitable.py).
python scripts\sync-feishu-bitable.py >> C:\mihomo\logs\feishu-bitable-sync.stdout.log 2>&1
python scripts\proxy-auto-switch.py >> C:\mihomo\logs\proxy-auto-switch-stdout.log 2>&1

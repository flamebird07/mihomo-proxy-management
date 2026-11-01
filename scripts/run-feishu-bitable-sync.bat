@echo off
rem Daily Feishu sync (task MihomoFeishuBitableSync): full run, speed test on,
rem writes 表1/表2/表3. Do not add --no-speedtest here.
cd /d C:\mihomo-proxy-management
python scripts\sync-feishu-bitable.py >> C:\mihomo\logs\feishu-bitable-sync.log 2>&1
python scripts\proxy-auto-switch.py >> C:\mihomo\logs\proxy-failover.log 2>&1

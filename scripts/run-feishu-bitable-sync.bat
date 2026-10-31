@echo off
cd /d C:\mihomo-proxy-management
python scripts\sync-feishu-bitable.py >> C:\mihomo\logs\feishu-bitable-sync.log 2>&1
python scripts\proxy-auto-switch.py >> C:\mihomo\logs\proxy-failover.log 2>&1

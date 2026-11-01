@echo off
rem Optional manual entrypoint: speed test only, writes 表2/表3, never 表1.
rem The daily path is run-feishu-bitable-sync.bat, which already runs the speed
rem test and writes all three tables; use this only to refresh delays by hand.
rem proxy-auto-switch.py is deliberately NOT chained here - run-feishu-bitable-sync.bat
rem owns failover, so the two flows cannot fight over the same proxy selection.
cd /d C:\mihomo-proxy-management
python scripts\sync-feishu-bitable.py --speedtest-write >> C:\mihomo\logs\feishu-speedtest.log 2>&1

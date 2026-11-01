@echo off
rem Speedtest-only entrypoint: writes table2/table3, never table1 (sources).
rem proxy-auto-switch.py is deliberately NOT chained here - run-feishu-bitable-sync.bat
rem owns failover, so the two flows cannot fight over the same proxy selection.
cd /d C:\mihomo-proxy-management
python scripts\sync-feishu-bitable.py --speedtest-write >> C:\mihomo\logs\feishu-speedtest.log 2>&1

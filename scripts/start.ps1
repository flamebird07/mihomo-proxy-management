# =====================================================================
# start.ps1 - 启动 mihomo（通过计划任务）
# Start mihomo via scheduled task.
# =====================================================================

[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Ensure-Admin

if (Get-MihomoProcess) { Write-Warn 'mihomo 已在运行 / already running'; return }

$t = Get-MihomoTask
if (-not $t) { throw "计划任务不存在，请先运行 install.ps1 / scheduled task not found, run install.ps1 first" }

Write-Section 'Starting mihomo'
Start-ScheduledTask -TaskName $script:TaskName
if (Wait-MihomoReady -TimeoutSec 20) {
    Write-Ok "started, version=$(Get-MihomoVersion)"
} else {
    Write-Warn '启动超时 / startup timeout, check logs'
}
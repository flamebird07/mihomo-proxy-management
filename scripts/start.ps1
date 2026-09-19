# =====================================================================
# start.ps1 - 启动 mihomo（通过计划任务）
# Start mihomo via scheduled task.
# =====================================================================

[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Ensure-Admin

# 双进程自愈：发现多于 1 个 mihomo 进程时全部清理后由计划任务统一拉起，
# 而不是直接返回（旧逻辑会把重复进程留在原地）。
# Self-heal duplicates: kill all instances and let the scheduled task start a single one.
$running = @(Get-MihomoProcesses)
if ($running.Count -gt 1) {
    Write-Warn ("发现 {0} 个 mihomo 进程（应为 1 个），清理后重新启动 / duplicates found, cleaning up" -f $running.Count)
    Stop-Mihomo
} elseif ($running.Count -eq 1) {
    Write-Warn 'mihomo 已在运行 / already running'
    return
}

$t = Get-MihomoTask
if (-not $t) { throw "计划任务不存在，请先运行 install.ps1 / scheduled task not found, run install.ps1 first" }

Write-Section 'Starting mihomo'
Start-ScheduledTask -TaskName $script:TaskName
if (Wait-MihomoReady -TimeoutSec 20) {
    Write-Ok "started, version=$(Get-MihomoVersion)"
} else {
    Write-Warn '启动超时 / startup timeout, check logs'
}

# 启动后仍出现多个进程 = 还有别的自启动项在拉 mihomo（如启动文件夹里的 vbs）
$final = @(Get-MihomoProcesses)
if ($final.Count -gt 1) {
    Write-Err ("启动后仍有 {0} 个 mihomo 进程，请检查其他自启动项 / extra instances detected" -f $final.Count)
}
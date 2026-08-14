# =====================================================================
# uninstall.ps1 - 卸载 mihomo：停服、删计划任务、保留用户配置
# Uninstall: stop service, remove scheduled task, keep user configs.
# =====================================================================

[CmdletBinding()]
param(
    [switch]$Purge    # 顺手删除 <install_dir> 下的所有文件（不可恢复）
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Ensure-Admin

Write-Section 'Stopping mihomo'
Stop-Mihomo

Write-Section 'Removing scheduled task'
$t = Get-MihomoTask
if ($t) {
    Unregister-ScheduledTask -TaskName $script:TaskName -Confirm:$false
    Write-Ok 'task removed'
} else {
    Write-Warn 'task not found'
}

if ($Purge) {
    $dir = Get-InstallDir
    $ans = Read-Host "确认删除 $dir 下所有文件？[y/N]"
    if ($ans -match '(?i)^y') {
        Remove-Item $dir -Recurse -Force
        Write-Ok "$dir removed"
    } else {
        Write-Warn 'skipped purge'
    }
} else {
    Write-Host ''
    Write-Host "已卸载服务，目录保留：/ uninstalled service, directory kept:"
    Write-Host "  $(Get-InstallDir)"
    Write-Host "如需彻底删除，手动 Remove-Item -Recurse 即可。/ Remove manually if needed."
}
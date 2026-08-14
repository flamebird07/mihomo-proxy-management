# =====================================================================
# restart.ps1 - 重启 mihomo
# Restart mihomo.
# =====================================================================

[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Ensure-Admin
Stop-Mihomo
Start-Sleep -Seconds 2
& (Join-Path $PSScriptRoot 'start.ps1')
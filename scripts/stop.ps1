# =====================================================================
# stop.ps1 - 停止 mihomo
# Stop mihomo.
# =====================================================================

[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Stop-Mihomo
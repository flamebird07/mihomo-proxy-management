# scripts/sync-feishu-bitable.ps1
# Thin wrapper: run sync-feishu-bitable.py; register/remove Windows scheduled task.
# Usage:
#   .\sync-feishu-bitable.ps1
#   .\sync-feishu-bitable.ps1 -DryRun
#   .\sync-feishu-bitable.ps1 -EnsureTables
#   .\sync-feishu-bitable.ps1 -RegisterTask
#   .\sync-feishu-bitable.ps1 -RemoveTask

[CmdletBinding()]
param(
    [switch]$RegisterTask,
    [switch]$RemoveTask,
    [switch]$DryRun,
    [switch]$EnsureTables,
    [ValidateRange(5, 1440)]
    [int]$IntervalMinutes = 30
)

$ErrorActionPreference = 'Stop'
$taskName = 'MihomoFeishuBitableSync'
$repoRoot = Split-Path $PSScriptRoot -Parent
$scriptPy = Join-Path $PSScriptRoot 'sync-feishu-bitable.py'
$scriptBat = Join-Path $PSScriptRoot 'run-feishu-bitable-sync.bat'
$logDir   = 'C:\mihomo\logs'
$logFile  = Join-Path $logDir 'feishu-bitable-sync.log'

try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [Text.Encoding]::UTF8

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
if (-not (Test-Path $scriptPy)) { Write-Error "missing sync script: $scriptPy"; exit 2 }
try { $python = @(Get-Command python -CommandType Application -ErrorAction Stop)[0].Source } catch { Write-Error 'python not found in PATH'; exit 2 }

function Get-TaskState {
    $query = & schtasks.exe /Query /TN $taskName /FO LIST 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($query -join "`n")
}

function Register-SyncTask {
    $tr = '"{0}"' -f $scriptBat
    $ErrorActionPreference = 'Continue'; & schtasks.exe /Delete /TN $taskName /F 2>$null | Out-Null; $ErrorActionPreference = 'Stop'
    & schtasks.exe /Create /TN $taskName /TR $tr /SC MINUTE /MO $IntervalMinutes /RL LIMITED /F
    if ($LASTEXITCODE -ne 0) {
        # fallback ScheduledTasks module
        $action = New-ScheduledTaskAction -Execute $scriptBat -WorkingDirectory $repoRoot
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration ([TimeSpan]::MaxValue)
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Force | Out-Null
    }
    if (Get-TaskState) { Write-Host ("task {0} registered every {1} min" -f $taskName, $IntervalMinutes); return 0 }
    Write-Host ("task {0} register FAILED" -f $taskName); return 1
}

function Remove-SyncTask {
    $ErrorActionPreference = 'Continue'; & schtasks.exe /Delete /TN $taskName /F 2>$null | Out-Null; $ErrorActionPreference = 'Stop'
    if (-not (Get-TaskState)) { Write-Host ("task {0} removed" -f $taskName); return 0 }
    Write-Host ("task {0} remove FAILED" -f $taskName); return 1
}

if ($RegisterTask) { exit (Register-SyncTask) }
if ($RemoveTask) { exit (Remove-SyncTask) }

$argsPy = @($scriptPy)
if ($DryRun) { $argsPy += '--dry-run' }
if ($EnsureTables) { $argsPy += '--ensure-tables' }

Push-Location $repoRoot
try {
    & $python @argsPy
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

$state = Get-TaskState
if ($state) { Write-Host ("task {0} present" -f $taskName) } else { Write-Host ("task {0} not present (use -RegisterTask)" -f $taskName) }
exit $code

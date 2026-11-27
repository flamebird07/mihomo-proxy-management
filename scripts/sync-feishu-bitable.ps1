# scripts/sync-feishu-bitable.ps1
# Thin wrapper: run sync-feishu-bitable.py; register/remove Windows scheduled task.
# Default (no switches) is the daily path: speed test on, writes 表1/表2/表3.
# Usage:
#   .\sync-feishu-bitable.ps1
#   .\sync-feishu-bitable.ps1 -DryRun
#   .\sync-feishu-bitable.ps1 -EnsureTables
#   .\sync-feishu-bitable.ps1 -NoSpeedtest      # manual only: 表1 only, no speed test
#   .\sync-feishu-bitable.ps1 -SpeedtestWrite   # manual only: 表2/表3 only, no 表1
#   .\sync-feishu-bitable.ps1 -RegisterTask
#   .\sync-feishu-bitable.ps1 -RemoveTask
#
# The scheduled task (bootstrap) invokes:
#   powershell.exe ... -File <this script> -Run -InstallDir <abs> -Interpreter <abs python.exe>
# -Run sets MIHOMO_DIR for the child processes, runs sync then switch in that
# order with the fixed interpreter, and returns a non-zero exit if either fails.

[CmdletBinding()]
param(
    [switch]$RegisterTask,
    [switch]$RemoveTask,
    [switch]$DryRun,
    [switch]$EnsureTables,
    [switch]$NoSpeedtest,
    [switch]$SpeedtestWrite,
    [ValidateRange(5, 1440)]
    [int]$IntervalMinutes = 30,
    # Bootstrap-only registration: pin the interpreter + install dir and
    # register the task for the deploying user at Interactive/Limited.  The
    # default -RegisterTask path (schtasks /RL LIMITED, PATH python) is
    # unchanged for manual/legacy use.
    [switch]$Bootstrap,
    [string]$InstallDir = '',
    [string]$Interpreter = '',
    # Scheduled-task run mode (never used from an interactive shell).
    [switch]$Run
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$taskName = 'MihomoFeishuBitableSync'
$repoRoot = Split-Path $PSScriptRoot -Parent
$scriptPy = Join-Path $PSScriptRoot 'sync-feishu-bitable.py'
$scriptBat = Join-Path $PSScriptRoot 'run-feishu-bitable-sync.bat'

try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [Text.Encoding]::UTF8

if (-not (Test-Path -LiteralPath $scriptPy)) { Write-Error "missing sync script: $scriptPy"; exit 2 }

# Resolve a PATH python.  A bootstrap/run invocation passes -Interpreter, so a
# missing PATH python is only fatal for the manual paths.
$python = $null
try { $python = @(Get-Command python -CommandType Application -ErrorAction Stop)[0].Source } catch {}

function Get-TaskState {
    $query = & schtasks.exe /Query /TN $taskName /FO LIST 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($query -join "`n")
}

function Register-SyncTask {
    # Legacy/manual path: unchanged semantics (schtasks, /RL LIMITED, PATH python).
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

function Invoke-RunMode {
    # Scheduled-task entry: fixed interpreter + install dir, sync then switch.
    # A failure in either stage makes the task exit non-zero even if a later
    # stage succeeds (same precedence as the legacy batch file).
    $py = $Interpreter
    if (-not $py -and $env:MPM_PYTHON) { $py = $env:MPM_PYTHON }
    if (-not $py -or -not (Test-Path -LiteralPath $py -PathType Leaf)) {
        Write-Host "run mode: python not found: '$py'"; return 2
    }

    $dir = $InstallDir
    if (-not $dir) { $dir = if ($env:MIHOMO_DIR) { $env:MIHOMO_DIR } else { 'C:\mihomo' } }
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $dir = (Resolve-Path -LiteralPath $dir).Path
    $logs = Join-Path $dir 'logs'
    if (-not (Test-Path -LiteralPath $logs)) { New-Item -ItemType Directory -Path $logs -Force | Out-Null }

    $env:MIHOMO_DIR = $dir
    $env:MPM_PYTHON = $py
    $env:BILL_LOG_DIR = $logs

    $syncPy = Join-Path $PSScriptRoot 'sync-feishu-bitable.py'
    $switchPy = Join-Path $PSScriptRoot 'proxy-auto-switch.py'
    $syncLog = Join-Path $logs 'feishu-bitable-sync.stdout.log'
    $switchLog = Join-Path $logs 'proxy-auto-switch-stdout.log'

    $syncExit = 0
    $switchExit = 0
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location $repoRoot
    try {
        & $py $syncPy 2>&1 | Out-File -LiteralPath $syncLog -Append -Encoding utf8 -Width 4096
        $syncExit = $LASTEXITCODE
        & $py $switchPy 2>&1 | Out-File -LiteralPath $switchLog -Append -Encoding utf8 -Width 4096
        $switchExit = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $prevEap
    }
    if ($null -eq $syncExit) { $syncExit = 0 }
    if ($null -eq $switchExit) { $switchExit = 0 }

    Write-Host ("SYNC_RESULT exit={0}" -f $syncExit)
    Write-Host ("SWITCH_RESULT exit={0}" -f $switchExit)
    if ($syncExit -ne 0) { return [int]$syncExit }
    if ($switchExit -ne 0) { return [int]$switchExit }
    return 0
}

function Register-BootstrapSyncTask {
    # Register for the deploying user (Interactive/Limited), after granting that
    # user read on subscription.env and Modify on logs/providers.  The task runs
    # an absolute Windows PowerShell with this script's -Run mode; no batch
    # launcher is generated and never SYSTEM/Highest.
    $dir = $InstallDir
    if (-not $dir) { $dir = if ($env:MIHOMO_DIR) { $env:MIHOMO_DIR } else { 'C:\mihomo' } }
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $dir = (Resolve-Path -LiteralPath $dir).Path

    $py = $Interpreter
    if (-not $py -and $env:MPM_PYTHON) { $py = $env:MPM_PYTHON }
    if (-not $py) { $py = $python }
    if (-not $py -or -not (Test-Path -LiteralPath $py -PathType Leaf)) {
        Write-Host "bootstrap python not found: '$py'"; return 1
    }

    $logs = Join-Path $dir 'logs'
    $providers = Join-Path $dir 'providers'
    foreach ($d in @($logs, $providers)) {
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
    $envPath = Join-Path $dir 'subscription.env'

    $sid = Get-CurrentUserSid
    $acl = Grant-BootstrapTaskAcls -SubscriptionEnvPath $envPath -LogsDir $logs -ProvidersDir $providers -Sid $sid
    if (-not $acl.Ok) {
        Write-Host ("ACL grant FAILED for '{0}' (reason={1}, exit={2}); refusing to register {3}" -f $acl.FailedPath, $acl.Reason, $acl.ExitCode, $taskName)
        return 1
    }

    $spec = New-BootstrapTaskPrincipalSpec
    try {
        Assert-BootstrapTaskPrincipal -UserId $spec.UserId -LogonType $spec.LogonType -RunLevel $spec.RunLevel
    } catch {
        Write-Host ("principal check FAILED: {0}" -f $_.Exception.Message); return 1
    }

    $actionSpec = New-PowerShellFileActionSpec -ScriptPath $PSCommandPath -ScriptArguments @('-Run', '-InstallDir', $dir, '-Interpreter', $py)
    $action = New-ScheduledTaskAction -Execute $actionSpec.Execute -Argument $actionSpec.Argument -WorkingDirectory $repoRoot
    $trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration ([TimeSpan]::MaxValue)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $spec.UserId -LogonType $spec.LogonType -RunLevel $spec.RunLevel

    try {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Host ("task {0} register FAILED: {1}" -f $taskName, $_.Exception.Message); return 1
    }
    if (Get-TaskState) {
        Write-Host ("task {0} registered (bootstrap, {1}/{2}) every {3} min" -f $taskName, $spec.LogonType, $spec.RunLevel, $IntervalMinutes)
        return 0
    }
    Write-Host ("task {0} register FAILED" -f $taskName); return 1
}

if ($Run) { exit (Invoke-RunMode) }

if ($RegisterTask) {
    if ($Bootstrap) { exit (Register-BootstrapSyncTask) }
    exit (Register-SyncTask)
}
if ($RemoveTask) { exit (Remove-SyncTask) }

# ---- manual paths (unchanged): PATH python, no scheduled-task launcher ----
$logDir = 'C:\mihomo\logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
if (-not $python) { Write-Error 'python not found in PATH'; exit 2 }

if ($NoSpeedtest -and $SpeedtestWrite) { Write-Error '-NoSpeedtest and -SpeedtestWrite are mutually exclusive'; exit 2 }

$argsPy = @($scriptPy)
if ($DryRun) { $argsPy += '--dry-run' }
if ($EnsureTables) { $argsPy += '--ensure-tables' }
if ($NoSpeedtest) { $argsPy += '--no-speedtest' }
if ($SpeedtestWrite) { $argsPy += '--speedtest-write' }

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

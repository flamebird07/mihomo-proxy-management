# =====================================================================
# install-node-pipeline-task.ps1
# Register the hourly node pipeline task (MihomoNodePipeline).
#
# The task runs scripts\run-node-pipeline.py through an absolute Windows
# PowerShell -File entry that pins the interpreter and the install dir, so it
# never inherits path settings from the shell that registered it.  It inherits
# the principal of MihomoFeishuBitableSync (the deploying user at
# Interactive/Limited); an elevated/SYSTEM sync task is refused.  Before
# registering, the deploying user is granted read on subscription.env and
# Modify on logs/providers.
#
# Usage:
#   .\install-node-pipeline-task.ps1 [-InstallDir C:\mihomo] [-Interpreter <python.exe>] [-Force]
#   .\install-node-pipeline-task.ps1 -Run -InstallDir <abs> -Interpreter <abs python.exe>
# Exit 0 = registered/ran OK; non-zero = failure (Run returns the pipeline's).
# =====================================================================

[CmdletBinding()]
param(
    [string]$InstallDir = '',
    [string]$Interpreter = '',
    [switch]$Force,
    # Scheduled-task run mode: fixed interpreter + install dir.
    [switch]$Run
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$taskName = 'MihomoNodePipeline'
$repoRoot = Split-Path $PSScriptRoot -Parent
$scriptPath = Join-Path $PSScriptRoot 'run-node-pipeline.py'

function Invoke-RunMode {
    # Scheduled-task entry: set MIHOMO_DIR for the child, call the fixed Python
    # with & and return its real exit code.
    $py = $Interpreter
    if (-not $py -and $env:MPM_PYTHON) { $py = $env:MPM_PYTHON }
    if (-not $py -or -not (Test-Path -LiteralPath $py -PathType Leaf)) {
        Write-Host "run mode: python not found: '$py'"; return 2
    }
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        Write-Host "missing pipeline script: $scriptPath"; return 2
    }

    $dir = $InstallDir
    if (-not $dir) { $dir = if ($env:MIHOMO_DIR) { $env:MIHOMO_DIR } else { 'C:\mihomo' } }
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $dir = (Resolve-Path -LiteralPath $dir).Path
    $logs = Join-Path $dir 'logs'
    if (-not (Test-Path -LiteralPath $logs)) { New-Item -ItemType Directory -Path $logs -Force | Out-Null }

    $env:MIHOMO_DIR = $dir
    $env:MPM_PYTHON = $py
    $log = Join-Path $logs 'node-pipeline.stdout.log'

    $code = 0
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location $repoRoot
    try {
        & $py $scriptPath 2>&1 | Out-File -LiteralPath $log -Append -Encoding utf8 -Width 4096
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $prevEap
    }
    if ($null -eq $code) { $code = 0 }
    return [int]$code
}

if ($Run) { exit (Invoke-RunMode) }

# The pipeline inherits the sync task identity.  Refuse anything elevated.
try {
    $existing = Get-ScheduledTask -TaskName 'MihomoFeishuBitableSync' -ErrorAction Stop
} catch {
    Write-Error "Cannot read the main sync task principal: $($_.Exception.Message)"
    exit 1
}
if ($null -eq $existing -or $null -eq $existing.Principal) {
    Write-Error 'The main sync task has no readable principal.'
    exit 1
}
if (Test-ElevatedTaskIdentity -UserId $existing.Principal.UserId -RunLevel $existing.Principal.RunLevel) {
    $msg = "Refusing to inherit an elevated task principal (user='{0}', runLevel='{1}'). Re-register MihomoFeishuBitableSync as the current user at Limited." -f $existing.Principal.UserId, $existing.Principal.RunLevel
    Write-Error $msg
    exit 1
}

# ---- fixed install dir (absolute; never inherited from a temp shell) ----
if (-not $InstallDir) {
    $InstallDir = if ($env:MIHOMO_DIR) { $env:MIHOMO_DIR } else { 'C:\mihomo' }
}
if (-not (Test-Path -LiteralPath $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
$InstallDir = (Resolve-Path -LiteralPath $InstallDir).Path
$logDir = Join-Path $InstallDir 'logs'
$providersDir = Join-Path $InstallDir 'providers'
foreach ($d in @($logDir, $providersDir)) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

# ---- fixed interpreter: explicit > bootstrap env > PATH ----
$python = $Interpreter
if (-not $python -and $env:MPM_PYTHON) { $python = $env:MPM_PYTHON }
if (-not $python) {
    try { $python = (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source }
    catch { Write-Error 'python.exe not found; pass -Interpreter'; exit 1 }
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Error "python.exe not found: $python"; exit 1
}
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    Write-Error "missing pipeline script: $scriptPath"; exit 1
}

# ---- ACL grants: the inherited user must be able to read the credentials and
#      write logs/providers before the task is registered ----
$sid = Get-CurrentUserSid
$envPath = Join-Path $InstallDir 'subscription.env'
$acl = Grant-BootstrapTaskAcls -SubscriptionEnvPath $envPath -LogsDir $logDir -ProvidersDir $providersDir -Sid $sid
if (-not $acl.Ok) {
    $msg = "ACL grant FAILED for '{0}' (reason={1}, exit={2}); refusing to register {3}" -f $acl.FailedPath, $acl.Reason, $acl.ExitCode, $taskName
    Write-Error $msg
    exit 1
}

# ---- absolute Windows PowerShell -File action with pinned interpreter/dir ----
$actionSpec = New-PowerShellFileActionSpec -ScriptPath $PSCommandPath -ScriptArguments @('-Run', '-InstallDir', $InstallDir, '-Interpreter', $python)
$action = New-ScheduledTaskAction -Execute $actionSpec.Execute -Argument $actionSpec.Argument -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(5)) -RepetitionInterval (New-TimeSpan -Hours 1)

# Inherit the main task identity exactly (UserId + LogonType + RunLevel).
$principal = New-ScheduledTaskPrincipal -UserId $existing.Principal.UserId -LogonType $existing.Principal.LogonType -RunLevel $existing.Principal.RunLevel

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
} catch {
    Write-Error "Failed to register $taskName (permission or scheduler error): $($_.Exception.Message)"
    exit 1
}
exit 0

$ErrorActionPreference = 'Stop'
$taskName = 'MihomoNinjaGrokFeishuSync'

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

$python = (Get-Command python.exe -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    Write-Error 'pythonw.exe was not found next to python.exe.'
    exit 1
}

$scriptPath = Join-Path $PSScriptRoot 'sync-feishu-bitable.py'
# Same-table writes must keep using the shared writer mutex. Do not pass a
# private --lock-file here: that parameter only moved the metadata file and
# never gave Ninja an independent lock, so it was misleading.
$arguments = '"{0}" --ninja-only --log-file "C:\mihomo\logs\feishu-ninja-grok.log"' -f $scriptPath
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(10)) -RepetitionInterval (New-TimeSpan -Minutes 10)

# Inherit the main task identity exactly (UserId + LogonType + RunLevel).
# Forcing Interactive/Limited here broke runs under the main task's account.
$principal = New-ScheduledTaskPrincipal -UserId $existing.Principal.UserId -LogonType $existing.Principal.LogonType -RunLevel $existing.Principal.RunLevel

# MultipleInstances IgnoreNew is the whole-round anti-duplicate guard; it is
# independent of the shared writer mutex used for same-table writes.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 8)

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -ErrorAction Stop | Out-Null
} catch {
    Write-Error "Failed to register $taskName (permission or scheduler error): $($_.Exception.Message)"
    exit 1
}
exit 0

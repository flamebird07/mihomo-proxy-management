[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$SourceProvider,
    [string]$TaskName = 'MihomoXiguaIpPool',
    [string]$RuntimeRoot = "$PSScriptRoot\..\runtime\xigua-ippool",
    [ValidateSet('dry-run', 'production')][string]$Mode = 'dry-run',
    [string]$FinalProvider = '',
    [string]$Core = '',
    [string[]]$Target = @(),
    [switch]$RunE2E,
    [int]$MixedPort = 17890,
    [int]$ControllerPort = 19090,
    [int]$TimeoutMs = 10000,
    [int]$Workers = 4,
    [string]$RunAsUser = '',
    [ValidateSet('LIMITED', 'HIGHEST')][string]$RunLevel = 'LIMITED'
)

if ($Mode -eq 'production' -and [string]::IsNullOrWhiteSpace($FinalProvider)) {
    throw 'Production task requires an explicit -FinalProvider.'
}
if (($Mode -eq 'production' -or $RunE2E.IsPresent) -and
    ([string]::IsNullOrWhiteSpace($Core) -or $Target.Count -lt 2)) {
    throw 'E2E task requires -Core and at least two explicit -Target values.'
}

# Explicit account: default to the current user, but always pass /RU so the
# registered task never silently inherits an ambiguous identity.
if ([string]::IsNullOrWhiteSpace($RunAsUser)) {
    $RunAsUser = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
}
if ([string]::IsNullOrWhiteSpace($RunAsUser)) {
    throw 'An explicit -RunAsUser is required.'
}

function ConvertTo-PSLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

$runner = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'run-xigua-ippool.ps1'))
$pairs = @(
    "SourceProvider=$(ConvertTo-PSLiteral ([IO.Path]::GetFullPath($SourceProvider)))",
    "RuntimeRoot=$(ConvertTo-PSLiteral ([IO.Path]::GetFullPath($RuntimeRoot)))",
    "Mode=$(ConvertTo-PSLiteral $Mode)",
    "MixedPort=$MixedPort", "ControllerPort=$ControllerPort", "TimeoutMs=$TimeoutMs", "Workers=$Workers"
)
if ($FinalProvider) { $pairs += "FinalProvider=$(ConvertTo-PSLiteral ([IO.Path]::GetFullPath($FinalProvider)))" }
if ($Core) { $pairs += "Core=$(ConvertTo-PSLiteral ([IO.Path]::GetFullPath($Core)))" }
if ($Target.Count) {
    $targetLiterals = ($Target | ForEach-Object { ConvertTo-PSLiteral $_ }) -join ','
    $pairs += "Target=@($targetLiterals)"
}
if ($RunE2E) { $pairs += 'RunE2E=$true' }
if ($PSCmdlet.ShouldProcess($TaskName, 'Register 5-minute scheduled task')) {
    $runtimeFull = [IO.Path]::GetFullPath($RuntimeRoot)
    $launcher = Join-Path $runtimeFull 'scheduled-run.ps1'
    $action = 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $launcher + '"'
    if ($action.Length -gt 261) {
        throw "Scheduled task action is $($action.Length) characters; schtasks /TR limit is 261. Use a shorter RuntimeRoot."
    }
    New-Item -ItemType Directory -Force -Path $runtimeFull | Out-Null
    $payload = @(
        '$ErrorActionPreference = ''Stop''',
        ('$p=@{' + ($pairs -join ';') + '}'),
        ('& ' + (ConvertTo-PSLiteral $runner) + ' @p'),
        'exit $LASTEXITCODE'
    ) -join "`r`n"
    $temporary = Join-Path $runtimeFull ('.scheduled-run.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backup = Join-Path $runtimeFull ('.scheduled-run.backup.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $payload + "`r`n", [Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $launcher) {
            [IO.File]::Replace($temporary, $launcher, $backup)
            Remove-Item -LiteralPath $backup -Force
        } else {
            [IO.File]::Move($temporary, $launcher)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
        if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Force }
    }
    $schtasksArgs = @(
        '/Create', '/TN', $TaskName, '/SC', 'MINUTE', '/MO', '5',
        '/TR', $action, '/RU', $RunAsUser, '/RL', $RunLevel, '/F'
    )
    & schtasks.exe @schtasksArgs
    if ($LASTEXITCODE -ne 0) { throw "schtasks failed with exit code $LASTEXITCODE" }
}

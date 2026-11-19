[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceProvider,
    [string]$RuntimeRoot = "$PSScriptRoot\..\runtime\xigua-ippool",
    [ValidateSet('dry-run', 'production')][string]$Mode = 'dry-run',
    [string]$FinalProvider = '',
    [string]$Core = '',
    [string[]]$Target = @(),
    [switch]$RunE2E,
    [ValidateRange(1, 65535)][int]$MixedPort = 17890,
    [ValidateRange(1, 65535)][int]$ControllerPort = 19090,
    [ValidateRange(100, 120000)][int]$TimeoutMs = 10000,
    [ValidateRange(1, 64)][int]$Workers = 4,
    [string]$LogFile = ''
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($RuntimeRoot)
New-Item -ItemType Directory -Force -Path $root | Out-Null
if ([string]::IsNullOrWhiteSpace($LogFile)) {
    $LogFile = Join-Path $root 'xigua-ippool.log'
} else {
    $LogFile = [IO.Path]::GetFullPath($LogFile)
}

function Protect-LogText {
    param([string]$Text)
    $Text = [regex]::Replace($Text, '(?i)\b(password|passwd|pwd|token|secret|app_secret|sni|servername)\b\s*[:=]\s*\S+', '$1=<redacted>')
    $Text = [regex]::Replace($Text, '(?i)\b(?:https?|socks5?)://\S+', '<redacted-url>')
    return $Text
}

function Invoke-LogRotation {
    param([string]$Path, [long]$MaxBytes = 1048576, [int]$Keep = 3)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ((Get-Item -LiteralPath $Path).Length -lt $MaxBytes) { return }
    for ($i = $Keep - 1; $i -ge 1; $i--) {
        $older = $Path + '.' + $i
        if (Test-Path -LiteralPath $older) {
            Move-Item -LiteralPath $older -Destination ($Path + '.' + ($i + 1)) -Force
        }
    }
    Move-Item -LiteralPath $Path -Destination ($Path + '.1') -Force
}

$script:logFailed = $false
function Write-ChainLog {
    param([string]$Message)
    $line = (Get-Date).ToString('o') + ' ' + (Protect-LogText $Message)
    try {
        $dir = Split-Path -Parent $LogFile
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
        Invoke-LogRotation -Path $LogFile
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 -ErrorAction Stop
    } catch {
        $script:logFailed = $true
        [Console]::Error.WriteLine('XIGUA_LOG_FAILED ' + $line)
    }
}

# XGFIX1: capture python stderr in memory only (nothing unsanitized is
# written to disk).  PS 5.1 trap: under $ErrorActionPreference='Stop' a
# redirected native stderr line raises a terminating NativeCommandError,
# so EAP is lowered only around the native call and restored in finally.
$script:lastPythonStderr = @()
function Invoke-PythonWithStderrCapture {
    param([string[]]$PythonArgs)
    $script:lastPythonStderr = @()
    $captured = @()
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & python @PythonArgs 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $captured += $_.ToString()
            } else {
                [Console]::Out.WriteLine([string]$_)
            }
        }
    } finally {
        $ErrorActionPreference = $prevEap
    }
    $script:lastPythonStderr = $captured
}

# XGFIX1: extends Protect-LogText (reused unchanged) with the three stderr
# tail shapes: credentials, ip:port, domains.  Bare IPv4 without a port and
# hyphen-split GUIDs stay visible on purpose; timestamps cannot match the
# ip:port rule because it requires a dotted quad before the colon.
function Protect-StderrTailText {
    param([string]$Text)
    $Text = Protect-LogText $Text
    $Text = [regex]::Replace($Text, '(?i)\bbearer\s+\S+', 'Bearer <redacted>')
    $Text = [regex]::Replace($Text, '(?i)\b(api[_-]?key|key|access[_-]?token)\b\s*[:=]\s*\S+', '$1=<redacted>')
    $Text = [regex]::Replace($Text, '(?i)(?<![0-9a-f])[0-9a-f]{32,}(?![0-9a-f])', '<redacted-hex>')
    $Text = [regex]::Replace($Text, '(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])', '<redacted-b64>')
    $Text = [regex]::Replace($Text, '\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d):[1-9]\d{0,4}\b', '<ip:port>')
    $Text = [regex]::Replace($Text, '(?i)(?<![\\/.\w-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}(?![a-z0-9-])', '<domain>')
    return $Text
}

# XGFIX1: log at most the last 20 stderr lines of one failed python stage.
function Write-StderrTail {
    param([string]$Stage, [string[]]$Lines)
    if ($null -eq $Lines -or $Lines.Count -eq 0) { return }
    $first = [Math]::Max(0, $Lines.Count - 20)
    for ($i = $first; $i -lt $Lines.Count; $i++) {
        Write-ChainLog ('stderr tail stage=' + $Stage + ' line ' + ($i + 1) + '/' + $Lines.Count + ' ' + (Protect-StderrTailText $Lines[$i]))
    }
}

$stage1 = Join-Path $root 'xigua-ippool.stage1.yaml'

if ($Mode -eq 'production' -and [string]::IsNullOrWhiteSpace($FinalProvider)) {
    throw 'Production mode requires an explicit -FinalProvider; no default is permitted.'
}
$needsE2E = $Mode -eq 'production' -or $RunE2E.IsPresent
if ($needsE2E) {
    if ([string]::IsNullOrWhiteSpace($Core)) { throw 'E2E filtering requires an explicit -Core.' }
    if ($Target.Count -lt 2) { throw 'E2E filtering requires at least two explicit -Target values.' }
}

Write-ChainLog "round start mode=$Mode"

# Stage 1 never publishes the production provider.
$collectorArgs = @(
    "$PSScriptRoot\ippool-collector.py",
    '--source-provider', [IO.Path]::GetFullPath($SourceProvider),
    '--state', (Join-Path $root 'ippool.json'),
    '--output-provider', $stage1,
    '--lock', (Join-Path $root 'collector.lock'),
    '--mode', 'dry-run'
)
Invoke-PythonWithStderrCapture $collectorArgs
$collectorExit = $LASTEXITCODE
$poolExit = $collectorExit
Write-ChainLog "stage collector exit=$collectorExit"
if ($collectorExit -ne 0) { Write-StderrTail -Stage 'collector' -Lines $script:lastPythonStderr }

$healthExit = $null
$stage2Output = if ($Mode -eq 'production') {
    [IO.Path]::GetFullPath($FinalProvider)
} else {
    Join-Path $root 'xigua-ippool.e2e.staging.yaml'
}
if ($collectorExit -eq 0 -and $needsE2E) {
    $healthArgs = @(
        "$PSScriptRoot\health-check-provider.py",
        '--candidate', $stage1,
        '--output', $stage2Output,
        '--core', [IO.Path]::GetFullPath($Core),
        '--required-name-regex', '西瓜',
        '--minimum-matching', '1',
        '--mixed-port', [string]$MixedPort,
        '--controller-port', [string]$ControllerPort,
        '--timeout-ms', [string]$TimeoutMs,
        '--workers', [string]$Workers
    )
    foreach ($url in $Target) { $healthArgs += @('--target', $url) }
    Invoke-PythonWithStderrCapture $healthArgs
    $healthExit = $LASTEXITCODE
    $poolExit = $healthExit
    Write-ChainLog "stage health exit=$healthExit"
    if ($healthExit -ne 0) { Write-StderrTail -Stage 'health' -Lines $script:lastPythonStderr }
} else {
    Write-ChainLog 'stage health skipped'
}

# Feishu is an independent result.  It runs after stage 2 in production so
# 最终可用 reflects this round's final provider.  A Feishu failure must still
# make the whole round non-zero.
$feishuArgs = @(
    "$PSScriptRoot\sync-xigua-ippool-feishu.py",
    '--ippool-state', (Join-Path $root 'ippool.json'),
    '--cursor', (Join-Path $root 'feishu-sync-state.json'),
    '--lock', (Join-Path $root 'feishu-sync.lock')
)
if ($Mode -eq 'production') { $feishuArgs += @('--final-provider', $stage2Output) }
Invoke-PythonWithStderrCapture $feishuArgs
$feishuExit = $LASTEXITCODE
Write-ChainLog "stage feishu exit=$feishuExit"
if ($feishuExit -ne 0) { Write-StderrTail -Stage 'feishu' -Lines $script:lastPythonStderr }

Write-Output "POOL_RESULT collector=$collectorExit final=$poolExit"
Write-Output "FEISHU_RESULT exit=$feishuExit"

# Pool failure wins; otherwise a Feishu failure still makes the round non-zero.
$finalExit = $poolExit
if ($finalExit -eq 0 -and $feishuExit -ne 0) { $finalExit = $feishuExit }
Write-ChainLog "round end pool=$poolExit feishu=$feishuExit exit=$finalExit"
if ($finalExit -eq 0 -and $script:logFailed) { $finalExit = 8 }
Write-Output "ROUND_RESULT exit=$finalExit"
exit $finalExit

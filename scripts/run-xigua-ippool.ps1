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
& python @collectorArgs
$collectorExit = $LASTEXITCODE
$poolExit = $collectorExit
Write-ChainLog "stage collector exit=$collectorExit"

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
    & python @healthArgs
    $healthExit = $LASTEXITCODE
    $poolExit = $healthExit
    Write-ChainLog "stage health exit=$healthExit"
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
& python @feishuArgs
$feishuExit = $LASTEXITCODE
Write-ChainLog "stage feishu exit=$feishuExit"

Write-Output "POOL_RESULT collector=$collectorExit final=$poolExit"
Write-Output "FEISHU_RESULT exit=$feishuExit"

# Pool failure wins; otherwise a Feishu failure still makes the round non-zero.
$finalExit = $poolExit
if ($finalExit -eq 0 -and $feishuExit -ne 0) { $finalExit = $feishuExit }
Write-ChainLog "round end pool=$poolExit feishu=$feishuExit exit=$finalExit"
if ($finalExit -eq 0 -and $script:logFailed) { $finalExit = 8 }
Write-Output "ROUND_RESULT exit=$finalExit"
exit $finalExit

[CmdletBinding()]
param(
    [string]$EnvFile = (Join-Path (Split-Path $PSScriptRoot -Parent) 'config\subscription.env'),
    [string]$Candidate = 'C:\mihomo\providers\three-sources-candidate.yaml',
    [string]$Healthy = 'C:\mihomo\providers\three-sources-all-targets.yaml',
    [ValidateRange(1, 2147483647)]
    [int]$MinNodes = 20
)
$ErrorActionPreference = 'Stop'

$root = Split-Path $PSScriptRoot -Parent
$python = @(Get-Command python -CommandType Application -ErrorAction Stop)[0].Source
$lock = 'C:\mihomo\subscription-refresh.lock'
$next = "$Healthy.next"
$previous = "$Healthy.previous"
$controller = 'http://127.0.0.1:9090'

function Get-Selections {
    $result = @{}
    $api = Invoke-RestMethod -Uri "$controller/proxies" -TimeoutSec 5
    $api.proxies.PSObject.Properties | ForEach-Object {
        if ($_.Value.type -eq 'Selector') { $result[$_.Name] = $_.Value.now }
    }
    return $result
}

function Reload-And-Restore([hashtable]$Selections) {
    Invoke-RestMethod -Method Put -Uri "$controller/configs?force=true" `
        -ContentType 'application/json' -Body '{"path":"C:\\mihomo\\config.yaml"}' -TimeoutSec 20 | Out-Null
    Start-Sleep -Seconds 3
    $api = Invoke-RestMethod -Uri "$controller/proxies" -TimeoutSec 5
    foreach ($entry in $Selections.GetEnumerator()) {
        $group = $api.proxies.PSObject.Properties[$entry.Key].Value
        if ($null -ne $group -and @($group.all) -contains $entry.Value) {
            $encoded = [uri]::EscapeDataString($entry.Key)
            $body = @{ name = $entry.Value } | ConvertTo-Json -Compress
            Invoke-RestMethod -Method Put -Uri "$controller/proxies/$encoded" `
                -ContentType 'application/json' -Body $body -TimeoutSec 5 | Out-Null
        }
    }
}

function Get-HttpStatus([string]$Url, [switch]$Follow) {
    $arguments = @('-x', 'http://127.0.0.1:7890', '-o', 'NUL', '-sS', '--connect-timeout', '10',
        '--max-time', '30', '-w', '%{http_code}', $Url)
    if ($Follow) { $arguments = @('-L') + $arguments }
    $status = & curl.exe @arguments
    if ($LASTEXITCODE -ne 0) { return 0 }
    return [int]$status
}

try {
    New-Item -ItemType File -Path $lock -ErrorAction Stop | Out-Null
    & (Join-Path $PSScriptRoot 'update-subscription.ps1') -EnvFile $EnvFile -Output $Candidate -MinNodes $MinNodes
    if ($LASTEXITCODE -notin 0, 5) { throw 'candidate refresh failed' }

    & $python (Join-Path $PSScriptRoot 'health-check-provider.py') `
        --candidate $Candidate --output $next `
        --target 'https://www.gstatic.com/generate_204' `
        --target 'https://chatgpt.com/cdn-cgi/trace' `
        --target 'https://muse.ai/' --minimum-matching 1
    if ($LASTEXITCODE -ne 0) { throw 'health publication gate failed' }

    $selections = Get-Selections
    if (Test-Path -LiteralPath $Healthy) { Copy-Item -LiteralPath $Healthy -Destination $previous -Force }
    Copy-Item -LiteralPath $next -Destination $Healthy -Force
    try {
        Reload-And-Restore $selections
        $group = Invoke-RestMethod -Uri "$controller/proxies/US-FAST" -TimeoutSec 5
        if (@($group.all).Count -lt 1) { throw 'US-FAST is empty after reload' }
        $chatgpt = Get-HttpStatus 'https://chatgpt.com/cdn-cgi/trace' -Follow
        $muse = Get-HttpStatus 'https://muse.ai/'
        if ($chatgpt -ne 200 -or $muse -lt 200 -or $muse -ge 400) {
            throw "application check failed (chatgpt=$chatgpt muse=$muse)"
        }
    } catch {
        if (Test-Path -LiteralPath $previous) {
            Copy-Item -LiteralPath $previous -Destination $Healthy -Force
            Reload-And-Restore $selections
        }
        throw
    }
    Write-Host "Production refresh completed; US pool count=$(@($group.all).Count)."
} finally {
    Remove-Item -LiteralPath $next -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
}

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EnvFile,
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [ValidateRange(1, 2147483647)]
    [int]$MinNodes
)
$ErrorActionPreference = 'Stop'

try {
    $python = @(Get-Command python -CommandType Application -ErrorAction Stop)[0].Source
} catch {
    Write-Error 'Python is required; candidate output was not changed.' -ErrorAction Continue
    exit 2
}

$converter = Join-Path $PSScriptRoot 'convert_sub.py'
$arguments = @($converter, '--sources-env-file', $EnvFile, '--output', $Output)
if ($PSBoundParameters.ContainsKey('MinNodes')) {
    $arguments += @('--min-nodes', [string]$MinNodes)
}

# Python owns atomic replacement and exit 5 means accepted output with partial source failure.
try {
    & $python @arguments
    $result = $LASTEXITCODE
} catch {
    Write-Error 'Python could not run; candidate refresh was not completed.' -ErrorAction Continue
    exit 2
}
if ($result -eq 0) {
    Write-Host 'Candidate pool written; no service reload or connectivity test performed.'
} elseif ($result -eq 5) {
    Write-Warning 'Candidate pool written with source failures; no service reload performed.'
} else {
    Write-Warning 'Candidate refresh failed; existing output was not replaced.'
}
exit $result

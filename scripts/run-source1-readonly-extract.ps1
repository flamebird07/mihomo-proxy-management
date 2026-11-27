param([ValidateSet('US','SG','JP','HK')][string]$SampleRegion = 'US', [switch]$All)
$ErrorActionPreference = 'Stop'
$taskCore = @(Get-CimInstance Win32_Process -Filter "Name='AtlasCore_amd64.exe'" | Where-Object {
    $_.ExecutablePath -eq 'C:\Program Files (x86)\wmsxwd\AtlasCore_amd64.exe'
})
if ($taskCore.Count -ne 1) { throw 'Expected exactly one running source1 AtlasCore. Keep source1 App open.' }
$taskArguments = @("$PSScriptRoot\extract_source1_readonly.py", '--pid', $taskCore[0].ProcessId, '--sample-region', $SampleRegion)
if ($All) { $taskArguments += '--all' }
$python = $null
try { $python = @(Get-Command python -CommandType Application -ErrorAction Stop)[0].Source } catch {}
if (-not $python) { Write-Error 'python not found in PATH'; exit 2 }
& $python @taskArguments

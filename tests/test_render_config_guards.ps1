# =====================================================================
# tests/test_render_config_guards.ps1
# Standalone validation of the three Render-Config write guards.
# Uses TEMP dirs only - never reads or writes the live C:\mihomo\config.yaml.
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_render_config_guards.ps1
# Exit 0 = all assertions passed.
# =====================================================================
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\lib\common.ps1')

$failures = @()
function Assert([bool]$cond, [string]$msg) {
    if ($cond) { Write-Host "  [PASS] $msg" -ForegroundColor Green }
    else       { Write-Host "  [FAIL] $msg" -ForegroundColor Red; $script:failures += $msg }
}

# Point the install-dir resolver at a throwaway dir so Gate 2 (mihomo -t) can
# never resolve to the real C:\mihomo\mihomo.exe during these tests. Gate 2 then
# warns+skips (no mihomo.exe present), which is the documented best-effort path.
$root = Join-Path $env:TEMP ('rc-guards-' + [Guid]::NewGuid().ToString('N'))
$env:MIHOMO_DIR = $root
New-Item -ItemType Directory -Path $root -Force | Out-Null

# Shared empty env file (template placeholders resolve to empty strings).
$envFile = Join-Path $root 'subscription.env'
Set-Content -LiteralPath $envFile -Value "SUBSCRIPTION_URL=`nSECRET=`n" -Encoding UTF8

try {
    # ---------------- Test A: Gate 3 catches a space-variant dangling ref,
    # ---------------- and Gate 1 backup is restored on failure.
    Write-Host "`n=== Test A: Gate3 dangling ref -> rollback ===" -ForegroundColor Cyan
    $dirA = Join-Path $root 'A'; New-Item -ItemType Directory -Path $dirA -Force | Out-Null
    $tplA = Join-Path $dirA 'tpl.yaml'
    # node declared WITH a space; group references it WITHOUT the space.
    $tplAContent = @'
proxies:
  - name: "日本 06-×1"
proxy-groups:
  - name: PROXY
    type: select
    proxies:
      - "日本06-×1"
      - DIRECT
'@
    Set-Content -LiteralPath $tplA -Value $tplAContent -Encoding UTF8
    $outA = Join-Path $dirA 'config.yaml'
    $goodA = "proxies:`n  - name: GOOD`n"
    Set-Content -LiteralPath $outA -Value $goodA -Encoding UTF8   # pre-existing good config

    $threw = $false
    try { Render-Config -EnvPath $envFile -TemplatePath $tplA -OutPath $outA } catch { $threw = $true }

    Assert $threw 'Render-Config threw on dangling group ref'
    $restoredRaw = Get-Content $outA -Raw
    Assert ((Test-Path $outA) -and ($restoredRaw -match 'name: GOOD') -and ($restoredRaw -notmatch '日本')) 'previous good config.yaml restored after failure'
    $bakA = @(Get-ChildItem -LiteralPath $dirA -Filter 'config.yaml.bak-guard-*' -ErrorAction SilentlyContinue)
    Assert ($bakA.Count -ge 1) 'Gate1 timestamped backup was created'

    # ---------------- Test B: happy path (valid refs) writes and keeps the file.
    Write-Host "`n=== Test B: valid config -> write kept ===" -ForegroundColor Cyan
    $dirB = Join-Path $root 'B'; New-Item -ItemType Directory -Path $dirB -Force | Out-Null
    $tplB = Join-Path $dirB 'tpl.yaml'
    $tplBContent = @'
proxies:
  - name: "日本 06-×1"
  - name: "US-01"
proxy-groups:
  - name: AUTO
    type: url-test
    proxies:
      - "日本 06-×1"
      - "US-01"
  - name: PROXY
    type: select
    proxies:
      - AUTO
      - DIRECT
      - REJECT
'@
    Set-Content -LiteralPath $tplB -Value $tplBContent -Encoding UTF8
    $outB = Join-Path $dirB 'config.yaml'

    Render-Config -EnvPath $envFile -TemplatePath $tplB -OutPath $outB
    Assert (Test-Path $outB) 'valid config written and kept'
    $wrote = Get-Content $outB -Raw
    Assert ($wrote -match '日本 06-×1') 'written content preserves exact node name (space + ×)'

    # ---------------- Test C: fresh write (no pre-existing file) that fails Gate3
    # ---------------- removes the rejected file rather than leaving a bad config.
    Write-Host "`n=== Test C: fresh dangling write -> rejected file removed ===" -ForegroundColor Cyan
    $dirC = Join-Path $root 'C'; New-Item -ItemType Directory -Path $dirC -Force | Out-Null
    $tplC = Join-Path $dirC 'tpl.yaml'
    Set-Content -LiteralPath $tplC -Value $tplAContent -Encoding UTF8
    $outC = Join-Path $dirC 'config.yaml'   # does NOT exist beforehand

    $threwC = $false
    try { Render-Config -EnvPath $envFile -TemplatePath $tplC -OutPath $outC } catch { $threwC = $true }
    Assert $threwC 'Render-Config threw on fresh dangling config'
    Assert (-not (Test-Path $outC)) 'rejected fresh config.yaml removed (no bad file left behind)'
}
finally {
    Remove-Item $env:MIHOMO_DIR -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item Env:\MIHOMO_DIR -ErrorAction SilentlyContinue
}

Write-Host ''
if ($failures.Count -eq 0) {
    Write-Host 'ALL GUARD TESTS PASSED' -ForegroundColor Green
    exit 0
} else {
    Write-Host ("{0} GUARD TEST(S) FAILED" -f $failures.Count) -ForegroundColor Red
    exit 1
}

# =====================================================================
# tests/test_deploy_ps1.ps1
# Windows smoke test for the bootstrap helpers:
#   - Test-EnvConfigured / Test-FeishuPoolMode over three subscription.env shapes
#   - Get-DeployPython returns an absolute interpreter path when Python exists
# Uses TEMP dirs only; never touches the live install dir or any credential.
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_deploy_ps1.ps1
# Exit 0 = all assertions passed.
# =====================================================================
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\lib\common.ps1')

$failures = @()
function Assert([bool]$cond, [string]$msg) {
    if ($cond) { Write-Host "  [PASS] $msg" -ForegroundColor Green }
    else       { Write-Host "  [FAIL] $msg" -ForegroundColor Red; $script:failures += $msg }
}

$tmpRoot = if ($env:TEMP) { $env:TEMP } else { [System.IO.Path]::GetTempPath() }
$root = Join-Path $tmpRoot ('deploy-ps1-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root -Force | Out-Null

try {
    $urlOnly = Join-Path $root 'url.env'
    Set-Content -LiteralPath $urlOnly -Encoding UTF8 -Value "SUBSCRIPTION_URL=https://sub.example.net/api?token=x`n"

    $feishuOnly = Join-Path $root 'feishu.env'
    Set-Content -LiteralPath $feishuOnly -Encoding UTF8 -Value "FEISHU_APP_ID=cli_demo`nFEISHU_APP_SECRET=demo`nFEISHU_BITABLE_APP_TOKEN=demo`n"

    $empty = Join-Path $root 'empty.env'
    Set-Content -LiteralPath $empty -Encoding UTF8 -Value "`n"

    Write-Host "`n=== Test A: subscription URL only ===" -ForegroundColor Cyan
    Assert (Test-EnvConfigured -Path $urlOnly) 'URL-only file is configured'
    Assert (-not (Test-FeishuPoolMode -EnvPath $urlOnly)) 'URL-only file is not Feishu-pool mode'

    Write-Host "`n=== Test B: Feishu credentials only ===" -ForegroundColor Cyan
    Assert (Test-EnvConfigured -Path $feishuOnly) 'Feishu-only file is configured (pool mode)'
    Assert (Test-FeishuPoolMode -EnvPath $feishuOnly) 'Feishu-only file is Feishu-pool mode'

    Write-Host "`n=== Test C: neither ===" -ForegroundColor Cyan
    Assert (-not (Test-EnvConfigured -Path $empty)) 'empty file is not configured'
    Assert (-not (Test-FeishuPoolMode -EnvPath $empty)) 'empty file is not Feishu-pool mode'

    Write-Host "`n=== Test D: Get-DeployPython ===" -ForegroundColor Cyan
    $deploy = Join-Path $PSScriptRoot '..\scripts\deploy.ps1'
    $tokens = $null; $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $deploy), [ref]$tokens, [ref]$errs)
    $fn = $ast.Find({
        param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-DeployPython'
    }, $true)
    Assert ($null -ne $fn) 'deploy.ps1 defines Get-DeployPython'
    if ($fn) {
        $script:DeployPython = $null
        Invoke-Expression $fn.Extent.Text
        $hasPython = [bool](Get-Command python -CommandType Application -ErrorAction SilentlyContinue)
        $py = Get-DeployPython
        if ($hasPython) {
            Assert ([bool]$py) 'Get-DeployPython found an interpreter'
            Assert ($py -and [System.IO.Path]::IsPathRooted($py)) 'Get-DeployPython returned an absolute path'
        } else {
            Write-Host '  [SKIP] no python on PATH; absolute-path assertion skipped' -ForegroundColor Yellow
        }
    }

    Write-Host "`n=== Test E: fixed PowerShell -File action ===" -ForegroundColor Cyan
    $psExe = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
    $scriptWithSpace = 'C:\repo with space\scripts\sync-feishu-bitable.ps1'
    $dirUnicode = 'C:\安装 目录\mihomo'
    $pyExe = 'C:\Program Files\Python311\python.exe'
    $spec = New-PowerShellFileActionSpec -ScriptPath $scriptWithSpace `
        -ScriptArguments @('-Run', '-InstallDir', $dirUnicode, '-Interpreter', $pyExe) `
        -PowerShellPath $psExe
    Assert ($spec.Execute -eq $psExe) 'action uses the fixed absolute powershell path'
    Assert ($spec.Execute -notmatch '(?i)^powershell(\.exe)?$') 'action never resolves powershell from PATH'
    Assert ($spec.Argument -like '*-File*') 'action passes -File'
    Assert ($spec.Argument.Contains($scriptWithSpace)) 'script path with spaces is preserved verbatim'
    Assert ($spec.Argument.Contains($dirUnicode)) 'unicode install dir is preserved verbatim'
    Assert ($spec.Argument.Contains($pyExe)) 'fixed interpreter path is preserved verbatim'
    Assert ($spec.Argument -like '*-Run*') 'run-mode switch is present'

    Write-Host "`n=== Test F: command-line quoting ===" -ForegroundColor Cyan
    Assert ((ConvertTo-CommandLineArgument 'C:\no-space\py.exe') -eq 'C:\no-space\py.exe') 'no-space value is left unquoted'
    Assert ((ConvertTo-CommandLineArgument 'C:\with space\py.exe') -eq '"C:\with space\py.exe"') 'space value is quoted intact'
    Assert ((ConvertTo-CommandLineArgument $dirUnicode) -eq ('"{0}"' -f $dirUnicode)) 'unicode value is quoted intact'
    Assert ((ConvertTo-CommandLineArgument 'C:\my dir\') -eq '"C:\my dir\\"') 'trailing backslash is doubled inside quotes'
    $fixed = Get-WindowsPowerShellPath -SystemRoot 'C:\Windows'
    Assert ($fixed -eq $psExe) 'powershell path is absolute and fixed'

    Write-Host "`n=== Test G: ACL grant argv ===" -ForegroundColor Cyan
    $sid = 'S-1-5-21-1004336348-1177238915-682003330-1001'
    $readArgv = New-AclGrantArguments -Path 'C:\mihomo\subscription.env' -Access Read -Sid $sid
    Assert ($readArgv.Count -eq 3) 'read grant has three argv parts'
    Assert ($readArgv[1] -eq '/grant') 'grant is additive (not /grant:r)'
    Assert ($readArgv[2] -eq ("*{0}:(R)" -f $sid)) 'read grant is *SID:(R)'
    $modArgv = New-AclGrantArguments -Path 'C:\安装 目录\logs' -Access Modify -Sid $sid
    Assert ($modArgv[2] -eq ("*{0}:(OI)(CI)M" -f $sid)) 'modify grant inherits to files/subdirs'
    Assert ($modArgv[0] -eq 'C:\安装 目录\logs') 'unicode dir path is preserved in icacls argv'

    Write-Host "`n=== Test H: ACL failure exit code ===" -ForegroundColor Cyan
    $failRunner = { param($command) return 5 }
    $failed = Invoke-AclGrant -Path 'C:\mihomo\subscription.env' -Access Read -Sid $sid -Runner $failRunner
    Assert (-not $failed.Ok) 'nonzero icacls exit is a failure'
    Assert ($failed.ExitCode -eq 5) 'icacls exit code is reported'

    Write-Host "`n=== Test I: ACL authorization order ===" -ForegroundColor Cyan
    $orderRoot = Join-Path $root 'acl-order'
    New-Item -ItemType Directory -Path (Join-Path $orderRoot 'logs') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $orderRoot 'providers') -Force | Out-Null
    $envFile = Join-Path $orderRoot 'subscription.env'
    Set-Content -LiteralPath $envFile -Value 'X=1' -Encoding UTF8
    $logsDir = Join-Path $orderRoot 'logs'
    $providersDir = Join-Path $orderRoot 'providers'
    $script:calls = New-Object System.Collections.Generic.List[string]
    $recorder = { param($command) $script:calls.Add($command[0] + '|' + $command[2]); return 0 }
    $ok = Grant-BootstrapTaskAcls -SubscriptionEnvPath $envFile -LogsDir $logsDir -ProvidersDir $providersDir -Sid $sid -Runner $recorder
    Assert ($ok.Ok) 'acl plan succeeds with a zero-exit runner'
    Assert ($script:calls.Count -eq 3) 'acl plan runs exactly three grants'
    Assert ($script:calls[0] -like "*subscription.env*:(R)") 'first grant is read on subscription.env'
    Assert ($script:calls[1] -like "*logs*:(OI)(CI)M") 'second grant is Modify on logs'
    Assert ($script:calls[2] -like "*providers*:(OI)(CI)M") 'third grant is Modify on providers'

    $script:calls2 = New-Object System.Collections.Generic.List[string]
    $failSecond = { param($command) $script:calls2.Add($command[0]); if ($script:calls2.Count -eq 2) { return 9 }; return 0 }
    $bad = Grant-BootstrapTaskAcls -SubscriptionEnvPath $envFile -LogsDir $logsDir -ProvidersDir $providersDir -Sid $sid -Runner $failSecond
    Assert (-not $bad.Ok) 'acl plan stops on the first nonzero grant'
    Assert ($bad.ExitCode -eq 9) 'failing grant exit code is surfaced'
    Assert ($script:calls2.Count -eq 2) 'no grant runs after the failure'

    $missing = Grant-BootstrapTaskAcls -SubscriptionEnvPath (Join-Path $orderRoot 'missing.env') -LogsDir $logsDir -ProvidersDir $providersDir -Sid $sid -Runner $recorder
    Assert ((-not $missing.Ok) -and ($missing.Reason -eq 'missing')) 'missing credential file blocks registration'

    Write-Host "`n=== Test J: elevated task identity is refused ===" -ForegroundColor Cyan
    Assert (Test-ElevatedTaskIdentity -UserId 'NT AUTHORITY\SYSTEM' -RunLevel Limited) 'SYSTEM is rejected as task owner'
    Assert (Test-ElevatedTaskIdentity -UserId 'CONTOSO\alice' -RunLevel Highest) 'Highest is rejected as run level'
    Assert (-not (Test-ElevatedTaskIdentity -UserId 'CONTOSO\alice' -RunLevel Limited)) 'user at Limited is allowed'
    $threwSystem = $false
    try { Assert-BootstrapTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType Interactive -RunLevel Limited } catch { $threwSystem = $true }
    Assert $threwSystem 'Assert-BootstrapTaskPrincipal refuses SYSTEM'
    $threwHighest = $false
    try { Assert-BootstrapTaskPrincipal -UserId 'CONTOSO\alice' -LogonType Interactive -RunLevel Highest } catch { $threwHighest = $true }
    Assert $threwHighest 'Assert-BootstrapTaskPrincipal refuses Highest'
    $spec2 = New-BootstrapTaskPrincipalSpec -UserId 'CONTOSO\alice'
    Assert (($spec2.LogonType -eq 'Interactive') -and ($spec2.RunLevel -eq 'Limited')) 'default principal spec is Interactive/Limited'
}
finally {
    Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
if ($failures.Count -eq 0) {
    Write-Host 'ALL DEPLOY SMOKE TESTS PASSED' -ForegroundColor Green
    exit 0
} else {
    Write-Host ("{0} DEPLOY SMOKE TEST(S) FAILED" -f $failures.Count) -ForegroundColor Red
    exit 1
}

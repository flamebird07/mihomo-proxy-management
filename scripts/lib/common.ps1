# =====================================================================
# scripts/lib/common.ps1
# 共享函数与常量，被所有脚本引用
# Shared helpers and constants, dot-sourced by every script.
# =====================================================================

$script:ProjectName      = 'mihomo-proxy-management'
$script:TaskName         = 'MihomoProxy'
$script:DefaultInstallDir = 'C:\mihomo'

# Mihomo 官方 release（运行时下载）
# Mihomo official releases (downloaded at install time)
$script:MihomoReleaseApi = 'https://api.github.com/repos/MetaCubeX/mihomo/releases/latest'
$script:MihomoAssetName  = 'mihomo-windows-amd64-v3.zip'   # amd64 v3 (AVX2)
$script:WintunAssetName  = 'wintun-0.14.1.zip'
$script:MihomoRepo       = 'MetaCubeX/mihomo'

function Get-InstallDir {
    if ($env:MIHOMO_DIR -and (Test-Path $env:MIHOMO_DIR)) { return $env:MIHOMO_DIR }

    # 探测常见位置的 subscription.env，取第一个存在的。
    # 避免反向调 Get-EnvPath（那会无限递归）
    foreach ($candidate in @(
        (Join-Path $script:DefaultInstallDir 'subscription.env'),
        (Join-Path (Get-RepoRoot) 'subscription.env'),
        (Join-Path (Get-RepoRoot) 'install_dir\subscription.env')
    )) {
        if (Test-Path $candidate) {
            $env = Read-EnvFile $candidate
            if ($env['INSTALL_DIR'] -and (Test-Path $env['INSTALL_DIR'])) {
                return $env['INSTALL_DIR']
            }
        }
    }

    if (Test-Path $script:DefaultInstallDir) { return $script:DefaultInstallDir }
    return $script:DefaultInstallDir
}

function Get-RepoRoot {
    # scripts/lib/common.ps1 -> scripts/lib -> scripts -> repo root
    return (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}

function Get-TemplatePath {
    return (Join-Path (Get-RepoRoot) 'config\config.yaml.template')
}

function Get-EnvPath {
    $dir = Get-InstallDir
    return (Join-Path $dir 'subscription.env')
}

function Get-ConfigPath {
    $dir = Get-InstallDir
    return (Join-Path $dir 'config.yaml')
}

function Get-MihomoExePath {
    $dir = Get-InstallDir
    return (Join-Path $dir 'mihomo.exe')
}

function Get-LogPath {
    $dir = Get-InstallDir
    return (Join-Path $dir 'logs\mihomo.log')
}

function Ensure-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $msg = "需要管理员权限运行。请右键 PowerShell -> 以管理员身份运行。`n" +
               "Run as Administrator (required for TUN / scheduled task)."
        throw $msg
    }
}

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ('=== {0} ===' -f $Title) -ForegroundColor Cyan
}

function Get-ControllerSecret {
    # controller 密钥只来自 subscription.env（本地机密文件，勿提交）。
    # The controller secret comes only from subscription.env (local, secret).
    $env = Read-EnvFile (Get-EnvPath)
    $sec = if ($env['SECRET']) { ([string]$env['SECRET']).Trim() } else { '' }
    return $sec
}

function Invoke-MihomoApi {
    # 所有 controller 调用必须经过本函数：统一携带 Bearer 认证。
    # 密钥缺失时抛错 fail-closed，绝不发起未认证请求（D16-1）。
    # Every controller call goes through this function with the Bearer header.
    # A missing secret is a hard error: no unauthenticated fallback exists.
    param(
        [Parameter(Mandatory)][string]$Path,
        [string]$Method = 'GET',
        [int]$TimeoutSec = 5
    )
    if (-not $Path.StartsWith('/')) { $Path = '/' + $Path }
    $secret = Get-ControllerSecret
    if (-not $secret) {
        throw 'controller secret (SECRET in subscription.env) is empty; refusing unauthenticated API call'
    }
    $headers = @{ Authorization = "Bearer $secret" }
    return Invoke-RestMethod -Uri ("http://127.0.0.1:9090" + $Path) -Headers $headers -Method $Method -TimeoutSec $TimeoutSec
}

function Write-Ok   { param($m) Write-Host ('[OK] {0}'   -f $m) -ForegroundColor Green }
function Write-Warn { param($m) Write-Host ('[WARN] {0}' -f $m) -ForegroundColor Yellow }
function Write-Err  { param($m) Write-Host ('[ERR] {0}'  -f $m) -ForegroundColor Red }

function Read-EnvFile {
    # 解析 KEY=VALUE（忽略注释 / 空行 / 引号）
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path $Path)) { return $map }
    Get-Content $Path -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#')) { return }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { return }
        $k = $line.Substring(0, $eq).Trim()
        $v = $line.Substring($eq + 1).Trim().Trim('"', "'")
        $map[$k] = $v
    }
    return $map
}

function Test-FeishuPoolMode {
    # 飞书供池模式：没有可用的 SUBSCRIPTION_URL，但 FEISHU_APP_ID 与
    # FEISHU_APP_SECRET 都非空。Feishu-pool mode: no usable subscription URL,
    # but both Feishu app credentials are present.
    param([string]$EnvPath)
    $env = Read-EnvFile $EnvPath
    $url = if ($env.ContainsKey('SUBSCRIPTION_URL')) { [string]$env['SUBSCRIPTION_URL'] } else { '' }
    if (-not [string]::IsNullOrWhiteSpace($url) -and $url -notmatch 'example\.(com|invalid)') {
        return $false
    }
    $id  = if ($env.ContainsKey('FEISHU_APP_ID'))     { [string]$env['FEISHU_APP_ID'] }     else { '' }
    $sec = if ($env.ContainsKey('FEISHU_APP_SECRET')) { [string]$env['FEISHU_APP_SECRET'] } else { '' }
    return (-not [string]::IsNullOrWhiteSpace($id)) -and (-not [string]::IsNullOrWhiteSpace($sec))
}

function Test-EnvConfigured {
    param([string]$Path)
    $env = Read-EnvFile $Path
    $url = if ($env.ContainsKey('SUBSCRIPTION_URL')) { [string]$env['SUBSCRIPTION_URL'] } else { '' }
    if (-not [string]::IsNullOrWhiteSpace($url) -and $url -notmatch 'example\.(com|invalid)') {
        return $true
    }
    # 没有订阅地址时，飞书供池模式也算已配置 / Feishu-pool mode counts as configured.
    return (Test-FeishuPoolMode -EnvPath $Path)
}

function Render-Config {
    # 用 subscription.env 替换模板中的占位符，输出到 install_dir\config.yaml
    # 写入前后有三道防护门（崩溃保护）：
    #   Gate1 覆盖前先做时间戳备份；Gate3 校验 proxy-groups 引用；Gate2 mihomo -t。
    #   任一门失败都会回滚到备份（或删除刚写入的全新文件）并抛错，绝不留下坏配置。
    param([string]$EnvPath, [string]$TemplatePath, [string]$OutPath, [switch]$FeishuPool)
    $env = Read-EnvFile $EnvPath
    $url  = if ($env['SUBSCRIPTION_URL'])    { $env['SUBSCRIPTION_URL'] }    else { '' }
    $intv = if ($env['SUBSCRIPTION_INTERVAL']) { $env['SUBSCRIPTION_INTERVAL'] } else { '60' }
    $sec  = if ($env['SECRET']) { $env['SECRET'] } else { '' }

    $tpl = Get-Content $TemplatePath -Raw -Encoding UTF8
    # 用 .Replace() 而非 -replace：后者把 $ 当正则替换串，会改写含 $ 的 URL/密钥
    $out = $tpl.Replace('__SUBSCRIPTION_URL__',      $url)
    $out = $out.Replace('__SUBSCRIPTION_INTERVAL__', $intv)
    $out = $out.Replace('__SECRET__',                $sec)

    $dir = Split-Path $OutPath -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

    # ---------- Gate 1: 覆盖前备份 / timestamped backup before overwrite ----------
    $backup = $null
    if (Test-Path -LiteralPath $OutPath) {
        $backup = ('{0}.bak-guard-{1}' -f $OutPath, (Get-Date).ToString('yyyyMMdd-HHmmss'))
        Copy-Item -LiteralPath $OutPath -Destination $backup -Force
        Write-Ok "已备份现有 config.yaml / backed up existing config -> $(Split-Path $backup -Leaf)"
    }

    Set-Content -LiteralPath $OutPath -Value $out -Encoding UTF8 -NoNewline

    try {
        # 优先使用引导阶段选定的绝对解释器，避免 PATH 里换了另一个 Python。
        # Prefer the interpreter the bootstrap pinned; fall back to PATH python.
        $python = $null
        if ($env:MPM_PYTHON -and (Test-Path -LiteralPath $env:MPM_PYTHON -PathType Leaf)) {
            $python = $env:MPM_PYTHON
        }
        if (-not $python) {
            try { $python = @(Get-Command python -CommandType Application -ErrorAction Stop)[0].Source } catch {}
        }

        # ---------- Gate 3: proxy-groups 引用校验 / group-ref check ----------
        # 需要 python；缺失时跳过（Gate2 的 mihomo -t 仍会兜底捕获悬空引用）。
        $checker = Join-Path $PSScriptRoot 'check_group_refs.py'
        if ($python -and (Test-Path $checker)) {
            $refOut = & $python $checker $OutPath --json 2>&1
            if ($LASTEXITCODE -eq 1) {
                throw "proxy-groups 引用校验失败（存在悬空成员）/ dangling group refs: $refOut"
            } elseif ($LASTEXITCODE -ne 0) {
                Write-Warn "group-ref 检查器异常（exit $LASTEXITCODE）/ checker error: $refOut"
            } else {
                Write-Ok 'proxy-groups 引用校验通过 / group-ref check passed'
            }
        } else {
            Write-Warn '跳过 group-ref 校验（python 或检查器不可用）/ skipped (python/checker unavailable)'
        }

        # ---------- 飞书供池：把 subscription provider 改成 file provider ----------
        # Feishu-pool hosts render with an empty SUBSCRIPTION_URL; render_config.py
        # rewrites only proxy-providers.subscription into the managed file pool.
        if ($FeishuPool) {
            if (-not $python) {
                throw 'FeishuPool 渲染需要 python，但未找到 / python not found for FeishuPool rendering'
            }
            $renderer = Join-Path (Get-RepoRoot) 'scripts\render_config.py'
            if (-not (Test-Path -LiteralPath $renderer)) {
                throw "缺少 render_config.py / missing render_config.py: $renderer"
            }
            $renderOut = & $python $renderer --input $OutPath 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw "render_config 失败 / failed exit=$LASTEXITCODE : $renderOut"
            }
            Write-Ok '飞书节点池 provider 已写入 / Feishu pool provider applied'
        }

        # ---------- Gate 2: mihomo -t（重载前必须通过）/ config test ----------
        $mihomoExe = Join-Path $dir 'mihomo.exe'
        if (-not (Test-Path $mihomoExe)) { $mihomoExe = Get-MihomoExePath }
        if (Test-Path $mihomoExe) {
            $testOut = & $mihomoExe -t -d $dir 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw "mihomo -t 校验失败 / config test failed: $testOut"
            }
            Write-Ok 'mihomo -t 校验通过 / config test passed'
        } else {
            Write-Warn '跳过 mihomo -t（未找到 mihomo.exe）/ skipped (mihomo.exe not found)'
        }
    } catch {
        # 任一门失败：回滚到上一个可用配置（或删除刚写入的全新坏文件）后抛出。
        if ($backup) {
            Copy-Item -LiteralPath $backup -Destination $OutPath -Force
            Write-Err "已从备份回滚 config.yaml / restored from backup: $(Split-Path $backup -Leaf)"
        } elseif (Test-Path -LiteralPath $OutPath) {
            Remove-Item -LiteralPath $OutPath -Force
            Write-Err '已删除未通过校验的新 config.yaml / removed rejected new config'
        }
        throw
    }
}

# mihomo 可能以两个二进制名运行（历史遗留的 mihomo-windows-amd64.exe），
# 只按 "mihomo" 查会漏掉第二个进程，导致 stop/restart 后残留实例。
# mihomo may run under two exe names (legacy mihomo-windows-amd64.exe);
# matching only "mihomo" misses the second instance.
$script:MihomoProcessNames = @('mihomo', 'mihomo-windows-amd64')

function Get-MihomoProcesses {
    Get-Process -Name $script:MihomoProcessNames -ErrorAction SilentlyContinue
}

function Get-MihomoProcess {
    # 兼容旧调用方：返回第一个进程 / legacy single-process accessor
    Get-MihomoProcesses | Select-Object -First 1
}

function Get-MihomoTask {
    Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
}

function Stop-Mihomo {
    Write-Section 'Stopping mihomo'

    # 必须先停计划任务再杀进程：任务带"失败自动重启"策略，
    # 直接杀进程会让 Task Scheduler 在 1 分钟内把 mihomo 又拉起来（幽灵进程）。
    # Stop the task first: its failure-restart policy would otherwise
    # respawn mihomo right after we kill the process.
    $t = Get-MihomoTask
    if ($t) { Stop-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue }

    $procs = @(Get-MihomoProcesses)
    if ($procs.Count -eq 0) {
        Write-Warn 'mihomo is not running'
        return
    }

    foreach ($p in $procs) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Write-Host ('[OK] stopped PID={0} ({1})' -f $p.Id, $p.ProcessName)
    }

    # 等待进程真正退出（端口/TUN 释放），否则下一个实例会绑定失败
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and @(Get-MihomoProcesses).Count -gt 0) {
        Start-Sleep -Milliseconds 500
    }
    if (@(Get-MihomoProcesses).Count -gt 0) {
        Write-Err '仍有 mihomo 进程未退出（可能是 SYSTEM 权限进程，需管理员）/ processes still alive'
    } else {
        Write-Ok 'all mihomo processes stopped'
    }
}

function Wait-MihomoReady {
    # readiness 也必须走认证接口；未认证 200 不算就绪（默认 CAP 下 401）。
    # Readiness uses the authenticated API as well.
    param([int]$TimeoutSec = 15)
    for ($i = 0; $i -lt $TimeoutSec; $i++) {
        try {
            $r = Invoke-MihomoApi -Path '/version' -TimeoutSec 2
            if ($r) { return $true }
        } catch { }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Get-MihomoVersion {
    try {
        $r = Invoke-MihomoApi -Path '/version' -TimeoutSec 3
        return $r.version
    } catch { return $null }
}

# =====================================================================
# Bootstrap task identity, ACL grants and fixed PowerShell -File actions.
# The two bootstrap-registered tasks (Feishu sync, node pipeline) must run
# as the deploying user at Interactive/Limited - never SYSTEM/Highest - and
# must be handed an absolute interpreter and install dir.  These helpers are
# pure/injectable so the PowerShell smoke test can exercise them on any host.
# =====================================================================

function Get-CurrentUserName {
    return ([Security.Principal.WindowsIdentity]::GetCurrent()).Name
}

function Get-CurrentUserSid {
    return ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
}

function Test-ElevatedTaskIdentity {
    # True for an identity that must never own a bootstrap script task:
    # SYSTEM / LocalService / NetworkService, or any non-Limited run level.
    param([string]$UserId = '', [string]$RunLevel = '')
    if ($RunLevel -and $RunLevel -ne 'Limited') { return $true }
    if (-not $UserId) { return $true }
    $u = $UserId.Trim()
    if ($u -match '^S-1-5-(18|19|20)$') { return $true }
    if ($u -match '(?i)(^|\\)(SYSTEM|LOCAL SERVICE|NETWORK SERVICE)$') { return $true }
    return $false
}

function Assert-BootstrapTaskPrincipal {
    # Refuse to register a task its deployer could abuse by rewriting a file
    # the task later executes: the owner must be the current user, interactive
    # and unelevated.
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    if ($RunLevel -ne 'Limited') {
        throw "refusing scheduled task run level '$RunLevel' (must be Limited)"
    }
    if ($LogonType -ne 'Interactive') {
        throw "refusing scheduled task logon type '$LogonType' (must be Interactive)"
    }
    if (Test-ElevatedTaskIdentity -UserId $UserId -RunLevel $RunLevel) {
        throw "refusing service-account scheduled task identity '$UserId'"
    }
}

function New-BootstrapTaskPrincipalSpec {
    # The parts of New-ScheduledTaskPrincipal that matter for safety, returned
    # as data so tests can assert them without the ScheduledTasks module.
    param([string]$UserId = '')
    if (-not $UserId) { $UserId = Get-CurrentUserName }
    return [pscustomobject]@{
        UserId    = $UserId
        LogonType = 'Interactive'
        RunLevel  = 'Limited'
    }
}

function New-AclGrantArguments {
    # icacls argv for an additive (!) grant using a well-known SID.  Read is a
    # plain file ACE; Modify inherits to files (OI) and subdirectories (CI).
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][ValidateSet('Read', 'Modify')][string]$Access,
        [Parameter(Mandatory)][string]$Sid
    )
    $grant = if ($Access -eq 'Modify') { "*${Sid}:(OI)(CI)M" } else { "*${Sid}:(R)" }
    return @($Path, '/grant', $grant)
}

function Invoke-AclGrant {
    # Run one icacls grant and report its exit code.  ``Runner`` is injectable
    # (argv -> exit code) so failure handling is testable off Windows.
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][ValidateSet('Read', 'Modify')][string]$Access,
        [Parameter(Mandatory)][string]$Sid,
        [scriptblock]$Runner
    )
    $argv = New-AclGrantArguments -Path $Path -Access $Access -Sid $Sid
    if (-not $Runner) {
        $Runner = {
            param($command)
            try {
                & icacls.exe @command 2>&1 | Out-Null
                return $LASTEXITCODE
            } catch {
                return -1
            }
        }
    }
    $code = & $Runner $argv
    return [pscustomobject]@{
        Ok        = ($code -eq 0)
        ExitCode  = $code
        Arguments = $argv
        Path      = $Path
        Access    = $Access
    }
}

function Grant-BootstrapTaskAcls {
    # Add the deploying user's access before registering a task for that user:
    # read on the credential file, Modify (inherited) on logs and providers.
    # Stops at the first failure so no dependent task is registered afterwards.
    param(
        [Parameter(Mandatory)][string]$SubscriptionEnvPath,
        [Parameter(Mandatory)][string]$LogsDir,
        [Parameter(Mandatory)][string]$ProvidersDir,
        [Parameter(Mandatory)][string]$Sid,
        [scriptblock]$Runner
    )
    $plan = @(
        [pscustomobject]@{ Path = $SubscriptionEnvPath; Access = 'Read' },
        [pscustomobject]@{ Path = $LogsDir; Access = 'Modify' },
        [pscustomobject]@{ Path = $ProvidersDir; Access = 'Modify' }
    )
    $applied = New-Object System.Collections.Generic.List[object]
    foreach ($step in $plan) {
        if (-not (Test-Path -LiteralPath $step.Path)) {
            return [pscustomobject]@{
                Ok = $false; Reason = 'missing'; FailedPath = $step.Path
                ExitCode = $null; Applied = $applied.ToArray()
            }
        }
        $result = Invoke-AclGrant -Path $step.Path -Access $step.Access -Sid $Sid -Runner $Runner
        $applied.Add($result) | Out-Null
        if (-not $result.Ok) {
            return [pscustomobject]@{
                Ok = $false; Reason = 'icacls'; FailedPath = $step.Path
                ExitCode = $result.ExitCode; Applied = $applied.ToArray()
            }
        }
    }
    return [pscustomobject]@{
        Ok = $true; Reason = ''; FailedPath = ''
        ExitCode = 0; Applied = $applied.ToArray()
    }
}

function Get-WindowsPowerShellPath {
    # Absolute Windows PowerShell 5.1, never a bare "powershell" from PATH.
    # String concatenation (not Join-Path) keeps the Windows form on POSIX hosts.
    param([string]$SystemRoot = '')
    if (-not $SystemRoot) { $SystemRoot = $env:SystemRoot }
    if (-not $SystemRoot) { $SystemRoot = 'C:\Windows' }
    return ($SystemRoot.TrimEnd([char[]]@('\', '/')) + '\System32\WindowsPowerShell\v1.0\powershell.exe')
}

function ConvertTo-CommandLineArgument {
    # CreateProcess-style quoting: preserves spaces, Unicode and a trailing
    # backslash (which would otherwise escape the closing quote).
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    if ($Value -notmatch '[\s"]') { return $Value }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $backslashes = 0
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq '\') { $backslashes++; continue }
        if ($ch -eq '"') {
            [void]$sb.Append('\' * (($backslashes * 2) + 1))
            [void]$sb.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$sb.Append('\' * $backslashes)
            $backslashes = 0
        }
        [void]$sb.Append($ch)
    }
    if ($backslashes -gt 0) { [void]$sb.Append('\' * ($backslashes * 2)) }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function New-PowerShellFileActionSpec {
    # A Task Scheduler action that runs the absolute Windows PowerShell with a
    # -File entry point, passing the fixed interpreter and install dir exactly.
    param(
        [Parameter(Mandatory)][string]$ScriptPath,
        [string[]]$ScriptArguments = @(),
        [string]$PowerShellPath = ''
    )
    if (-not $PowerShellPath) { $PowerShellPath = Get-WindowsPowerShellPath }
    $parts = New-Object System.Collections.Generic.List[string]
    $parts.Add('-NoProfile')
    $parts.Add('-ExecutionPolicy')
    $parts.Add('Bypass')
    $parts.Add('-File')
    $parts.Add((ConvertTo-CommandLineArgument $ScriptPath))
    foreach ($value in $ScriptArguments) {
        $parts.Add((ConvertTo-CommandLineArgument $value))
    }
    return [pscustomobject]@{
        Execute   = $PowerShellPath
        Argument  = ($parts -join ' ')
        Arguments = $parts.ToArray()
    }
}
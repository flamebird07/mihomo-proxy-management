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

function Test-EnvConfigured {
    param([string]$Path)
    $env = Read-EnvFile $Path
    if (-not $env.ContainsKey('SUBSCRIPTION_URL') -or
        [string]::IsNullOrWhiteSpace($env['SUBSCRIPTION_URL']) -or
        $env['SUBSCRIPTION_URL'] -match 'example\.(com|invalid)') {
        return $false
    }
    return $true
}

function Render-Config {
    # 用 subscription.env 替换模板中的占位符，输出到 install_dir\config.yaml
    # 写入前后有三道防护门（崩溃保护）：
    #   Gate1 覆盖前先做时间戳备份；Gate3 校验 proxy-groups 引用；Gate2 mihomo -t。
    #   任一门失败都会回滚到备份（或删除刚写入的全新文件）并抛错，绝不留下坏配置。
    param([string]$EnvPath, [string]$TemplatePath, [string]$OutPath)
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
        # ---------- Gate 3: proxy-groups 引用校验 / group-ref check ----------
        # 需要 python；缺失时跳过（Gate2 的 mihomo -t 仍会兜底捕获悬空引用）。
        $checker = Join-Path $PSScriptRoot 'check_group_refs.py'
        $python = $null
        try { $python = @(Get-Command python -CommandType Application -ErrorAction Stop)[0].Source } catch {}
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
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
        $env['SUBSCRIPTION_URL'] -match 'example\.com') {
        return $false
    }
    return $true
}

function Render-Config {
    # 用 subscription.env 替换模板中的占位符，输出到 install_dir\config.yaml
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
    Set-Content -Path $OutPath -Value $out -Encoding UTF8 -NoNewline
}

function Get-MihomoProcess {
    Get-Process mihomo -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Get-MihomoTask {
    Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
}

function Stop-Mihomo {
    Write-Section 'Stopping mihomo'
    $p = Get-MihomoProcess
    if ($p) {
        Stop-Process -Id $p.Id -Force
        Start-Sleep -Seconds 2
        Write-Ok "Stopped PID=$($p.Id)"
    } else {
        Write-Warn 'mihomo is not running'
    }
}

function Wait-MihomoReady {
    param([int]$TimeoutSec = 15)
    for ($i = 0; $i -lt $TimeoutSec; $i++) {
        try {
            $r = Invoke-RestMethod -Uri 'http://127.0.0.1:9090/version' -TimeoutSec 2
            if ($r) { return $true }
        } catch { }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Get-MihomoVersion {
    try {
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:9090/version' -TimeoutSec 3
        return $r.version
    } catch { return $null }
}
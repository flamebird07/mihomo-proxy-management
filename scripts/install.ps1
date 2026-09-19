# =====================================================================
# install.ps1 - 一键安装 / 重新生成配置
# One-shot installer / config re-generator.
#
# 用法 / Usage:
#   1. 把 config\subscription.env.example 复制为 <install_dir>\subscription.env
#      Copy config\subscription.env.example to <install_dir>\subscription.env
#   2. 编辑 subscription.env，填入 SUBSCRIPTION_URL
#      Edit subscription.env, set SUBSCRIPTION_URL
#   3. 管理员 PowerShell 运行:  .\scripts\install.ps1
#      Run from elevated PowerShell.
# =====================================================================

[CmdletBinding()]
param(
    [string]$InstallDir,
    [switch]$SkipDownload,    # 跳过二进制下载（仅重新生成配置 + 注册计划任务）
    [switch]$SkipTask,        # 跳过注册计划任务
    [switch]$SkipConfigRender, # 保留 install_dir 中已存在的 config.yaml（手工维护的内联节点配置）
    [switch]$Force            # 强制重新下载
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Ensure-Admin

if (-not $InstallDir) { $InstallDir = Get-InstallDir }
Write-Section "Install dir: $InstallDir"

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallDir 'providers') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallDir 'logs')      -Force | Out-Null

# ---------- subscription.env ----------
if (-not $SkipConfigRender) {
    $envPath = Join-Path $InstallDir 'subscription.env'
    if (-not (Test-Path $envPath)) {
        Write-Host "subscription.env 不存在，从模板创建空文件并打开 / creating from template"
        Copy-Item (Join-Path (Get-RepoRoot) 'config\subscription.env.example') $envPath -Force
        Start-Process notepad.exe $envPath
        throw "请先填写 subscription.env 中的 SUBSCRIPTION_URL，再重新运行本脚本 / `nPlease fill SUBSCRIPTION_URL in subscription.env and re-run."
    }

    if (-not (Test-EnvConfigured $envPath)) {
        Write-Err "subscription.env 中的 SUBSCRIPTION_URL 未配置或仍为示例值 / SUBSCRIPTION_URL not configured"
        Start-Process notepad.exe $envPath
        throw "请编辑 $envPath 后重试 / Please edit it then re-run."
    }

    # ---------- 渲染 config ----------
    Write-Section 'Rendering config'
    Render-Config `
        -EnvPath     $envPath `
        -TemplatePath (Get-TemplatePath) `
        -OutPath     (Join-Path $InstallDir 'config.yaml')
    Write-Ok "config.yaml 已生成 / generated at $(Join-Path $InstallDir 'config.yaml')"
} else {
    if (-not (Test-Path (Join-Path $InstallDir 'config.yaml'))) {
        throw "-SkipConfigRender 需要 $InstallDir\config.yaml 已存在 / config.yaml must exist"
    }
    Write-Ok "跳过配置渲染，保留现有 config.yaml / keeping existing config.yaml"
}

# ---------- 下载二进制 / Geo 数据 ----------
if (-not $SkipDownload) {
    $exe = Join-Path $InstallDir 'mihomo.exe'
    $wintun = Join-Path $InstallDir 'wintun.dll'

    $needExe    = $Force -or -not (Test-Path $exe)
    $needWintun = $Force -or -not (Test-Path $wintun)
    $needGeo    = $Force -or -not (Test-Path (Join-Path $InstallDir 'GeoIP.dat')) -or -not (Test-Path (Join-Path $InstallDir 'GeoSite.dat'))

    if ($needExe -or $needWintun -or $needGeo) {
        Write-Section 'Resolving latest mihomo release'
        $rel = $null
        try {
            $apiHeaders = @{ 'User-Agent' = 'mihomo-proxy-management-installer' }
            if ($env:GH_TOKEN) { $apiHeaders['Authorization'] = "Bearer $env:GH_TOKEN" }
            $rel = Invoke-RestMethod -Uri $script:MihomoReleaseApi -Headers $apiHeaders -TimeoutSec 20
            $ver = $rel.tag_name
            Write-Ok "latest version: $ver"
        } catch {
            Write-Warn "GitHub API 失败，改用无校验直链下载 / API failed, fall back to direct URL without digest"
            $ver = $null
        }

        if ($needExe) {
            Write-Section 'Downloading mihomo.exe'
            $expectedSha = ''
            if ($rel) {
                $asset = $rel.assets | Where-Object { $_.name -eq $script:MihomoAssetName } | Select-Object -First 1
                if (-not $asset) { throw "release 中未找到 $($script:MihomoAssetName)" }
                # 提取 sha256（digest 形如 "sha256:abc..."）
                $expectedSha = ($asset.digest -split ':')[1]
                $url = $asset.browser_download_url
            } else {
                # API 失败：用固定版本直链（无 digest 校验）
                $url = "https://github.com/$script:MihomoRepo/releases/download/v1.19.18/$script:MihomoAssetName"
            }
            $tmp = Join-Path $env:TEMP 'mihomo.zip'
            Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
            if ($expectedSha) {
                $actualSha = (Get-FileHash $tmp -Algorithm SHA256).Hash
                if ($actualSha -ne $expectedSha) { throw "mihomo sha256 不匹配: expected=$expectedSha actual=$actualSha" }
            } else {
                Write-Warn '跳过 sha256 校验（release API 不可用）/ skipped sha256 (API unavailable)'
            }
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $expanded = Join-Path $env:TEMP 'mihomo-extracted'
            if (Test-Path $expanded) { Remove-Item $expanded -Recurse -Force }
            [System.IO.Compression.ZipFile]::ExtractToDirectory($tmp, $expanded)
            $bin = Get-ChildItem -Path $expanded -Recurse -Filter 'mihomo-windows-amd64*.exe' | Select-Object -First 1
            if (-not $bin) { throw "解压后未找到 mihomo-windows-amd64.exe" }
            Move-Item -Force $bin.FullName $exe
            Remove-Item $tmp, $expanded -Recurse -Force
            Write-Ok "mihomo.exe 已下载 / downloaded"
        }

        if ($needWintun) {
            Write-Section 'Downloading wintun.dll'
            $url = "https://www.wintun.net/builds/$script:WintunAssetName"
            $tmp = Join-Path $env:TEMP $script:WintunAssetName
            try {
                Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
                Add-Type -AssemblyName System.IO.Compression.FileSystem
                $expanded = Join-Path $env:TEMP 'wintun-extracted'
                if (Test-Path $expanded) { Remove-Item $expanded -Recurse -Force }
                [System.IO.Compression.ZipFile]::ExtractToDirectory($tmp, $expanded)
                $dll = Get-ChildItem -Path (Join-Path $expanded 'bin\amd64') -Filter 'wintun.dll' -ErrorAction SilentlyContinue | Select-Object -First 1
                if (-not $dll) { throw "wintun zip 内未找到 bin/amd64/wintun.dll" }
                # 简化校验：至少 10KB 且 PE 头 MZ
                $bytes = [System.IO.File]::ReadAllBytes($dll.FullName)
                if ($bytes.Length -lt 10KB) { throw "wintun.dll 太小: $($bytes.Length)B" }
                if ($bytes[0] -ne 0x4D -or $bytes[1] -ne 0x5A) { throw "wintun.dll 非 PE 格式" }
                Move-Item -Force $dll.FullName $wintun
                Remove-Item $tmp, $expanded -Recurse -Force
                Write-Ok "wintun.dll 已安装 / installed"
            } catch {
                Write-Warn "wintun 下载失败（$($_.Exception.Message)），跳过 / skipped"
            }
        }

        if ($needGeo) {
            Write-Section 'Downloading GeoIP / GeoSite'
            # fallback 链：v$ver → 去 patch 版本 → latest
            $geoVer = $ver
            $geoRel = $null
            for ($i = 0; $i -lt 3; $i++) {
                try {
                    $apiHeaders = @{ 'User-Agent' = 'mihomo-proxy-management-installer' }
                    if ($env:GH_TOKEN) { $apiHeaders['Authorization'] = "Bearer $env:GH_TOKEN" }
                    $geoRel = Invoke-RestMethod -Uri "https://api.github.com/repos/MetaCubeX/meta-rules-dat/releases/tags/$geoVer" -Headers $apiHeaders -TimeoutSec 15
                    if ($geoRel) { break }
                } catch {}
                # 退化：去掉 patch 版本号，例如 v1.19.18 → v1.19
                if ($geoVer -match '^(v\d+\.\d+)\.') { $geoVer = $matches[1] }
                else { $geoVer = ''; break }
            }
            if (-not $geoRel) {
                Write-Warn "meta-rules-dat 任何版本都拿不到，尝试 latest / fallback to latest"
                $geoRel = Invoke-RestMethod -Uri 'https://api.github.com/repos/MetaCubeX/meta-rules-dat/releases/latest' -Headers $apiHeaders -TimeoutSec 15
            }
            foreach ($g in @(@{n='geoip.dat';p='GeoIP.dat'}, @{n='geosite.dat';p='GeoSite.dat'})) {
                $asset = $geoRel.assets | Where-Object { $_.name -eq $g.n } | Select-Object -First 1
                if (-not $asset) { Write-Warn "跳过 $($g.n)（asset 不存在）"; continue }
                $expectedSha = ($asset.digest -split ':')[1]
                $tmp = Join-Path $env:TEMP $g.n
                Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmp -UseBasicParsing
                $actualSha = (Get-FileHash $tmp -Algorithm SHA256).Hash
                if ($actualSha -ne $expectedSha) { throw "$($g.n) sha256 不匹配" }
                Move-Item -Force $tmp (Join-Path $InstallDir $g.p)
            }
            Write-Ok "GeoIP.dat / GeoSite.dat 已下载 / downloaded"
        }
    } else {
        Write-Ok "二进制已是最新，跳过下载 / binaries present, skipping download"
    }
}

# ---------- 计划任务 ----------
if (-not $SkipTask) {
    Write-Section 'Registering scheduled task'

    # 移除所有会启动 mihomo 的遗留任务（含旧名 MihomoProxy50 / MihomoProxy）。
    # 多个任务同时拉起 mihomo 是双进程的直接来源之一。
    # Remove every legacy task whose action launches mihomo (incl. old
    # MihomoProxy50 / MihomoProxy) - multiple starters cause duplicate processes.
    foreach ($tk in (Get-ScheduledTask)) {
        $launchesMihomo = $false
        foreach ($a in $tk.Actions) {
            if ("$($a.Execute) $($a.Arguments)" -match '(?i)mihomo') { $launchesMihomo = $true }
        }
        if ($launchesMihomo) {
            Unregister-ScheduledTask -TaskName $tk.TaskName -Confirm:$false
            Write-Warn "已注销旧任务 / unregistered old task: $($tk.TaskName)"
        }
    }

    $action  = New-ScheduledTaskAction -Execute (Join-Path $InstallDir 'mihomo.exe') `
                                       -Argument "-d `"$InstallDir`"" `
                                       -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = 'PT15S'
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask -TaskName $script:TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "$script:ProjectName auto-start" -Force | Out-Null
    Write-Ok "计划任务已注册 / scheduled task registered: $script:TaskName"
}

# ---------- 检查其他自启动项 / detect foreign autostart starters ----------
$foreignStarters = @()
foreach ($folder in @("$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Startup",
                      "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup")) {
    $foreignStarters += Get-ChildItem $folder -Filter '*.vbs' -ErrorAction SilentlyContinue |
        Where-Object { (Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue) -match '(?i)mihomo' } |
        ForEach-Object { $_.FullName }
}
if ($foreignStarters) {
    Write-Warn "以下启动项也会拉起 mihomo，会造成双进程，请删除（计划任务已覆盖该职责）/ these autostart entries also start mihomo and MUST be removed:"
    $foreignStarters | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
}

# ---------- 立即启动一次 ----------
Write-Section 'Starting mihomo'
Get-MihomoProcesses | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Start-ScheduledTask -TaskName $script:TaskName
if (Wait-MihomoReady -TimeoutSec 20) {
    $v = Get-MihomoVersion
    Write-Ok "mihomo 已启动，版本 / started, version: $v"
} else {
    Write-Warn 'mihomo 启动超时，检查 logs\mihomo.log / check logs\mihomo.log'
}

Write-Section 'Done'
Write-Host "管理命令 / management commands:"
Write-Host "  .\scripts\status.ps1      查看状态 / show status"
Write-Host "  .\scripts\restart.ps1     重启 / restart"
Write-Host "  .\scripts\stop.ps1        停止 / stop"
Write-Host "  .\scripts\test.ps1        测速 / connectivity test"
Write-Host "  .\scripts\update-subscription.ps1  强制刷新订阅 / refresh subscription"
Write-Host "  .\scripts\uninstall.ps1   卸载 / uninstall"
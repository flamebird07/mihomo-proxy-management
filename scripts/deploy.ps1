# =====================================================================
# deploy.ps1 - Windows 一键引导入口 / Windows bootstrap entrypoint
#
# 用法 / Usage（管理员 PowerShell / elevated PowerShell）:
#   1. git clone https://github.com/flamebird07/mihomo-proxy-management.git
#   2. cd mihomo-proxy-management
#   3. powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
#
# 默认是 Bootstrap 阶段：准备 Python 3.10+ 与 PyYAML，然后调用
# scripts\deploy\__main__.py（凭据由它交互询问，本脚本不读取凭据）。
# The Install phase is called back by the Python flow:
#   powershell -File scripts\deploy.ps1 -Phase Install -StagingDir <dir> [-NoStart]
#
# 本脚本不会打印或传递任何凭据值；日志里只有路径、任务名、退出码和摘要。
# =====================================================================

[CmdletBinding()]
param(
    [ValidateSet('Bootstrap', 'Install')][string]$Phase = 'Bootstrap',
    [string]$InstallDir = '',          # 默认 Get-InstallDir（通常是 C:\mihomo）
    [string]$StagingDir = '',          # Install 阶段必填
    [switch]$NoStart,                  # 装完不启动（透传 -SkipStart）
    [switch]$DryRun,
    [switch]$SkipSupply,
    [switch]$SkipInstall,
    [switch]$SkipPython,               # 不自动安装 Python
    [int]$MinNodes = 1
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Ensure-Admin

$script:DeployPython = $null

function Get-DeployPython {
    # 返回满足 Python >= 3.10 的 python.exe 绝对路径；都没有则返回 $null。
    # Return an absolute python.exe satisfying Python >= 3.10, or $null.
    if ($script:DeployPython -and (Test-Path -LiteralPath $script:DeployPython -PathType Leaf)) {
        return $script:DeployPython
    }

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($env:MPM_PYTHON) { $candidates.Add($env:MPM_PYTHON) | Out-Null }

    try {
        $cmd = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
        foreach ($c in @($cmd)) { if ($c -and $c.Source) { $candidates.Add($c.Source) | Out-Null } }
    } catch {}

    $patterns = New-Object System.Collections.Generic.List[string]
    if ($env:LOCALAPPDATA) { $patterns.Add((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe')) | Out-Null }
    $patterns.Add('C:\Program Files\Python3*\python.exe') | Out-Null
    if ($env:ProgramFiles) { $patterns.Add((Join-Path $env:ProgramFiles 'Python3*\python.exe')) | Out-Null }
    foreach ($pattern in $patterns) {
        try {
            Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending |
                ForEach-Object { $candidates.Add($_.FullName) | Out-Null }
        } catch {}
    }

    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        # The Microsoft Store alias (%LOCALAPPDATA%\Microsoft\WindowsApps\
        # python.exe) is a 0-byte placeholder that opens the Store; never
        # treat it as the interpreter even though it exists on PATH.
        if ($candidate -match '(?i)[\\/]WindowsApps[\\/]') { continue }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            & $candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { $script:DeployPython = $candidate; return $candidate }
        } catch {}
    }
    return $null
}

function Install-DeployPython {
    Write-Section 'Installing Python'
    $lockPath = Join-Path $PSScriptRoot 'deploy\supply.lock.json'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw "supply lock not found: $lockPath" }
    $lock = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $asset = @($lock.assets | Where-Object { $_.key -eq 'python-windows-amd64' }) | Select-Object -First 1
    if (-not $asset) { throw 'supply lock has no asset: python-windows-amd64' }

    $tmp = Join-Path $env:TEMP $asset.install_as
    $needDownload = $true
    if (Test-Path -LiteralPath $tmp -PathType Leaf) {
        $cachedOk = ((Get-Item -LiteralPath $tmp).Length -eq [long]$asset.size) -and
                    ((Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash -ieq $asset.digest)
        if ($cachedOk) { $needDownload = $false; Write-Ok 'cached installer verified / 已校验缓存安装包' }
    }
    if ($needDownload) {
        Write-Section 'Downloading Python'
        Invoke-WebRequest -Uri $asset.url -OutFile $tmp -UseBasicParsing
    }

    $actualSize = (Get-Item -LiteralPath $tmp).Length
    if ($actualSize -ne [long]$asset.size) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        throw "python installer size mismatch: expected=$($asset.size) actual=$actualSize"
    }
    $actualHash = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash
    if ($actualHash -ine $asset.digest) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        throw "python installer sha256 mismatch: expected=$($asset.digest) actual=$actualHash"
    }
    $sig = Get-AuthenticodeSignature -LiteralPath $tmp
    if ($sig.Status -ne 'Valid') {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        throw "python installer signature not valid: status=$($sig.Status)"
    }
    if (-not $sig.SignerCertificate -or $sig.SignerCertificate.Subject -notlike "*$($asset.authenticode_signer)*") {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        throw "python installer signer mismatch"
    }

    Write-Section 'Running Python installer'
    $p = Start-Process -FilePath $tmp -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1','Include_test=0' -Wait -PassThru
    if (-not $p.HasExited) { $p.WaitForExit() }
    if ($p.ExitCode -ne 0) { throw "python installer exit=$($p.ExitCode)" }

    # 刷新当前会话的 PATH（安装器只改注册表）。
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = ($machinePath, $userPath | Where-Object { $_ }) -join ';'
    Remove-Item -Path Alias:python* -ErrorAction SilentlyContinue
    Write-Ok 'Python installed'
}

function Invoke-Bootstrap {
    # Normalize the install dir to an absolute path whether the caller passed
    # -InstallDir or not, then hand that exact path to the CLI (via MIHOMO_DIR,
    # which the Windows platform adapter reads) and to the Install phase.
    if ([string]::IsNullOrWhiteSpace($InstallDir)) { $InstallDir = Get-InstallDir }
    $InstallDir = [System.IO.Path]::GetFullPath($InstallDir)

    $python = Get-DeployPython
    if (-not $python) {
        if ($SkipPython) {
            throw 'Python 3.10+ not found: install Python or run without -SkipPython'
        }
        Install-DeployPython
        $python = Get-DeployPython
        if (-not $python) { throw 'Python 3.10+ still not found after installation' }
    }
    Write-Ok "python: $python"

    & $python -c "import yaml" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Section 'Installing PyYAML'
        & $python -m pip install --disable-pip-version-check --quiet pyyaml
        if ($LASTEXITCODE -ne 0) { throw 'PyYAML install failed: check network or set PIP_INDEX_URL' }
        & $python -c "import yaml" 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'PyYAML still missing after pip install' }
    }

    $cli = Join-Path $PSScriptRoot 'deploy\__main__.py'
    if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) { throw "deploy CLI not found: $cli" }

    $cliArgs = @('--platform', 'windows')
    if ($DryRun) { $cliArgs += '--dry-run' }
    if ($NoStart) { $cliArgs += '--no-start' }
    if ($SkipSupply) { $cliArgs += '--skip-supply' }
    if ($SkipInstall) { $cliArgs += '--skip-install' }
    if ($MinNodes -ne 1) { $cliArgs += @('--min-nodes', "$MinNodes") }

    # 规范化后的绝对安装目录与选定的解释器都通过子进程环境传给 CLI 与
    # Install 阶段：MIHOMO_DIR 决定 Windows 适配器的所有路径，MPM_PYTHON
    # 防止 Install 阶段悄悄换另一个 Python。
    $env:MIHOMO_DIR = $InstallDir
    $env:MPM_PYTHON = $python
    & $python $cli @cliArgs
    exit $LASTEXITCODE
}

function Invoke-Install {
    if ([string]::IsNullOrWhiteSpace($StagingDir) -or -not (Test-Path -LiteralPath $StagingDir -PathType Container)) {
        throw "Install phase requires an existing -StagingDir: '$StagingDir'"
    }

    if ([string]::IsNullOrWhiteSpace($InstallDir)) {
        # Honour the normalized path the bootstrap pinned via MIHOMO_DIR even
        # before the directory exists; Get-InstallDir would otherwise fall
        # back to C:\mihomo and lose a custom install dir.
        if ($env:MIHOMO_DIR) { $InstallDir = $env:MIHOMO_DIR } else { $InstallDir = Get-InstallDir }
    }
    $InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
    if (-not (Test-Path -LiteralPath $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }
    $InstallDir = (Resolve-Path -LiteralPath $InstallDir).Path
    New-Item -ItemType Directory -Path (Join-Path $InstallDir 'providers') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $InstallDir 'logs') -Force | Out-Null
    $env:MIHOMO_DIR = $InstallDir

    # ---- 先完整校验所有待安装文件，再考虑停止运行实例 ----
    $required = @('mihomo.exe', 'GeoIP.dat', 'GeoSite.dat', 'geoip.metadb')
    foreach ($name in $required) {
        $src = Join-Path $StagingDir $name
        if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { throw "staged file missing: $src" }
    }
    $wintunSrc = Join-Path $StagingDir 'wintun.dll'
    $haveWintun = Test-Path -LiteralPath $wintunSrc -PathType Leaf
    if (-not $haveWintun) { Write-Warn 'wintun.dll 未暂存，跳过 / not staged, skipping' }

    $copyList = @()
    foreach ($name in $required) {
        $copyList += @{ Name = $name; Source = (Join-Path $StagingDir $name) }
    }
    if ($haveWintun) { $copyList += @{ Name = 'wintun.dll'; Source = $wintunSrc } }

    # 同一文件内容不变化就不需要停止旧实例。
    $changed = @()
    foreach ($item in $copyList) {
        $dest = Join-Path $InstallDir $item.Name
        $diff = $true
        if (Test-Path -LiteralPath $dest -PathType Leaf) {
            $srcHash = (Get-FileHash -LiteralPath $item.Source -Algorithm SHA256).Hash
            $dstHash = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash
            if ($srcHash -ieq $dstHash) { $diff = $false }
        }
        if ($diff) { $changed += $item }
    }

    if ($changed.Count -gt 0 -and @(Get-MihomoProcesses).Count -gt 0) {
        Stop-Mihomo
    } elseif ($changed.Count -eq 0) {
        Write-Ok 'install files unchanged / 已是最新，保留运行实例'
    }

    foreach ($item in $changed) {
        Copy-Item -LiteralPath $item.Source -Destination (Join-Path $InstallDir $item.Name) -Force
        Write-Ok "installed $($item.Name)"
    }

    if ($haveWintun) {
        $dll = Join-Path $InstallDir 'wintun.dll'
        if (Test-Path -LiteralPath $dll) {
            $sig = Get-AuthenticodeSignature -LiteralPath $dll
            if ($sig.Status -ne 'Valid') {
                Write-Warn "wintun.dll 签名无效（sha256 已校验）/ signature status=$($sig.Status)"
            }
        }
    }

    # ---- 固定解释器：优先 Bootstrap 传下来的绝对路径 ----
    $python = Get-DeployPython
    if (-not $python) { throw 'Python 3.10+ not found for Install phase' }
    $env:MPM_PYTHON = $python

    Write-Section 'Running installer'
    $skipStart = [bool]$NoStart
    if ($changed.Count -eq 0 -and @(Get-MihomoProcesses).Count -gt 0) {
        # Nothing changed and mihomo is running: keep the live instance.
        $skipStart = $true
    }
    $installArgs = @{ InstallDir = $InstallDir; SkipDownload = $true }
    if ($skipStart) { $installArgs['SkipStart'] = $true }
    & (Join-Path $PSScriptRoot 'install.ps1') @installArgs

    Write-Section 'Registering Feishu sync task'
    & (Join-Path $PSScriptRoot 'sync-feishu-bitable.ps1') -RegisterTask -Bootstrap -InstallDir $InstallDir -Interpreter $python
    if ($LASTEXITCODE -ne 0) { throw "Feishu sync task registration failed exit=$LASTEXITCODE" }
    Write-Ok 'Feishu sync task registered'

    Write-Section 'Registering node pipeline task'
    & (Join-Path $PSScriptRoot 'install-node-pipeline-task.ps1') -InstallDir $InstallDir -Interpreter $python
    if ($LASTEXITCODE -ne 0) { throw "node pipeline task registration failed exit=$LASTEXITCODE" }
    Write-Ok 'node pipeline task registered'

    Write-Section 'Done'
    Write-Host "引导安装完成 / bootstrap install complete: $InstallDir"
    Write-Host "  config:        $(Join-Path $InstallDir 'config.yaml')"
    Write-Host "  service task:  $script:TaskName"
    Write-Host "  sync task:     MihomoFeishuBitableSync"
    Write-Host "  pipeline task: MihomoNodePipeline"
    Write-Host "  .\scripts\status.ps1      查看状态 / show status"
    Write-Host "  .\scripts\restart.ps1     重启 / restart"
    Write-Host "  .\scripts\uninstall.ps1   卸载 / uninstall"
    exit 0
}

switch ($Phase) {
    'Bootstrap' { Invoke-Bootstrap }
    'Install'   { Invoke-Install }
}

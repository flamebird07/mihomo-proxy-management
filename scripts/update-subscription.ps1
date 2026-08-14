# =====================================================================
# update-subscription.ps1 - 强制立即拉取订阅
# Force-refresh the subscription right now.
# =====================================================================

[CmdletBinding()]
param(
    [switch]$Resubscribe,    # 强制重新下载（忽略本地缓存）
    [switch]$ShowYaml        # 拉取后打印节点概要
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$dir = Get-InstallDir
$envPath = Get-EnvPath
if (-not (Test-Path $envPath)) { throw "缺少 subscription.env，请先运行 install.ps1" }

$env = Read-EnvFile $envPath
$url = $env['SUBSCRIPTION_URL']
if (-not $url) { throw "subscription.env 中 SUBSCRIPTION_URL 未设置" }

Write-Section "Fetching subscription"
# 只显示 host，不泄露完整 URL（含 token）
try {
    $hostOnly = ([uri]$url).Host
    Write-Host "Provider: $hostOnly"
} catch {
    Write-Host 'Provider: (无法解析 URL host)'
}

# mihomo 用 ua: clash-verge/2.0 之类的会被机场识别为 Clash 客户端，
# 但 mihomo 默认 user-agent 是 clash-meta，多数机场会自动返回 YAML。
# 这里覆写 ua 提高兼容性。
$headers = @{ 'User-Agent' = 'clash.meta' }
if ($env['SUBSCRIPTION_USER_AGENT']) {
    $headers['User-Agent'] = $env['SUBSCRIPTION_USER_AGENT']
}

# mihomo 内置定时拉取会写入 ./providers/subscription.yaml；
# 我们这里只是触发一次刷新（通过 controller API）
Ensure-Admin
if (-not (Get-MihomoProcess)) {
    Write-Warn 'mihomo 未运行，先启动 / starting first'
    & (Join-Path $PSScriptRoot 'start.ps1')
}

# 调用 controller 强制刷新（v1.19+ 支持）
try {
    if ($Resubscribe) {
        # 先删缓存，让 mihomo 重启后按当前 SUBSCRIPTION_URL 重新拉取
        # mihomo 在运行时不主动重拉 url，只在启动时拉
        $cache = Join-Path $dir 'providers\subscription.yaml'
        if (Test-Path $cache) {
            Remove-Item $cache -Force
            Write-Warn '已删除本地缓存 / provider cache deleted'
        }
        Write-Warn '强重拉需要重启 mihomo / -Resubscribe forces restart'
        & (Join-Path $PSScriptRoot 'restart.ps1')
        return
    } else {
        # 软刷新：删缓存 + 重载 provider，触发新一轮 health-check
        $cache = Join-Path $dir 'providers\subscription.yaml'
        if (Test-Path $cache) {
            Remove-Item $cache -Force
            Write-Warn '已删除本地缓存 / cache deleted'
        }
        Invoke-WebRequest -Uri 'http://127.0.0.1:9090/providers/proxies/subscription' `
            -Method PUT -UseBasicParsing -TimeoutSec 10 | Out-Null
        Write-Ok '已触发软刷新（仅重载 provider，不重拉 url）/ soft refresh triggered'
    }
} catch {
    Write-Warn "controller 调用失败，请手动重启 mihomo / controller call failed: $($_.Exception.Message)"
    & (Join-Path $PSScriptRoot 'restart.ps1')
}

Start-Sleep -Seconds 5

if ($ShowYaml) {
    Write-Section 'Current nodes'
    try {
        $d = Invoke-RestMethod 'http://127.0.0.1:9090/providers/proxies' -TimeoutSec 5
        $d.providers.subscription.proxies | Select-Object -First 20 `
            @{n='Name';e={$_.name}}, type,
            @{n='Alive';e={$_.alive}},
            @{n='Delay(ms)';e={ if ($_.history) { $_.history[-1].delay } else { 0 } }} |
            Format-Table -AutoSize
    } catch {
        Write-Warn "无法读取 /providers/proxies : $($_.Exception.Message)"
    }
}
# =====================================================================
# test.ps1 - 连通性测试（直连 + 走代理）
# Connectivity test (direct + via proxy).
# =====================================================================

[CmdletBinding()] param()
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

function Test-Url {
    param([string]$Label, [string]$Url, [string]$Proxy = '')
    $args = @('-s', '-o', 'NUL', '-w', "$Label : %{http_code} (%{time_total}s)`n", '--max-time', '15')
    if ($Proxy) { $args += @('-x', $Proxy) }
    $args += $Url
    & curl.exe @args
}

Write-Section 'Direct (TUN)'
Test-Url 'google' 'https://www.google.com'
Test-Url 'github' 'https://github.com'
Test-Url 'openai' 'https://api.openai.com/v1/models'
Test-Url 'baidu' 'https://www.baidu.com'

Write-Section 'Via mixed-port 7890 (no TUN)'
Test-Url 'google' 'https://www.google.com'        'http://127.0.0.1:7890'
Test-Url 'github' 'https://github.com'            'http://127.0.0.1:7890'
Test-Url 'openai' 'https://api.openai.com/v1/models' 'http://127.0.0.1:7890'
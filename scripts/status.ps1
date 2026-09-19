# =====================================================================
# status.ps1 - 查看 mihomo 状态 / 节点 / 健康度
# Show mihomo status: process, ports, TUN, node count, selected group.
# =====================================================================

[CmdletBinding()] param()
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Write-Section 'Process'
$procs = @(Get-MihomoProcesses)
if ($procs.Count -gt 0) {
    foreach ($p in $procs) {
        Write-Ok "running PID=$($p.Id) name=$($p.ProcessName) started=$($p.StartTime) mem=$([math]::Round($p.WorkingSet64/1MB,1))MB"
    }
    if ($procs.Count -gt 1) {
        Write-Err "检测到 $($procs.Count) 个 mihomo 进程（应为 1 个）！运行 start.ps1 可自动清理去重 / DUPLICATE instances, run start.ps1 to self-heal"
    }
    $v = Get-MihomoVersion
    if ($v) { Write-Host "  version: $v" }
} else {
    Write-Warn 'mihomo is NOT running'
}

Write-Section 'Scheduled task'
$t = Get-MihomoTask
if ($t) {
    Write-Ok "Task: $($t.TaskName)  State=$($t.State)  UserId=$($t.Principal.UserId)"
} else {
    Write-Warn "Task '$script:TaskName' not found"
}

Write-Section 'Listening ports (7890/9090/1053)'
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 7890,9090,1053 } |
    Select-Object LocalAddress, LocalPort |
    Format-Table -AutoSize

Write-Section 'TUN adapter'
Get-NetAdapter -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceDescription -match 'TUN|Meta|wintun' } |
    Select-Object Name, InterfaceDescription, Status |
    Format-Table -AutoSize

if ($procs.Count -gt 0) {
    Write-Section 'Proxies (subscription provider)'
    try {
        $d = Invoke-RestMethod 'http://127.0.0.1:9090/providers/proxies' -TimeoutSec 5
        $nodes = $d.providers.subscription.proxies
        Write-Host ("Total nodes: {0}" -f $nodes.Count)
        $nodes | Select-Object -First 10 `
            @{n='Name';e={$_.name}},
            @{n='Type';e={$_.type}},
            @{n='Alive';e={$_.alive}},
            @{n='Delay(ms)';e={ if ($_.history) { $_.history[-1].delay } else { 0 } }} |
            Format-Table -AutoSize
    } catch {
        Write-Warn "无法读取 /providers/proxies : $($_.Exception.Message)"
    }

    Write-Section 'Selected proxy groups'
    try {
        $groups = Invoke-RestMethod 'http://127.0.0.1:9090/proxies' -TimeoutSec 5
        $groups.proxies |
            Where-Object { $_.type -in 'Selector','URLTest' } |
            Select-Object name, type,
                @{n='Selected';e={ if ($_.now) { $_.now } else { '-' }} } |
            Format-Table -AutoSize
    } catch {
        Write-Warn "无法读取 /proxies : $($_.Exception.Message)"
    }
}
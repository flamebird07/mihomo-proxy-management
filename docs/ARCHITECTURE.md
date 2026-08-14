# Architecture & Troubleshooting

## 设计目标 / Goals

- **零节点维护**：用户只关心一个 `SUBSCRIPTION_URL`，节点列表由 mihomo 按 `proxy-providers` 自动拉取
- **零二进制提交**：mihomo / wintun / Geo 数据全部运行时下载（带 fallback 版本）
- **零意外提交凭证**：所有用户本地配置在 `.gitignore` 内，绝不会进 git
- **可复现**：所有脚本幂等，重跑只会更新变化的部分

## 数据流 / Data flow

```
subscription.env (user)
       │
       ▼ Render-Config (PowerShell)
config.yaml.template ──► config.yaml (runtime)
       │
       ▼ mihomo.exe -d C:\mihomo
       │
       ▼ http GET SUBSCRIPTION_URL
       │
providers/subscription.yaml (缓存)
       │
       ▼ health-check
proxy-groups: AUTO-FOREIGN / US-FAST / VIETNAM / GLOBAL
       │
       ▼ rules
TUN → 全局流量
```

## 计划任务 / Scheduled task

- 任务名: `MihomoProxy`
- 触发: `AtStartup` + `Delay = PT15S`
- 主体: `SYSTEM / ServiceAccount / Highest`
- 重启策略: 失败 3 次 / 间隔 1 分钟（mihomo 崩溃自动恢复）
- 电源: 允许电池下启动，电池耗电不停止

兼容性：脚本同时清理旧的 `MihomoProxy50` / `MihomoProxy` 任务名。

## 故障排查 / Troubleshooting

### 1. mihomo 起不来

```powershell
# 看日志
Get-Content C:\mihomo\logs\mihomo.log -Tail 50

# 直接前台跑
C:\mihomo\mihomo.exe -d C:\mihomo
```

常见 YAML 错误：

| 错误 | 原因 |
|---|---|
| `proxy-providers[subscription] url is empty` | `subscription.env` 里 URL 没替换 / 仍是 `example.com` |
| `decode yaml: invalid character` | 机场返回的是 base64 不是 yaml，需要在 URL 加 `&flag=clash` |
| `TUN stack gvisor not supported` | 当前 mihomo 太老，升级：删 `C:\mihomo\mihomo.exe` 后重跑 `install.ps1 -Force` |

### 2. 节点连不上 / 延迟高

```powershell
# 查看哪个被选中
.\scripts\status.ps1
# 强刷订阅
.\scripts\update-subscription.ps1 -Resubscribe -ShowYaml
# 临时切到 DIRECT 看是不是机场问题
# Dashboard: http://127.0.0.1:9090  →  选 GLOBAL → DIRECT
```

### 3. TUN 模式没生效

- 确认 `wintun.dll` 在 `C:\mihomo\`
- 看系统适配器列表有没有 `Meta Tunnel` / `wintun`：
  ```powershell
  Get-NetAdapter | Where-Object { $_.InterfaceDescription -match 'TUN|Meta|wintun' }
  ```
- 如果残留旧 TUN 卡死：
  ```powershell
  Get-NetAdapter | Where-Object { $_.InterfaceDescription -match 'wmsxwd' } |
      Disable-NetAdapter -Confirm:$false
  ```

### 4. 卸载不干净

```powershell
.\scripts\uninstall.ps1 -Purge
```

会一并删除 `C:\mihomo\` 整目录（用户配置 / 缓存 / 二进制）。

## 安全 / Security

- **永远不要把 `subscription.env` 提交进 git** — 它包含你的订阅凭证
- **永远不要把 `providers/subscription.yaml` 提交进 git** — 它是订阅解码后的明文节点
- Dashboard 默认监听 `127.0.0.1:9090`，仅本机访问。若需远程 Dashboard，
  在 `subscription.env` 设 `SECRET=强密码`，并把 `external-controller`
  改为 `0.0.0.0:9090`（自行修改模板）。

## 版本兼容 / Compatibility matrix

| 组件 | 要求 |
|---|---|
| Windows | 10 1809+ / 11 / Server 2019+ |
| PowerShell | 5.1 (内置) |
| mihomo | 1.18.0+（本项目用 1.19.x 验证） |
| wintun | 0.14+ |
| .NET | 4.5+ (PowerShell 5.1 依赖) |

## 与上游同步 / Upstream sync

```powershell
# 检查新版 mihomo
Invoke-RestMethod https://api.github.com/repos/MetaCubeX/mihomo/releases/latest |
    Select-Object tag_name, published_at

# 升级二进制（不重生成配置）
.\scripts\install.ps1 -Force -SkipTask
```
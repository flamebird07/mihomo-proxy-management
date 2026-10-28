# Architecture & Troubleshooting

> Windows 架构如下；Linux 系统部署的架构、支持矩阵、DEGRADED 语义、供应链与
> 生命周期规则见 [`LINUX.md`](LINUX.md)。
> Everything below documents the Windows flow; the Linux system profile lives in
> [`LINUX.md`](LINUX.md).

## 设计目标 / Goals

- **节点维护**：旧安装入口由 mihomo 按单一 `SUBSCRIPTION_URL` 自动拉取；三源入口只生成候选，不自动上线
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

## 三源候选流（与上方旧运行流分离）

本地 env 文件或进程环境 `SUBSCRIPTION_URL_1/2/3` → 各自 UA 抓取 →
逐源 YAML / sing-box JSON / base64-URI 识别 → 转换与分类统计 →
语义去重、重名处理 → 最小数量门槛 → 同目录临时文件 → `os.replace` 原子替换指定候选文件。

`update-subscription.ps1` 仅把显式 `-EnvFile`、`-Output`、可选 `-MinNodes` 传给原有 Python 转换器；
不加载有运行副作用的共享脚本，不调用 controller，不删除缓存，不改变进程或计划任务。
`--sources-env` 可直接从进程环境取三个来源，避免读取整个本地配置；本阶段只测试合成 fixture。

- 每源 output 为转换后数量；全局 merged 是去重后数量，二者不可混用。
- 语义键是除 name 外的完整规范化字段（含认证、TLS 和传输），只在内存比较，不记录键或摘要。
  原节点不原地修改；同名不同配置按源顺序加数字后缀，预留所有原名称以避免二次碰撞。
- 最小门槛默认 1，仅防止空输出；建议依据已知规模提高。CLI 覆盖 env，env 只对三源入口生效。
  门槛检查新候选池，不能用 `--merge` 中的旧节点补足。不同格式的显式/省略默认值不会强行合并。
- 部分来源失败且候选达标：写出并返回 5，日志明确告警；全成功为 0；全部失败或不足为 1。
  单源网络失败为 4，解析/写出失败为 3，设置错误为 2。跳过节点单独统计，0 不等于零跳过或联网可用。
- 整体转换失败（单源格式损坏、全部来源失败）、门槛不足、合并目标损坏或原子替换失败时，
  不覆盖原文件；部分源损坏但剩余候选达标按上述退出码 5 处理。输出目录必须事先存在。
- 日志仅用来源序号、节点序号、白名单类型与计数；不打印 URL/路径、原名称、端点、UUID 或认证字段。
  候选 YAML 本身仍含明文凭据，需本地访问控制，禁止提交或上传。

## 动态健康池与美国路由

`health-check-provider.py` 在独立端口启动临时 Mihomo，TUN 与 DNS 均关闭，对全部候选并发执行多个
目标的 controller delay 检测。最终 provider 是各目标通过集合的交集，数量不固定。写出前还会用
`--required-name-regex` 检查美国节点数量；低于 `--minimum-matching` 时非零退出且不覆盖上一版文件。

生产 `US-FAST` 通过 file provider 的 `use` 加美国名称过滤器读取健康池，所以订阅变化后可自动扩缩，
无需把两条或任意固定数量节点写进 `config.yaml`。ChatGPT 与 `muse.ai` 规则都指向 `US-FAST`；
上线前仍须用真实 HTTPS 请求验证应用层 TLS，而不能只依赖 controller delay。

### sing-box 映射范围

| 输入 | mihomo 输出 |
|---|---|
| type/tag/server/server_port | type/name/server/port；shadowsocks 改为 ss |
| shadowsocks method/password | cipher/password |
| trojan password | password |
| vless uuid/flow/packet_encoding | uuid/flow/packet-encoding |
| tuic uuid/password/congestion_control/udp_relay_mode/zero_rtt_handshake/heartbeat | uuid/password/congestion-controller/udp-relay-mode/reduce-rtt/heartbeat-interval（毫秒） |
| hysteria2 password/up_mbps/down_mbps/obfs | password/up/down/obfs/obfs-password（salamander） |
| anytls password/idle_session_check_interval/idle_session_timeout/min_idle_session | password/idle-session-check-interval/idle-session-timeout/min-idle-session（时长转整数秒） |
| tls enabled/server_name/insecure/alpn | vless 的 tls/servername；其他协议 sni；skip-cert-verify/alpn |
| tls utls/reality | client-fingerprint（vless/trojan/anytls）；reality-opts（vless） |
| transport ws | network: ws，ws-opts 含 path/headers/max-early-data/early-data-header-name |
| transport grpc | network: grpc，grpc-opts.grpc-service-name |
| multiplex（ss/vless/trojan） | smux：enabled/protocol/max-connections/min-streams/max-streams/padding；未启用时不输出 |
| bind_interface/routing_mark/tcp_fast_open/tcp_multi_path | interface-name/routing-mark/tfo/mptcp |

非节点 selector/urltest/direct/block/dns 计数跳过；未知类型告警并跳过。
未映射选项（包括 detour、外部证书、未知传输/扩展）跳过整节点并计入 skipped_unsupported，
避免静默改变连接语义；结构错误计入 skipped_malformed。YAML 仍原字段透传，未知 type 默认保留，
仅 `--skip-unknown` 明确丢弃；目标核心是否支持这些字段需要下一步校验。

### 候选验收与后续健康检查（需独立授权）

1. 先用合成 fixture 验证转换；真实订阅仅从本机环境供给，不记录命令行 URL 或响应原文。
2. 对目标 mihomo 版本建立隔离临时配置：仅导入候选 proxies、关闭 TUN/DNS/controller 和健康检查，
   使用不依赖 Geo 下载的规则；在独立目录用核心 `-t` 离线检查，不启动服务、不覆盖生产 config。
   AnyTLS 需要原生支持该类型的核心；未知 YAML type 或未支持字段可能让核心拒绝候选。
3. 核心接受后，再单独授权真实延迟、连接成功率和出口检查，只报告脱敏聚合结果。
   只有完成真实检查的节点才能称为“已联网可用”，未经探测的数量是候选数。
4. 如要上线三源池，另行批准切换到本地 file provider 并处理旧 HTTP provider 的自动刷新，
   然后才考虑明确授权的 reload；转换器本身不连接 controller，也不代做这些动作。

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
# 仅生成候选，需先授权真实抓取；不重载服务
.\scripts\update-subscription.ps1 -EnvFile .\subscription.env -Output .\providers\candidate.yaml
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

- **永远不要把 `subscription.env` 提交进 git** — 它包含你的订阅凭证（`.gitignore`
  现为递归规则：任意目录下的 `**/subscription.env` / `**/*.env`（`*.env.example` 除外）
  / `**/config.yaml` / `providers/*.yaml` 全部忽略）
- **永远不要把 `providers/subscription.yaml` 提交进 git** — 它是订阅解码后的明文节点
- Windows controller API（127.0.0.1:9090）调用现统一经由 `Invoke-MihomoApi` 携带
  `Bearer $SECRET`；`SECRET` 为空时直接抛错，不存在未认证回退（Linux 侧同理，见 LINUX.md）
- `convert_sub.py` 不再原样回显订阅 URL；节点 server:port 默认隐藏，
  `--show-endpoints` 也只显示脱敏后的 `***.example.invalid`
- **已知未修复（本期范围外，明示）**：Windows `install.ps1` 的下载仍是 latest +
  无摘要校验 + 失败回退，不具备 Linux 侧的供应链验证；不要将 Windows 安装视为已验证
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

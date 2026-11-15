# 西瓜动态入口 IP 池

`scripts/ippool-collector.py` 绕过 fake-ip，通过 DoH 分别累计每个西瓜入口域名的真实 A 记录，再经本机 Mihomo HTTP CONNECT 和该域名原节点的 SNI 做 TLS 探测。状态、健康判断和发布映射都按原始 `server host` 隔离，绝不把 A 域名发现的 IP 复制给 B 域名。连续成功 2 轮才入池，连续失败 3 轮才出池；每个原节点最多发布 3 个稳定 IP。同一 host 有多个节点时，使用端口号最小、名称排序稳定的节点作为该 host 的代表探针。

## 安全边界

- 状态文件只保存 IP、时间、成功/失败次数和延迟，不保存密码、UUID、订阅地址或完整节点。
- 状态 schema 为 v2（`hosts -> host -> ips`）。旧 v1 全局 IP 状态无法证明 host 归属，collector 会拒绝读取；请先留档，再删除旧状态，让 v2 重新积累两轮，不能直接迁移。
- 第一次运行通常没有成熟 IP，必须至少连续运行两轮。没有成熟 IP 时保留旧 provider，不写空文件。
- 本机出口到全部西瓜入口都被限制，或西瓜服务整体不可用时，脚本不能恢复服务；连续 3 轮零健康 IP 会输出 `alert: true`。
- 生产链路固定两阶段：collector 只更新独立 stage1；随后仓库现有 `health-check-provider.py` 启动隔离 Mihomo，对至少两个显式目标取健康交集。健康交集至少有 1 个名称含“西瓜”的节点才原子发布显式 FinalProvider。
- stage2 失败或健康数为 0 时保持旧 FinalProvider 不变并非 0 退出。脚本不会自动改生产配置、热加载控制器或重启 Mihomo。

## Dry-run

```powershell
.\scripts\run-xigua-ippool.ps1 `
  -SourceProvider 'C:\private\extracted-provider.yaml' `
  -RuntimeRoot 'C:\private\xigua-ippool' `
  -Mode dry-run
```

产物为 `ippool.json` 和 `xigua-ippool.stage1.yaml`。日志只输出计数和状态。若要在 dry-run 中额外验证 stage2，可加 `-RunE2E -Core <mihomo.exe> -Target <目标1>,<目标2>`；结果只写 `xigua-ippool.e2e.staging.yaml`，不会碰生产文件。

## 生产发布

生产模式必须显式指定最终 provider、Mihomo core 和至少两个应用目标。wrapper 先更新 stage1，再把 stage1 作为 `--candidate` 交给现有隔离健康检查；后者以 `--minimum-matching 1 --required-name-regex 西瓜` 发布最终文件：

```powershell
.\scripts\run-xigua-ippool.ps1 -SourceProvider 'C:\private\source.yaml' `
  -RuntimeRoot 'C:\private\xigua-ippool' -Mode production `
  -FinalProvider 'C:\mihomo\providers\xigua-ippool.yaml' `
  -Core 'C:\mihomo\mihomo.exe' `
  -Target 'https://www.gstatic.com/generate_204','https://chatgpt.com/cdn-cgi/trace' `
  -MixedPort 17890 -ControllerPort 19090 -TimeoutMs 10000 -Workers 4
```

Production 缺少 `-FinalProvider` 会在运行 collector 前拒绝，禁止默认路径误写。两个目标必须都通过才进入健康交集；任一 stage 失败不会覆盖旧最终文件。实际生产配置应把该 file provider 放入西瓜专用 fallback/url-test 组，并保留非西瓜应急线路，避免全池不可达时形成死循环。

## 退出码矩阵

wrapper 逐阶段保存退出码，并按“池失败优先、其次飞书、最后日志”聚合；任一阶段非 0 都不会被后续成功覆盖。

| 场景 | pool（collector / stage2） | 飞书 | 日志 | 最终退出码 |
| --- | --- | --- | --- | --- |
| 全部成功 | 0 | 0 | 可写 | 0 |
| 池未成熟（无健康 IP） | 2 | 0 | 可写 | 2 |
| 池未成熟且飞书失败 | 2 | 非 0（示例 6） | 可写 | 2（池优先） |
| collector 失败 | 1 | 0 | 可写 | 1 |
| stage2 失败 | 非 0（示例 7） | 0 | 可写 | 该非 0（示例 7） |
| stage2 失败且飞书失败 | 非 0（示例 7） | 非 0（示例 6） | 可写 | 池的退出码（池优先） |
| 池成功但飞书失败 | 0 | 非 0（示例 6） | 可写 | 飞书退出码（仍非 0） |
| 池与飞书成功但日志不可写 | 0 | 0 | 失败 | 8 |
| 池失败且日志不可写 | 非 0 | 任意 | 失败 | 池的退出码（仍非 0） |

说明：collector 的 `2` 表示本轮没有成熟 IP，`3` 表示锁忙，`4` 表示生产校验失败，`1` 表示其他错误；stage2 与飞书的非 0 码以各自进程实际返回值为准。日志不可写时 stderr 输出 `XIGUA_LOG_FAILED`；只有池与飞书都为 0 时才会把最终退出码提升为 `8`。

## 每 5 分钟运行

先用 `-WhatIf` 检查参数，确认后再注册；安装脚本本身不会由部署流程自动执行：

```powershell
.\scripts\install-xigua-ippool-task.ps1 -SourceProvider 'C:\private\source.yaml' `
  -RuntimeRoot 'C:\private\xigua-ippool' -Mode production `
  -FinalProvider 'C:\mihomo\providers\xigua-ippool.yaml' `
  -Core 'C:\mihomo\mihomo.exe' `
  -Target 'https://www.gstatic.com/generate_204','https://chatgpt.com/cdn-cgi/trace' -WhatIf
```

安装器不会把长参数直接塞进 `schtasks /TR`。它会在 RuntimeRoot 原子写入 UTF-8 的 `scheduled-run.ps1`（仅含经过 PowerShell 字面量转义的非敏感参数），任务动作只引用这个短启动器。更新已有启动器时使用同目录唯一 backup 调用 `File.Replace`，成功后清理 backup；绝不先删除旧启动器，异常也会清理临时文件。注册前硬检查 `/TR` 动作不超过 Windows 的 261 字符限制；若 RuntimeRoot 仍使动作过长，会明确拒绝并提示换短路径。注册时显式传入 `/RU <账户>` 与 `/RL <LIMITED|HIGHEST>`，不再隐式继承账户或级别。`-WhatIf` 不创建 RuntimeRoot、不写启动器、也不调用 `schtasks`。

删除任务：`schtasks /Delete /TN MihomoXiguaIpPool /F`。回滚时停用/删除任务并恢复原 provider；脚本不会修改原 provider。

## 飞书近实时表

每轮 wrapper 结束前会调用 `scripts/sync-xigua-ippool-feishu.py`，维护独立子表 `西瓜IP池`。它不写现有“订阅源状态 / 可测速节点 / GPT可用地区组”三张表。Production 在 stage2 之后同步，因此“最终可用”对应本轮最终 provider；collector 尚未成熟或 stage2 失败时仍会尝试同步安全可读的 state。退出码按“池失败优先、其次飞书、最后日志”聚合：池成功但飞书失败时最终仍为非 0。stdout 会分别打印 `POOL_RESULT`、`FEISHU_RESULT` 与 `ROUND_RESULT`。

飞书凭据从 `--env-file`（默认 `C:\Users\Administrator\.hermes\.env`）读取：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_BITABLE_APP_TOKEN`（多维表 app token）。token 解析优先级为命令行 `--app-token` > 进程环境变量 `FEISHU_BITABLE_APP_TOKEN` > env 文件同名键；缺失时只报键名与来源并以非 0 退出，任何日志都不打印 token 值。真实值只填在本机 env 文件，不进仓库。

表中每个 `(原入口域名, IP)` 一行，包含稳定唯一键、发现/健康/防抖计数、延迟、时间和最终节点名。状态缺失、非 schema v2、结构损坏或 0 行时拒绝替换，绝不清空旧表。同步 cursor 位于 RuntimeRoot 的 `feishu-sync-state.json`，变化键覆盖 state SHA-256、最终 provider SHA-256 和 table id；无变化时跳过整表替换。写入后必须回读记录总数和至少一个稳定唯一键才更新 cursor。

同步使用独立锁文件 `feishu-sync.lock`（仅保存 pid/token 元数据）和独立命名 mutex `Local\mihomo-xigua-ippool-feishu-sync`；主同步使用 `Local\mihomo-feishu-bitable-sync`，两者不同，因此西瓜表同步不会与主同步的 writer mutex 串行。wrapper 另写全链日志 `xigua-ippool.log`（默认在 RuntimeRoot，1 MiB 轮转、保留 3 份、键值与 URL 脱敏）；飞书阶段日志仍为 `C:\mihomo\logs\xigua-ippool-feishu.log`。日志、cursor 和表记录都不包含密码、token、SNI 或订阅 URL。日志不可写时 stderr 输出 `XIGUA_LOG_FAILED`，且若池与飞书均成功则最终退出码为 `8`。现有计划任务 launcher 调用仓库 wrapper，因此不需要增加 launcher 参数或重注册任务；部署新脚本和 wrapper 后，下一轮自然生效。

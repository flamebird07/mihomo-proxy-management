# macOS 启动问题报告

日期：2026-11-23 · 主机：macOS · 部署：TUN 模式（mihomo v1.19.34）

## 1. 结论

- **项目现在正常运行**：mihomo + runner 在跑，DNS 走 mihomo fake-ip，外网访问正常。
- 之前“没启动”不是二进制或 launchd 坏了，而是**启动事务的健康闸门失败后按设计回滚到 Nextin**；而 Nextin 的隧道当时也是坏的，所以表现为“整机网络不正常”。
- 自动启动链路本身可用，但存在过 **3 个逻辑缺陷**（组存活竞态、健康检查 provider 选错、status 误报），已一并修复；boot 入口已用 `mpm_macos.boot` 模拟验证通过（幂等，exit 0）。
- 测试：`macos/tests` **446 passed**。
- 代码改动**尚未提交 git**。

## 2. 故障时间线（本次开机 12:13）

| 时间 | 事件 | 证据 |
|---|---|---|
| 12:13:05 | boot-switch 由 RunAtLoad 启动 | launchd 日志 |
| 12:13:16 | 开始断开 Nextin，等待 4s 稳定窗口 | boot-switch.out.log |
| 12:13:21 | mihomo 启动，订阅 provider 强制刷新 | core.out.log |
| 12:13:22 | mihomo 已在转发流量 | core.out.log |
| 12:13:23 | 健康闸门失败 → stop + disable core → 回滚 Nextin | boot-switch.err.log |
| 12:13:23 | `switch failed: tun core is not healthy: group AUTO-FOREIGN has no live node` | boot-switch.err.log |
| 之后 | Nextin 处于 Connected，但其隧道不可用，整机 DNS/出网异常 | `scutil --nc list`、curl 全失败 |

补充：同一天更早的两次开机被拒绝，原因是 Nextin 的 Always-On VPN（On Demand）状态不可读，属于设计内的 fail-closed；本次 On Demand 为 FALSE，所以进入了真正的切换流程。

## 3. 根因

启动闸门要求 `subscription` provider 至少 1 个存活节点，且 `AUTO-FOREIGN`、`GPT` 两个组都存活。实测（临时 mihomo，关闭 TUN/DNS、独立端口，未影响生产）：

| 节点源 | 节点数 | alive | 更新时间 |
|---|---|---|---|
| `subscription` | 40 | **0 / 40** | 11-19 21:56（长期未更新） |
| `source1-local`（nextin.yaml） | 35 | **0 / 35** | 11-19 22:04 |
| `node-valid` | 56 | 正常（US-FAST / GPT alive） | 当天刷新 |

`AUTO-FOREIGN` 当时取 `subscription + source1-local`，75 个节点全不可用，组必然不存活 → 回滚。此后 Nextin 又是坏的，于是“项目没启动”和“网络不正常”同时出现。

另外两个独立问题：

1. macOS `mDNSResponder` 把 DNS 查询绑到 Wi-Fi 服务上，直连 114/8.8.8.8，**绕过 TUN**，拿到被污染答案（`chatgpt.com → 31.13.x.x`）。所以即使 mihomo 起来了，Google/ChatGPT 仍会失败。
2. 组存活的读取是“一次性采样”；url-test 组在第一轮测速完成前必然报 `alive: false`，与 provider 判定存在竞态，可能把健康启动误判为失败。

## 4. 本次修复

| # | 问题 | 修复 | 文件 |
|---|---|---|---|
| 1 | `AUTO-FOREIGN` 建在死源上 | assignment 模式下改用 `node-valid`（与 `US-FAST`/`GPT` 一致），无有效池时保留旧行为 | `macos/python/mpm_macos/config.py`（`AUTO-FOREIGN` 覆写） |
| 2 | 组存活一次性采样竞态 | 新增 20s、可被总体 deadline 截断的 settle 窗口，逐秒轮询直到两个组都存活 | `macos/python/mpm_macos/lifecycle.py::_group_liveness_settled` |
| 3 | 健康闸门检查错误的 provider | 新增 `_health_provider()`：存在 `node-valid` 时闸门检查 `node-valid`，否则回退 `subscription` | `macos/python/mpm_macos/lifecycle.py::_health_provider` |
| 4 | 系统 DNS 绕过 TUN | 新增 DNS 事务：Wi-Fi DNS 指向 TUN 网关 `198.19.0.1`；改动前快照原 DNS（0600），`stop`/切回 Nextin 时校验后恢复；boot 入口发现 core 未运行且残留快照时先恢复 DNS | `macos/python/mpm_macos/tun_dns.py`、`lifecycle.py::_apply_tun_dns`、`boot.py` |
| 5 | `status`/`health` 误报 `controller unreachable: TimeoutError` | 改用 `evaluate_with_deadline`（先读 `/version`、短超时触发 healthcheck、按 deadline 轮询） | `macos/python/mpm_macos/lifecycle.py::status/health` |
| 6 | node-pipeline 的 GPT 切换总是记录 `JSONDecodeError` | `PUT /proxies/<group>` 返回 204 无 body，空 body 现在返回 `{}`，不再把成功当失败 | `macos/python/mpm_macos/node_pipeline.py::_api_json` |

新增测试：`macos/tests/test_macos_tun_dns.py`（快照/应用/恢复/幂等，无宿主机依赖）。

## 5. 当前状态验证（2026-11-23 17:4x）

| 检查 | 结果 |
|---|---|
| mihomo / runner | 运行中 |
| launchd | `boot-switch` enabled、`core` disabled（设计如此） |
| 系统 DNS | Wi-Fi → `198.19.0.1`，`getaddrinfo` 返回 fake-ip |
| `AUTO-FOREIGN` / `GPT` / `US-FAST` / `PROXY` | alive=true |
| `node-valid` provider | 55 nodes，53 alive，17:22 刷新 |
| `subscription` provider | 40 nodes，**5 alive**（长期未更新） |
| `source1-local` provider | 35 nodes，**0 alive** |
| chatgpt / google / grok / baidu / github / cursor | 全部 HTTP 200 |
| `stop` → `start` 自检 | DNS 正确恢复为 DHCP，再指向 TUN；服务恢复 |
| boot 入口模拟 | `mihomo is already in charge…nothing to do`，exit 0 |
| `macos/tests` | 446 passed |

## 6. 仍存在的风险 / 未修复项

1. **订阅源质量差**：`subscription` 40 个节点仅 5 个存活，缓存停在 11-19。启动闸门已改为看 `node-valid`，但依赖订阅的区域组（如 `CURSOR` 0/2、`GROKBOT` 1/4）仍然不可用。
2. **Nextin supplement 过期**：`source1-local`（nextin.yaml）35 个节点全死，最后更新 11-19；需要重新导出 supplement 或移除该 provider，否则依赖它的组长期空转。
3. **node-valid 依赖流水线**：`node-pipeline` 每小时跑一次，本次已成功刷新；但历史上出现过 `step provide failed (exit 3)`，且失败没有告警，只落在日志里。
4. **硬断电后的 DNS 窗口**：关机/崩溃不会执行 `stop`，Wi-Fi DNS 会保留 `198.19.0.1`；boot 入口会在任何可能拒绝切换的分支之前恢复快照，但如果长期不启动 mihomo，需要手工执行 `stop` 或把 DNS 改回自动。
5. **install 的 hold 路径**被要求“零副作用”，因此不会修复残留 DNS；极端情况下安装时会短暂无 DNS。
6. **status 成功时无输出**（CLI 交互问题，不是故障）。
7. ~~仓库根目录有一个 Windows 路径产物 `C:\mihomo\logs\proxy-failover-rounds.jsonl`（未跟踪），建议删除。~~ **已修复（2026-11-23）**：新增共享模块 `scripts/portable_paths.py`；Windows 保持原布局，POSIX 落到 `$MPM_HOME`（默认 `~/.local/state/mihomo`）或当前 home。产物文件已删除，其余同类脚本的审计见 §8。

## 7. 变更清单（未提交）

```
 M macos/python/mpm_macos/config.py
 M macos/python/mpm_macos/lifecycle.py
 M macos/python/mpm_macos/paths.py
 M macos/python/mpm_macos/boot.py
 M macos/python/mpm_macos/node_pipeline.py
?? macos/python/mpm_macos/tun_dns.py
?? macos/tests/test_macos_tun_dns.py
 M scripts/proxy-auto-switch.py
 M scripts/sync-feishu-bitable.py
 M scripts/sync-xigua-ippool-feishu.py
 M scripts/health-check-provider.py
 M scripts/source1_independent.py
 M scripts/node_ops/probe.py
 M scripts/node_ops/runner.py
 M scripts/node_provider/runner.py
?? scripts/portable_paths.py
?? tests/test_proxy_auto_switch_portable_paths.py
 M tests/test_feishu_app_token_config.py
 M tests/test_sync_fetch_resilience.py
```

## 8. Windows 路径硬编码审计（2026-11-23）

问题模式：Windows 绝对路径（`C:\...` / `C:/...`）在 POSIX 上会被当作**单个相对文件名**；一旦用作写路径的默认值，就会在当前目录生成字面量文件/目录。

### 已修复（macOS 安装目录里会存在的脚本）

| 文件 | 问题 | 处理 |
|---|---|---|
| `scripts/proxy-auto-switch.py` | 6 个日志/状态/告警默认路径 + source3 读取路径 | 改用 `portable_path()` |
| `scripts/sync-feishu-bitable.py` | 8 个状态/日志/锁/ provider 目录默认值、`--config`、`C:\mihomo\mihomo.exe`、Windows `curl.exe` | 路径改用 `portable_path()`，core 用 `portable_core()`，非 Windows 用 `shutil.which("curl")` |
| `scripts/sync-xigua-ippool-feishu.py` | 5 个 ippool 状态/锁/日志默认值 | 复用 `base.portable_path()` |
| `scripts/health-check-provider.py` | `--core` 默认 Windows exe | `portable_core()`（macOS 下解析到已安装 core） |
| `scripts/node_ops/probe.py` | `default_core_path()` 返回 Windows exe | 非 Windows 用 `which("mihomo")` / `/usr/local/libexec/.../mihomo` |
| `scripts/node_ops/runner.py`、`scripts/node_provider/runner.py` | `DEFAULT_ENV_FILE` Windows | 非 Windows 用 `~/.hermes/.env` |
| `scripts/source1_independent.py` | 日志路径 `C:/mihomo/logs/...` | 复用 `portable_path()` |

同时更新了 3 个把旧 Windows 字面路径写进断言的测试（`test_feishu_app_token_config`、`test_sync_fetch_resilience`），并新增 `tests/test_proxy_auto_switch_portable_paths.py`。`macos` 安装清单已加入 `portable_paths.py`。

### 保留（Windows 专用工具，不随 macOS 安装/计划任务执行）

`connect_source3_main.py`、`extract_source1_readonly.py`、`fix-source3-health.py`、`live_status_*.py`、`run-node-pipeline.py`、`validate-source1-sample.py`、`*.ps1`、`*.bat`。

它们依赖 `mihomo.exe`、`AtlasCore_amd64.exe`、`SystemRoot` 等 Windows 能力，本就不应在 macOS 上运行；本次未改写，避免把 Windows 运维语义搬到 macOS。若未来要在 mac 上使用，应统一改为 `portable_path()` 并单独验证。

### 保留（安装但由 macOS 调用方显式传参覆盖，或仅 Windows 分支）

- `node_provider/app_extract.py` 的 `AtlasCore_amd64.exe` 默认值：仅 Windows App 提取路径使用，macOS 走 `--from-app source4` 并返回 `profile_missing`。
- `sync-feishu-bitable.py` 的 source1 cache 回退文件名列表（`sub1_check.txt` 等）：只读，macOS 上不存在即跳过，不会创建文件。
- `source3_compatible.py`：依赖 `ctypes.wintypes`，Windows 专用。

### 验证

- `pytest tests macos/tests`：**1295 passed, 16 skipped, 89 subtests**
- 安装目录导入自检：`sync-feishu-bitable.py / sync-xigua / health-check / node_ops / node_provider` 默认值均为 POSIX 路径，`default_core_path()` 解析到 `/usr/local/libexec/mihomo-proxy-management/current/mihomo`。

已同步到 `/usr/local/share/mihomo-proxy-management/python/mpm_macos/` 并重新渲染生产配置。

建议下一步：先修订阅/Nextin supplement 数据源，再提交以上代码；`node-pipeline` 的失败建议接入 `feishu-sync` 告警，避免“池子死了但没人知道”。

# 跨平台逻辑对照 / CROSS-PLATFORM

> **读者**：Windows 侧与 macOS 侧的开发者/Agent。两端**异步开发**，本文件是双方必须
> 共同维护的"接口契约"——改动任何一侧的共享逻辑，先读这里，改完更新这里。
> 最后更新：2026-11-19（feat/macos-compat PR #3）。

## 1. 一句话架构

两边跑的是**同一套业务逻辑**（订阅→测速→门控→分配，数据只存飞书），差别只在
**宿主适配层**：Windows 用计划任务 + `C:\mihomo`，macOS 用 launchd + `/Library/...`。

## 2. 共享单一来源（改一处，两端生效）

| 文件/资源 | 作用 | 注意 |
|---|---|---|
| `config/config.yaml.template` | 策略组/规则唯一来源。Windows 渲染器=install.ps1，macOS 渲染器=`macos/python/mpm_macos/config.py`（叠加 mac 专用覆盖，见 §4） | 改组/规则必须两侧重装生效；mac audit 会锁组形，Windows 侧改组要先看 `mpm_common/policy.py` |
| `common/python/mpm_common/policy.py` | 区域过滤器常量（CURSOR/CODEBUDDY/GROKBOT 系） | template 与 audit 都引用它，单一事实源 |
| `scripts/convert_sub.py` | 订阅转换/合并（三源候选）；`--source1-supplement` 多源模式 | 跨平台纯逻辑 |
| `scripts/sync-feishu-bitable.py` | 飞书表1(订阅源状态)/表2(可测速节点)/表3(GPT可用地区组) 写入链 | 两端共用；`--source-cache-dir`/`--source1-supplement` 是 mac 守护传的新参数 |
| `scripts/node_provider/` + `node_ops/` + `gpt_switch_entry.py` + `gpt_stability_policy.py` | 节点流水线核心（采集→测速门控→有效节点 provider；GPT 稳定切换） | 跨平台纯逻辑；**数据只存飞书两表** |
| `scripts/named_mutex_lock.py` | GPT 切换跨进程锁：Windows=kernel32 named mutex，POSIX=`flock` 文件锁（`NAMED_MUTEX_LOCK_DIR` 可改目录） | 两实现语义对齐：超时=LockBusy，持锁方死亡=内核释放 |
| **飞书 Bitable** `URo9bAsd5azYe5swAmtclIbEnNh` | 数据总线：「节点测速总表」「有效节点」「采集状态」（流水线）+ 表1/2/3、实时状态、西瓜IP池（sync 链） | 表按**名称**运行时定位，列结构见 `node_provider/schema.py` / `node_ops/schema.py`（两侧镜像，改一边必须同步另一边） |

## 3. 双链并行（谁写哪张表，别搞混）

```text
链 A「sync 链」（每端独立跑，Windows bat / mac feishu-sync 600s）:
  订阅抓取+本地补充 → 隔离内核测速(grok/chatgpt/openai+地区) → 表1/表2/表3
  入口: scripts/sync-feishu-bitable.py（--speedtest-write / --ninja-only）

链 B「流水线链」（自动分配用，Windows 每小时 / mac node-pipeline 3600s）:
  collect(订阅槽位+App通道+本地补充) → 总表按源替换
  → node_ops 测总表(隔离内核, 空闲端口, 不碰生产) → 有效节点表
  → node_provider --output → 本机 file provider
  Windows 入口: scripts/run-node-pipeline.py
  macOS 入口:   macos/python/mpm_macos/node_pipeline.py（含第 4 步稳定切换 + 翻转重渲染）
```

**自动分配只消费链 B**（总表→有效节点→本机 provider）。链 A 的表 2 不喂分配。

## 4. 平台差异对照（有意为之，勿"修复"）

| 维度 | Windows | macOS |
|---|---|---|
| 调度 | 计划任务（bat/ps1） | 4 个 LaunchDaemon：core / boot-switch / feishu-sync(600s) / node-pipeline(3600s)，均无 KeepAlive 风暴 |
| 生产目录 | `C:\mihomo` | `/Library/Application Support/mihomo-proxy-management`（= mihomo `-d`） |
| 有效节点 provider | `C:\mihomo\providers\node-valid.yaml` | `providers/node-valid.yaml`（在 `-d` 内，mihomo 拒绝 home 外路径） |
| 渲染器 | `install.ps1`（模板原样） | `config.py`（叠加覆盖：GPT/US-FAST/GROKBOT 审计形、fake-ip 198.19/16、provider interval 秒化、source1-local/nextin 补充、**valid_nodes 分配模式**） |
| 切换 | — | `mpm-switch`（osascript 密码框、Always On VPN 预检、失败回滚） |
| GPT 组管理 | 30 分钟链里 proxy-auto-switch | node-pipeline 第 4 步（同一 gpt_switch_entry） |
| 本地节点补充 | App 通道（AtlasCore/Ninja/西瓜进程内存/DPAPI 缓存） | `--supplement-yaml`：`providers/nextin.yaml` + `providers/supplements/*.yaml`（任意来源文件丢入即入库总表） |
| 已知噪音 | — | node-pipeline 末步报 `missing_group MUSE` 退出 1：mac 模板无 MUSE 组，纯日志噪音，分配/切换不受影响 |

## 5. 多机共存语义（Windows + Mac 同时跑）+ 写入纪律裁定

**用户裁定（2026-11-20，最高优先级，覆盖此前的"按源替换/保留"实现）——两个功能互不干扰的根基：**

1. **抓取侧（node_provider collect、sync 链的表写入）只有追加和更新，没有删除能力**。
   写入 = upsert：按行身份匹配（表2/表3：测速路径+来源+节点名；表1：源名称；总表：来源+节点名），
   命中→更新该行字段；未命中→新增。载荷差异永不触发删除——一台机器的轮次绝不可能删掉
   另一台机器（或自己）的任何来源行。
2. **测速/自动切换侧（node_ops、sync 链测速半边）纯读节点身份，只写测量列**：
   速率（延迟ms）、实际地区、可不可用（存活/GPT可用/连续轮数）。
3. **同步时间 = 时间墓碑时钟，语义是"最后一次测出有效速率的时刻"**（按小时累计）。
   本轮测出有效速率 → 推进时钟；测不出 → 时钟不动（旧值保留，原地更新不重置）。
   新行以创建时刻起表。抓取侧的身份更新**不推进**时钟。
4. **删除只有一个来源：时间墓碑**。行超过 `FEISHU_STALE_NODE_MINUTES`（默认 10080 分钟
   = 7 天，按小时精确）没有任何有效速率数据 → 删除，与载荷无关、与哪台机器无关。
   时钟不可读 → 保留（fail-closed）。Ninja/独立路径行维持其独立的 60 分钟新鲜度窗口。
   「有效节点」表是测速侧的派生输出，整表重建（它就是速率/可用性本身）。

落地位置：sync 链 `upsert_records`；总表 `node_provider.feishu.upsert_source_rows` +
`tombstone_stale_rows`；测量回写 `node_ops.feishu.upsert_node_rows` + 墓碑；测试锚
`tests/test_write_discipline.py`。

- **采集 24h 滚动去重**（S7）：任一机器抓过某源（成功或失败都记录），另一台 24h 内自动跳过——
  状态在飞书「采集状态」表；测速/发布不受影响，照常每小时。
- 凭据：两端可用**同一个**飞书应用（推荐）；mac 端凭据在 0600 的
  `state/subscription.env`（FEISHU_APP_ID/SECRET/BITABLE_APP_TOKEN + SUBSCRIPTION_URL*），
  缺失=报告+下周期重试，不阻塞安装。

## 6. 修改纪律（异步开发不互相踩）

1. **共享文件**（§2 表格）里的任何行为变更：提交说明写清"影响两端"，mac 侧守护是
   `install` 复制后运行的——升级 mac 要重跑 install，Windows 要重新部署脚本目录。
2. **新增 CLI 参数**给共享脚本时：必须有跨平台默认值（不许把 Windows 路径设成必填）；
   mac 守护通过 argv 覆盖路径，参数名一旦发布就不要改名。
3. **表结构变更**（schema.py）：`node_provider/schema.py` 与 `node_ops/schema.py` 是镜像，
   两侧同时改；飞书旧列会被 `ensure_table` 自动补齐，但删列要人工。
4. **macos/ 目录**改动不影响 Windows（Windows 不读这个目录）；反之 Windows 的
   bat/ps1 改动不应改 mac 行为——若必须改共享行为，走 §2 共享文件。
5. mac 侧守护代码 = `macos/python/mpm_macos/node_pipeline.py`，是 `run-node-pipeline.py`
   的移植：**改步骤顺序/新增步骤时两边同步**（两个文件头部互相指认）。
6. 测试基线：mac 侧 `pytest` 允许的失败=4 个本分支历史 + 5 个上游 Windows 宿主假设
   （列表见 PR #3）；出现清单外失败先 `git stash` 对照再归因。

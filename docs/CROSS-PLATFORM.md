# 跨平台逻辑对照 / CROSS-PLATFORM

> **读者**：Windows 侧与 macOS 侧的开发者/Agent。两端**异步开发**，本文件是双方必须
> 共同维护的"接口契约"——改动任何一侧的共享逻辑，先读这里，改完更新这里。
> 最后更新：2026-11-28（新增从 GitHub 用飞书供池的 macOS/Windows 引导入口与池刷新约定）。

## 1. 一句话架构

两边跑的是**同一套业务逻辑**（订阅→测速→门控→分配，数据只存飞书），差别只在
**宿主适配层**：Windows 用计划任务 + `C:\mihomo`，macOS 用 launchd + `/Library/...`。

## 2. 共享单一来源（改一处，两端生效）

| 文件/资源 | 作用 | 注意 |
|---|---|---|
| `config/config.yaml.template` | 策略组/规则唯一来源。Windows 渲染器=install.ps1，macOS 渲染器=`macos/python/mpm_macos/config.py`（叠加 mac 专用覆盖，见 §4） | 改组/规则必须两侧重装生效；mac audit 会锁组形，Windows 侧改组要先看 `mpm_common/policy.py` |
| `common/python/mpm_common/policy.py` | 区域过滤器常量（CURSOR/CODEBUDDY/GROKBOT 系） | template 与 audit 都引用它，单一事实源 |
| `scripts/deploy/` | GitHub 引导命令、飞书权限校验、供应链锁与校验、配置及服务安装的宿主接线 | macOS/Windows 共用入口；凭据来自环境变量、stdin JSON 或交互 |
| `macos/scripts/bootstrap.sh` | macOS 引导入口（Apple Silicon，root）：`sudo ./macos/scripts/bootstrap.sh [--dry-run / --no-start / --skip-supply / --skip-install / --min-nodes N]`；按需准备专用 venv/PyYAML 后调用 `scripts/deploy/__main__.py --platform macos` | 与 Windows 同一 Python 流程；接口锁定、勿改名；由 `feat/github-bootstrap-deploy` 交付 |
| `scripts/deploy.ps1` | Windows 引导入口（管理员 PowerShell）：Bootstrap 阶段准备 Python 3.10+/PyYAML 后调用同一流程；Install 阶段安装资产并注册任务，支持 `-InstallDir` | 进程级 `-ExecutionPolicy Bypass`；两个飞书任务以部署账号 Interactive/Limited 运行 |
| `scripts/render_config.py` | 把渲染好的 config 的 `proxy-providers.subscription` 改成受管 pool 的 file provider | 纯飞书供池时由 install 调用；显式 `--output` 时总是写出 |
| `scripts/feishu_pool.py` | 从「节点测速总表」生成 mihomo 文件节点池 | 只读飞书；引导建池一次，两端每轮 pipeline 重建；低于节点下限时保留原池 |
| `scripts/convert_sub.py` | 订阅转换/合并（三源候选）；`--source1-supplement` 多源模式 | 跨平台纯逻辑 |
| `scripts/sync-feishu-bitable.py` | 飞书表1(订阅源状态)/表2(可测速节点)/表3(GPT可用地区组) 写入链 | 两端共用；`--source-cache-dir`/`--source1-supplement` 是 mac 守护传的新参数 |
| `scripts/node_provider/` + `node_ops/` + `gpt_switch_entry.py` + `gpt_stability_policy.py` | 节点流水线核心（采集→测速门控→有效节点 provider；GPT 稳定切换） | 跨平台纯逻辑；**数据只存飞书两表** |
| `scripts/named_mutex_lock.py` | GPT 切换跨进程锁：Windows=kernel32 named mutex，POSIX=`flock` 文件锁（`NAMED_MUTEX_LOCK_DIR` 可改目录） | 两实现语义对齐：超时=LockBusy，持锁方死亡=内核释放 |
| **飞书 Bitable** | 数据总线：「节点测速总表」「有效节点」「采集状态」（流水线）+ 表1/2/3、实时状态、西瓜IP池（sync 链） | 表按**名称**运行时定位，列结构见 `node_provider/schema.py` / `node_ops/schema.py`（两侧镜像，改一边必须同步另一边）。app_token **没有代码内置默认值**（2026-11-27）：每台机器自带自己的表格地址，解析优先级 `--app-token` > 进程环境变量 `FEISHU_BITABLE_APP_TOKEN` > env 文件同名键；三处都没有时 `node_provider` / `node_ops` 以退出码 2 报告键名（`app_token_missing`）并等下个周期重试 |

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
  macOS 入口:   macos/python/mpm_macos/node_pipeline.py（先重渲染或刷新 Provider，再切换 GPT）
```

**自动分配只消费链 B**（总表→有效节点→本机 provider）。链 A 的表 2 不喂分配。

**Windows 纯飞书池的流水线入口（2026-11-28）**：`scripts/run-node-pipeline.py` 的 pool 模式
不再无条件传仓库内 `config/subscription.env`（新机没有该文件，真实 `node_provider` 会以退出码 2
每轮必败）。改为选择当前安装的 env 文件（Windows=`MIHOMO_DIR\subscription.env`；解析见
`portable_paths.default_env_file`），并把**同一个 env 文件**显式传给 collect/pool/ops/provide。
collect 只在两类输入之一可用时运行：安装 env 或仍有效的仓库源文件里配置了 `SUBSCRIPTION_URL_n`，
或存在可用的 source4（西瓜加速）App 输入；两者都没有时记 `SKIP collect` 日志，正常跑
pool/ops/provide。非零退出码一律算失败（`node_provider` 的 3 同时表示写权限/API 故障，不再当
"空采集"吞掉）。pool 模式的 ops `--core` 与 provide `--output` 都取 `MIHOMO_DIR` 下的安装路径；
订阅模式三个旧 argv 语义不变。`scripts/render_config.py`：显式给了 `--output` 时，即使输入已是
目标形状也写出该文件；默认原地重复（无 `--output` 且 changed=False）不重写。

**Windows 引导入口的宿主接线（2026-11-28，`scripts/deploy.ps1`）**：
- 入口分两阶段：默认 `Bootstrap` 准备 Python 3.10+ / PyYAML 后调用 `scripts/deploy/__main__.py`；
  Python 适配器回调 `deploy.ps1 -Phase Install -StagingDir <dir> [-NoStart]`（`platforms.py` 已显式带
  `-Phase Install`）。Bootstrap 选定的绝对解释器经子进程环境 `MPM_PYTHON` 传给 Install 阶段，
  `Render-Config` 与任务注册都优先用它，绝不悄悄换另一个 Python。
- Install 阶段先校验暂存文件齐全、按 SHA256 判断内容是否变化；全部一致时不停止运行中的 mihomo。
- 引导注册的 `MihomoFeishuBitableSync` / `MihomoNodePipeline` 以**当前部署账号**的
  Interactive/Limited principal 运行（绝不 SYSTEM/Highest；`Assert-BootstrapTaskPrincipal` /
  `Test-ElevatedTaskIdentity` 会拒绝提升身份，pipeline 也拒绝继承提升后的 sync principal）。
  注册前先用 `icacls` + 当前用户 SID 追加授权：对 `subscription.env` 追加只读（保留 Python 已设的
  SYSTEM/Administrators ACL），对 `logs` 与 `providers` 追加 `(OI)(CI)M`（继承到子文件）；
  每条 `icacls` 都检查退出码，失败则不注册依赖任务，也不给 Everyone、不读取凭据内容。
  任务动作由 Task Scheduler 直接执行**绝对 Windows PowerShell 5.1 的 `-File` 入口**
  （`sync-feishu-bitable.ps1 -Run` / `install-node-pipeline-task.ps1 -Run`，固定 `-InstallDir` 与
  `-Interpreter`），不再生成 `.cmd` 启动器或拼 cmd 字符串；`-Run` 通过进程环境设置 `MIHOMO_DIR`，
  以 `&` 调固定 Python，sync 先于 switch 且任一非零即让任务失败。手工 `-RegisterTask` 的旧行为
  （schtasks /RL LIMITED、PATH python、仓库内 `.bat` 入口）保持不变。Bootstrap 无论默认或显式
  `InstallDir` 都用规范化绝对路径经 `MIHOMO_DIR` 传给 CLI/Install 阶段，并跳过 Microsoft Store 的
  `WindowsApps` 占位 python 再按版本挑选解释器。
- `--json` 要求 stdout 只有一个 JSON 值：两个平台的安装子进程 stdout 统一导向 stderr，
  交互提示也写 stderr。

**从 GitHub 用飞书供池引导（2026-11-28，macOS + Windows；功能分支 `feat/github-bootstrap-deploy`）**：

- 入口：macOS `sudo ./macos/scripts/bootstrap.sh`（Apple Silicon；Python ≥ 3.9 + 可用
  venv，引导可按需准备专用 venv/PyYAML），Windows 管理员 PowerShell 里
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\deploy.ps1`
  （Python ≥ 3.10，缺失时按锁下载官方安装器并装 PyYAML）。两端都要求显式
  `git checkout feat/github-bootstrap-deploy`（功能分支交付、尚未并入 `main`），
  命令细节见 `docs/GITHUB-DEPLOY.md`。
- 同一流程（`scripts/deploy/__main__.py`）：地址 -> 飞书访问校验（读表 + 写「部署记录」
  探针）-> 供应链（`supply.lock.json` 的 URL+大小+SHA256，滚动资产优先用 GitHub API
  digest）-> 节点池（无订阅 URL 时）-> 写本机凭据 -> 平台安装器。任一步失败即停；
  供应链失败回滚本轮暂存，安装前失败不留半套安装。
- 凭据优先级：进程环境变量（`MPM_FEISHU_BASE_URL` / `MPM_FEISHU_APP_ID` /
  `MPM_FEISHU_APP_SECRET`；地址也认 `MPM_FEISHU_BITABLE_APP_TOKEN`）> stdin JSON
  （键 `feishu_url` / `app_id` / `app_secret`）> 隐藏交互；值不进 argv。可选
  `MPM_SUBSCRIPTION_URL` 或本机文件已有的有效主订阅会选择订阅模式；`_2.._4` 可提供辅助采集源。
- 开关：`--dry-run` 只做读校验 + 池门槛（写探针跳过，判定只看读权限），不下载/不写池/
  不写凭据/不建目录/不安装；`--no-start` 照常安装与注册任务但不启动服务；`--skip-supply`
  复用已有暂存；`--skip-install` 到写凭据为止；`--min-nodes` 仅纯飞书供池生效（默认 1）。
  退出码 0/2/3/4/5/6（130=交互中断）。
- 路径：Windows `%MIHOMO_DIR%\providers\feishu-pool.yaml` 与
  `%MIHOMO_DIR%\subscription.env`（默认 `C:\mihomo`，`-InstallDir` 可改）；macOS
  `/Library/Application Support/mihomo-proxy-management/providers/feishu-pool.yaml` 与
  `state/subscription.env`（0600）。飞书池路径必须是被 pipeline 认得的受管路径
  （`./providers/feishu-pool.yaml` 或其安装目录绝对形式）；指到自定义文件会退回订阅模式。
- 池刷新：Windows 每小时 `MihomoNodePipeline` 的 pool 步骤重建池，内容哈希记在
  `logs/pool.sha256`，成功热加载（`PUT /providers/proxies/subscription`）后才推进标记；
  macOS node-pipeline 每轮做同一件事。取数/门槛/写文件失败保留旧池（退出码 3/4/5，
  原子写）；纯飞书且无订阅、无本机补充输入时记 `SKIP collect` 后照常 pool/ops/provide。
- Windows 任务：`MihomoFeishuBitableSync`（默认 30 分钟，先 sync 后 switch）与
  `MihomoNodePipeline`（每小时）都以当前部署账号 Interactive/Limited 运行、用户登录时才执行；
  核心 `MihomoProxy` 是独立的 SYSTEM/Highest 开机任务。注册前用 `icacls` 给部署账号追加
  `subscription.env` 只读与 `logs`/`providers` Modify，拒绝继承提升的 sync 身份。
  macOS 对应 feishu-sync / node-pipeline 两个 LaunchDaemon。
- 验收边界：Windows 的真实权限/计划任务与 macOS 的 root 安装尚未实机验证，由主脑在最终
  集成分支核对；文档与离线测试只锁定接口与路径。

**表2 数据来源（2026-11-21，影响两端）**：西瓜加速没有订阅 URL，链 A 每轮优先读链 B 的
「节点测速总表」作为其节点清单（按 来源 过滤、按 节点名 去重、取 节点配置JSON），
表1 拉取结果=总表；读不到时才回退本地 `source4-cache.yaml`（拉取结果=缓存）。
「可测速节点」因此是全部可用节点的数据库，不再依赖无人刷新的本地缓存；
总表仍只由链 B 写，链 A 只读不改（约 30 分钟一轮，测速结果照常 upsert 进表2）。

## 4. 平台差异对照（有意为之，勿"修复"）

| 维度 | Windows | macOS |
|---|---|---|
| 调度 | 计划任务（bat/ps1） | 4 个 LaunchDaemon：core / boot-switch / feishu-sync(600s) / node-pipeline(3600s)，均无 KeepAlive 风暴 |
| 引导入口 | `scripts/deploy.ps1`（管理员 PowerShell，进程级 `-ExecutionPolicy Bypass`；`-InstallDir` 自定义目录） | `macos/scripts/bootstrap.sh`（`sudo`；Apple Silicon，Python ≥ 3.9 + venv） |
| 引导运行时 | Python ≥ 3.10（缺失时引导按锁安装）+ PyYAML（pip） | 引导可按需准备专用 venv + PyYAML |
| 飞书池路径 | `%MIHOMO_DIR%\providers\feishu-pool.yaml`（默认 `C:\mihomo`） | `/Library/Application Support/mihomo-proxy-management/providers/feishu-pool.yaml` |
| 飞书任务身份 | sync/pipeline 以部署账号 Interactive/Limited（登录才跑）；核心 `MihomoProxy` = SYSTEM/Highest | feishu-sync / node-pipeline 为 root LaunchDaemon |
| 池刷新 | 每小时 pipeline 的 pool 步骤 + controller 热加载 | 每小时 node-pipeline 轮次（接口锁定，同一语义） |
| 生产目录 | `C:\mihomo` | `/Library/Application Support/mihomo-proxy-management`（= mihomo `-d`） |
| 有效节点 provider | `C:\mihomo\providers\node-valid.yaml` | `providers/node-valid.yaml`（在 `-d` 内，mihomo 拒绝 home 外路径） |
| 渲染器 | `install.ps1`（订阅模式渲染模板，飞书供池经 `scripts/render_config.py` 转文件 provider） | `config.py`（叠加覆盖：GPT/US-FAST/GROKBOT 审计形、fake-ip 198.19/16、provider interval 秒化、source1-local/nextin 补充、**valid_nodes 分配模式**） |
| 切换 | — | `mpm-switch`（osascript 密码框、Always On VPN 预检、失败回滚） |
| GPT 组管理 | 30 分钟链里 proxy-auto-switch（GPT+MUSE+US-FAST 三组） | node-pipeline 刷新 `node-valid` 后对 GPT 做组级 trace 测速；隔离验证实际出口和 ChatGPT 主页，按美国 → 新加坡 → 日本取同级延迟最低的合格节点，切换后读回确认 |
| 本地节点补充 | App 通道（AtlasCore/Ninja/西瓜进程内存/DPAPI 缓存） | `--supplement-yaml`：`providers/nextin.yaml` + `providers/supplements/*.yaml`（任意来源文件丢入即入库总表） |

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

落地位置：sync 链 `upsert_records`（表1/表2/表3 及 ninja 流）；独立通道
`source1_independent.replace_scope` / `source3_compatible`（原本连 Ninja 行都删，已改）；
总表 `node_provider.feishu.upsert_source_rows` + `tombstone_stale_rows`；测量回写
`node_ops.feishu.upsert_node_rows` + 墓碑；测试锚 `tests/test_write_discipline.py`
（host-neutral，Windows 上同样运行）+ 各写入点的通道测试。

### 探测内核与宿主 TUN 的隔离（2026-11-21，`NODE_OPS_INTERFACE_NAME`）

隔离测速内核的出站会被宿主自己的 mihomo TUN 劫持：DNS 返回宿主的 fake-ip、
节点连接被双重代理穿过宿主当前选中的节点——**宿主池退化时全部探测随之失败**
（自指陷阱：池差→测不了→池好不了；2026-11-20/21 Mac 实测，197 行仅 6 存活）。
修复：`node_ops/probe.py` 与 sync 链 `test_isolated`（2026-11-22）都支持
`NODE_OPS_INTERFACE_NAME` 环境变量——设置后临时配置加 `interface-name`（探测
socket 绑物理网卡直连，绕开 TUN）且 DNS 换用大陆可达解析器
（223.5.5.5/119.29.29.29）；未设置时行为不变（Windows 默认不受影响，若 Windows
也跑 TUN+测速可设 Ethernet 同款受益）。mac 两个守护都自动探测默认路由接口
（`route -n get default`）并注入：node-pipeline 走子进程 env，feishu-sync
在进程内 `os.environ.setdefault` 后按路径调用（同文件加载链共享环境）。

### 订阅 URL 可选 / 飞书供池与 SECRET 生成（2026-11-28，影响两端）

安装模式由「主订阅 URL 是否真实配置」决定，两端语义必须一致：

1. **主订阅 URL 已配置** → 订阅模式（行为不变）。
2. **主订阅 URL 空 / 未配置** → 飞书供池模式：名为 `subscription` 的 provider 变成
   file provider，读 `providers/feishu-pool.yaml`（macOS 侧 `config.POOL_PROVIDER_PATH`，
   Windows 侧同路径形状）。provider 名字不变，各策略组 `use:` 与审计两种模式共用。
   池文件必须已存在且至少 1 个节点（引导流程 / 流水线周期写出），否则 fail closed。
3. **controller SECRET 不再可选**：全新机器的 `subscription.env` 没有它，macOS
   `load_secrets` 拒绝缺失、Windows 热重载需要 bearer。引导（`scripts/deploy/bootstrap.py`）
   写密钥时：现有 SECRET 有效 → 保留；空白 / 缺失 → 生成 `secrets.token_urlsafe(32)`。
   值只进文件，日志与 JSON 摘要只出现键名和数量。
4. **pool_required 与最终安装模式一致**：主 URL 判定顺序 = 环境覆盖（`MPM_SUBSCRIPTION_URL`）
   > 已有密钥文件的 `SUBSCRIPTION_URL`；空值与示例地址（`example.com` / `example.org` /
   `provider.example.invalid`）不算真实配置。环境只提供 `_2.._4` 辅助订阅而无主订阅时，
   仍需生成飞书池。dry-run 走到节点计数，但不生成、不写入任何凭据文件。
5. **推导出的主 URL 同步写回密钥文件**（2026-11-28 补修）：环境或旧文件里的占位主 URL
   会被明确清空（`SUBSCRIPTION_URL=`），真实原值与「环境覆盖优先」保持不变——否则
   `pool_required` 判定是池模式、安装器却从文件/环境拿到占位 URL 选了订阅模式。
   macOS 引导的 install 子进程把密钥文件里的主 URL 作为 `MPM_SUBSCRIPTION_URL` 覆盖
   传给安装器，调用环境里的失效值无法绕过规范化（Windows 接线不变）。
6. **流水线每周期刷新飞书池**（2026-11-28，macOS 已实现，Windows 入口后续同步）：
   飞书供池机器的流水线在 collect 之后插入 `feishu_pool.py` 步骤重建
   `providers/feishu-pool.yaml`（先并入新抓节点，再出池）；文件内容变化时对运行中的
   内核热重载该 provider（mihomo 不会自己重读 `type: file`），重载成功才落
   `pool.sha256` marker，失败下周期重试。纯池机器（无任何订阅槽位且无 supplement
   文件）记录并跳过 collect（零输入的 collect 只会因零节点退出非零），ops/provide
   照常刷新；有订阅槽位（含辅助）或 supplement 时保持采集。步骤规划、dry-run 与
   实际执行共用同一 `build_step_argv`。

### Windows 端接入清单（拉取 main 即生效）

**自动继承（共享文件，无需 Windows 侧改动）**：表1/2/3 与总表全部 upsert 化、
时钟语义（有效速率才推进）、7 天墓碑、ninja/独立通道不再删除（旧 Ninja 行由
60 分钟独立窗口自然过期——首轮 upsert 后最多残留 1 小时属预期）、source3 不再
误删 Ninja 行。

**Windows 侧自检**：① 计划任务 argv 未变，无需改；② 跑全套测试（裁定由
`test_write_discipline.py` 等强制，违反即红）；③ 观察首轮日志的
`upsert ... tombstoned=N` 行确认窗口行为；④ 若有 Windows 专属适配问题（路径、
计划任务、Ninja 会话读取等）由 Windows 侧自行修——但**不得改变写入纪律本身**：
任何"改回替换/按载荷删除"的提交都会被裁定测试拒绝。
⑤ `node_provider` / `node_ops` 不再内置 app_token（2026-11-27）：`--env-file` 指向的文件
（或进程环境变量）必须有 `FEISHU_BITABLE_APP_TOKEN`，否则这两个入口以退出码 2 退出并打印
`app_token_missing: FEISHU_BITABLE_APP_TOKEN`（只报键名）。

- **采集 24h 滚动去重**（S7）：订阅源是共享的，任一机器抓过（成功或失败都记录）其他机器
  24h 内跳过；App 通道按机器隔离（`读取机器` 一并入键），本机失败只影响本机——macOS 没有
  西瓜加速缓存也不会压住 Windows 的提取。状态在飞书「采集状态」表；测速/发布不受影响，照常每小时。
- 凭据：两端可用**同一个**飞书应用（推荐）；mac 端凭据在 0600 的
  `state/subscription.env`（FEISHU_APP_ID/SECRET/BITABLE_APP_TOKEN + SUBSCRIPTION_URL*），
  缺失=报告+下周期重试，不阻塞安装。
- 表格地址（app_token，2026-11-27，影响两端）：`node_provider` / `node_ops` / `sync-feishu-bitable.py` /
  `live_status_publish.py` 统一按 `--app-token` > 进程环境变量 `FEISHU_BITABLE_APP_TOKEN` > env 文件
  同名键解析，代码里不再有任何内置默认值——否则 argparse 默认值会压过 env 文件里配置的地址，
  把机器连到别的表格上。Windows 生产接入前确认 env 文件里有这个键（见上方接入清单 ⑤）。

### 新机引导：macOS venv 与 `bootstrap`（2026-11-28）

新机器从 Git 检出后只有一条命令，不再要求手工装 Python 包：

```sh
sudo ./macos/scripts/bootstrap.sh [--dry-run] [--no-start] [--skip-supply] [--skip-install] [--min-nodes N]
# 检出上下文的等价入口（仍未装 CLI 时）：
PYTHONPATH=common/python:macos/python /usr/bin/python3 -m mpm_macos.cli bootstrap ...
```

- **专用 venv**：`/usr/local/libexec/mpm-python-venv`（`Layout.libexec_venv` / `venv_python`，
  随 `--root` 派生）。`lifecycle.ensure_venv` 先复用它：解释器存在且 `import yaml` 通过即复用；
  否则按 **`MPM_BASE_PYTHON` → Python.org `/Library/Frameworks/Python.framework/Versions/*`
  （版本高优先）→ `/opt/homebrew/bin/python3` → `/usr/local/bin/python3` →
  `/usr/bin/python3` → `PATH`** 找基础解释器（可信 argv 探测 `>=3.9`），再
  `-m venv --clear` 重建**这个固定目录**并 `-m pip install --disable-pip-version-check
  --quiet pyyaml`，最后复验 `import yaml`。坏 venv 只会重建该目录，且必须先找到可用替代解释器。
- **交给 deploy**：`lifecycle.bootstrap` 确认本检出有 `scripts/deploy/__main__.py`，用继承
  stdin/stdout/stderr 的 `subprocess.run` 以 argv 调用
  `venv/python3 scripts/deploy/__main__.py --platform macos ...`，透传
  `--dry-run/--no-start/--skip-supply/--skip-install/--min-nodes`，并把 `MPM_PYTHON` 设为
  venv 给安装子进程；子进程退出码原样作为 CLI 退出码。stdout 的生命周期报告仍按 CLI 的
  `--quiet/--json` 输出（deploy 自己的进度走 stderr）。
- **不加锁、拒绝 `--root`**：`bootstrap` 不进 `MUTATING_COMMANDS`（`install` 子进程自己拿
  生命周期锁）；CLI 明确拒绝 `--root`，避免沙箱路径漏传时 venv/安装悄悄落到生产路径。
- **解释器兼容**：`mpm_common.controller_health.TimeoutSpec` 改为
  `typing.Optional`/`typing.Union` 写法，系统 `/usr/bin/python3`（3.9）上的
  `python3 -m mpm_macos.cli --help` / `import` 不再因 PEP 604 运行时求值而报错。
- `scripts/deploy/platforms.py` 的 `MACOS_DEFAULT_PYTHON` 与上述 venv 路径一致；`_common.sh`
  在没有 venv 时用 `MPM_BASE_PYTHON` 或 `/usr/bin/python3` 引导，普通管理命令仍优先用已安装 CLI。

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
   Windows 侧基线（2026-11-20，f4ead73 + 适配修复）：全套 **1230 passed / 279 skipped /
   0 failed**。POSIX 专属语义（chmod 000 读阻断、0600 权限位断言、secret 权限门）在
   win32 跳过；`extract_nextin_subscription.apply_env_write` 的 root 检查已 host-safe
   化（Windows 无 euid 视为非特权），Mac 行为不变。

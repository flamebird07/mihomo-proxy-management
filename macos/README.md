# macOS（Darwin arm64）部署说明

本目录是 macOS 侧的全部实现：`mpm_macos` Python 包、LaunchDaemon 模板、薄包装脚本和
host-free 测试。**策略规则只有一份**：仓库根目录的 `config/config.yaml.template`，
Windows 与 macOS 共用。macOS 渲染器注入本机秘密、切换 TUN/mixed 技术字段，并叠加
**macOS 专用覆盖**（严格区域过滤、`US-FAST` fallback、`GPT` 组、`PROXY` 不含原始节点、
provider interval 秒数、fake-ip 198.19/16）。这些覆盖只存在于 `macos/python/mpm_macos/config.py`，
**不修改共享模板**，因此 Windows 行为不变。

> 合并本分支后**不更新 2209 生产工作区**。2209 的 `C:\mihomo\config.yaml` 与
> `subscription.env` 保持原样，等网管单独安排；本分支只改仓库文件。

## 1. 架构

```text
common/python/mpm_common/        平台/执行器/脱敏/原子写/controller 健康/策略纯函数
macos/python/mpm_macos/
  paths.py            系统路径布局（root 可注入，测试用临时目录）
  preflight.py        Darwin + arm64 + root + 命令能力（不探测 /dev/net/tun）
  context.py          CLI/mpm-switch/boot 共用的安装上下文：登记唯一绝对 mihomo 路径
  launchdctl.py       只操作固定 label 的 launchctl 适配器
  plist.py            plistlib 渲染 + 静态审计
  network_proxy.py    networksetup 快照/设置/恢复/读回校验/事务状态
  locking.py          fcntl.flock（lifecycle 锁 + core 锁）
  config.py           统一模板渲染 TUN/mixed、macOS 专用组覆盖、策略校验、mihomo -t
  conflict.py         外部 VPN/端口/默认路由/198.18 地址冲突检测（纯解析 + 注入执行器）
  probe.py            切换后的 60 秒 HTTP 探测（国内必通、国外至少一项）
  switch.py           mpm-switch：Nextin <-> mihomo，失败自动回滚到 Nextin
  boot.py             开机入口（boot-switch LaunchDaemon）：等登录用户/默认路由 -> 复用 switch 的 root 流程
  feishu_sync.py      飞书测速同步入口（feishu-sync LaunchDaemon，StartInterval 驱动）
  node_pipeline.py    节点自动分配入口（node-pipeline LaunchDaemon：collect->测速门控->有效节点 provider->稳定切换）
  lifecycle.py        install/configure/start/stop/restart/status/health/groups/uninstall
  runner.py           持有 core 锁、启动前冲突检测、可选 caffeinate -i、信号转发、退出码映射
  cli.py              命令入口（无业务逻辑）
macos/templates/com.mihomo-proxy-management.core.plist.template
macos/templates/com.mihomo-proxy-management.boot-switch.plist.template
macos/templates/com.mihomo-proxy-management.feishu-sync.plist.template
macos/bin/mihomo-proxy-management      薄入口（安装时注入包路径）
macos/bin/mpm-switch                   切换入口（安装到 /usr/local/bin/mpm-switch）
macos/scripts/*.sh                     薄包装（真实逻辑在 Python）
macos/tests/                           host-free 测试（test_macos_*.py + macos_test_support.py）
```

关键约束：

* 业务模块不散落 `sys.platform` / `os.geteuid()`；平台事实由
  `mpm_common.platforms.PlatformFacts` 注入，测试可在 Windows/Linux 上覆盖 Darwin 分支。
* 所有会改状态的命令（`install`/`configure`/`start`/`stop`/`restart`/`uninstall`）
  在 CLI 统一入口以非阻塞 `fcntl.flock` 获取
  `/var/run/mihomo-proxy-management/lifecycle.lock`；拿不到锁立即以退出码 2 失败，
  不会交错修改配置、launchd 与代理快照。锁只在入口获取一次，`install`→`start`→回退、
  `restart`→`stop`+`start` 内部不再加锁，因此不存在重入死锁。
* 所有外部命令（`launchctl`、`networksetup`、`mihomo`、`ifconfig`、`route`）都经
  `Executor` 以 argv 列表执行，**从不拼 shell**；服务名含空格也只是一个 argv 元素。
* 任何输出都经过 `mpm_common.sanitize`，不会出现订阅 URL、controller secret、
  URL query 或节点凭据。

## 2. 安装路径

```text
/Library/LaunchDaemons/com.mihomo-proxy-management.core.plist
/Library/LaunchDaemons/com.mihomo-proxy-management.boot-switch.plist
/Library/LaunchDaemons/com.mihomo-proxy-management.feishu-sync.plist
/usr/local/bin/mihomo-proxy-management
/usr/local/bin/mpm-switch
/usr/local/libexec/mihomo-proxy-management/current/mihomo
/usr/local/libexec/mihomo-proxy-management/current/runner
/usr/local/libexec/mihomo-proxy-management/scripts/{install,start,stop,restart,status,health-check,uninstall}.sh
/usr/local/libexec/mihomo-proxy-management/scripts/convert_sub.py
/usr/local/libexec/mihomo-proxy-management/scripts/sync-feishu-bitable.py
/usr/local/share/mihomo-proxy-management/python/{mpm_common,mpm_macos}
/usr/local/share/mihomo-proxy-management/templates/
/Library/Application Support/mihomo-proxy-management/config.yaml          (0600)
/Library/Application Support/mihomo-proxy-management/providers/subscription.yaml
/Library/Application Support/mihomo-proxy-management/{GeoIP.dat,GeoSite.dat,Country.mmdb} (0644)
/Library/Application Support/mihomo-proxy-management/state/subscription.env (0600)
/Library/Application Support/mihomo-proxy-management/state/deployment.json  (0600)
/Library/Application Support/mihomo-proxy-management/state/network-proxy-backup.json (0600)
/Library/Application Support/mihomo-proxy-management/state/feishu/          (0700，飞书同步状态)
/Library/Application Support/mihomo-proxy-management/logs/{core.out.log,core.err.log}
/Library/Application Support/mihomo-proxy-management/logs/{boot-switch.out.log,boot-switch.err.log}
/Library/Application Support/mihomo-proxy-management/logs/{feishu-sync.out.log,feishu-sync.err.log}
/Library/Application Support/mihomo-proxy-management/logs/feishu-bitable-sync.log
/var/run/mihomo-proxy-management/{lifecycle.lock,core.lock}
```

**mihomo 的 home（`-d`）是 `support` 根目录**，即
`/Library/Application Support/mihomo-proxy-management`：`mihomo -t`（安装校验）与常驻
runner 的 `mihomo -d` 都用它，LaunchDaemon 的 `--state-dir` 与 `WorkingDirectory` 也是它
（`--state-dir` 只是 runner 的参数名，值就是 mihomo home，不是项目自己的 `state/`）。
`providers/`、geo 数据、mihomo 自己的 cache 都在这个目录下，因此渲染出的 provider 绝对路径
（`support/providers/subscription.yaml`）位于 `-d` 之内——mihomo 会拒绝不在 home 或
`SAFE_PATHS` 内的 provider 路径。

`state/` 只放本项目管理用的状态文件（`subscription.env`、`deployment.json`、
`network-proxy-backup.json`、`config.tun.yaml`、`staging/`），**不是** mihomo 的 `-d`
目录。

统一调用 `/usr/local/bin/mihomo-proxy-management <command>`；脚本只是兼容入口：

| 操作 | 安装后的脚本 | Python 命令 |
|---|---|---|
| 安装 | `/usr/local/libexec/mihomo-proxy-management/scripts/install.sh`（源仓库内为 `macos/scripts/install.sh`） | `install [--no-start]` |
| 启动 | `/usr/local/libexec/mihomo-proxy-management/scripts/start.sh` | `start` |
| 停止 | `/usr/local/libexec/mihomo-proxy-management/scripts/stop.sh` | `stop` |
| 重启 | `/usr/local/libexec/mihomo-proxy-management/scripts/restart.sh` | `restart` |
| 状态 | `/usr/local/libexec/mihomo-proxy-management/scripts/status.sh` | `status` |
| 健康检查 | `/usr/local/libexec/mihomo-proxy-management/scripts/health-check.sh` | `health` |
| 切换 VPN | `/usr/local/bin/mpm-switch` | `switch {mihomo,nextin,status}` / `probe`（`switch rollback` 为内部兜底） |
| 卸载 | `/usr/local/libexec/mihomo-proxy-management/scripts/uninstall.sh` | `uninstall [--purge]` |

秘密只从文件或环境变量读取，**永不出现在命令行参数里**：

```sh
# 首次安装（root）：三个输入由调用者的 shell 环境提供，install 会把 URL/secret
# 写成 0600 的 subscription.env。仓库与本文档都不记录它们的值。
sudo --preserve-env=MPM_SUBSCRIPTION_URL,MPM_SECRET,MPM_MIHOMO_BINARY \
     /usr/local/bin/mihomo-proxy-management install
```

三个输入（订阅地址、controller 秘密、mihomo 二进制路径）先在你的 shell 里
`export`，或在 root shell 里 `read -s` 读入；不要写进脚本或命令行参数
（会落进 shell 历史）。

安装器**不联网下载**：mihomo 由 `MPM_MIHOMO_BINARY`（或 PATH 上的 `mihomo`）提供。
`GeoIP.dat`、`GeoSite.dat`、`Country.mmdb` 若与源二进制同目录，会以 `0644` 原子复制到
**mihomo home**（`support` 根，即 `-d` 目录，与 `mihomo -t`、runner 的 `-d` 一致）。
模板里 `geodata-mode: true`，因此 `GeoIP.dat` 与 `GeoSite.dat` 是必需的：

* 渲染出的配置固定 `geo-auto-update: false`，`mihomo -t` 不会尝试从 GitHub 下载
  geo 数据（真实 Mac 上曾因此超时、安装失败）；
* 安装会在调用 `mihomo -t` **之前**检查 mihomo home 中的这两个文件；缺失时直接以
  `FailClosed`/`RenderError` 失败，并提示把 `GeoIP.dat`/`GeoSite.dat` 放到
  `MPM_MIHOMO_BINARY` 同目录后重试。

单测 `test_macos_lifecycle.py::test_provider_path_plist_and_geodata_share_the_mihomo_home`
锁定这条不变量：渲染出的 provider 路径必须位于传给 `mihomo -t` 的 `-d` 目录之下，
plist 的 `--state-dir`/`WorkingDirectory` 与 geodata 目标目录都等于同一个目录。

root（`system profile`）安装时，安装器会对 `support`、`state`、`providers`、`logs`
四个目录以及 `subscription.env`、`deployment.json`、`network-proxy-backup.json`、
`config.yaml` 等写入后显式 `chown(0, 0)`。原因是 BSD 语义下新文件继承父目录属组，而
`/Library/Application Support` 属组为 `admin`，不修正就会写出 `root:admin` 的
`subscription.env`，与 `check_secret_input` 要求的 `root:wheel` 冲突。非 root（沙箱/测试）
运行时不执行 `chown`。

`/usr/local/bin/mihomo-proxy-management` 的默认解释器在安装时被渲染为**安装所用的
解释器**（`MPM_PYTHON` 或 `sys.executable`），不再默认 `/usr/bin/python3`（stock macOS
为 3.9 且无 PyYAML）。运行时仍可用 `MPM_PYTHON=/path/to/python3` 覆盖。

## 3. KeepAlive、崩溃拉起与“开机不自启”

plist 固定为：

```xml
<key>RunAtLoad</key><true/>
<key>KeepAlive</key>
<dict><key>SuccessfulExit</key><false/></dict>
<key>ThrottleInterval</key><integer>5</integer>
<key>ProcessType</key><string>Interactive</string>
```

按 `launchd.plist(5)`，`KeepAlive = {SuccessfulExit: false}` **隐含 RunAtLoad**：只把
`RunAtLoad` 改成 `false` 并不能阻止开机启动（2026-11-14 事故正是 `install --no-start`
留下了一个 `RunAtLoad=true` 的 LaunchDaemon，重启后 launchd 直接以 TUN 拉起 mihomo，
绕过 `start` 的所有闸门）。

因此本项目用 **持久化 launchd override** 控制开机行为：

| 操作 | launchd 行为 |
|---|---|
| `install`（含 `--no-start`） | core plist 写入后立即 `launchctl disable system/<core>`，并读回 `print-disabled` 确认；同时写入 boot-switch plist，在 0600 hold stamp（`state/boot-switch.hold`）保护下把作业登记进当前 launchd domain：stamp 不存在则 `O_EXCL` 创建，上次失败 install 遗留的 stamp **接管而不删除**（删了再建会打开"无 hold"窗口，仍在启动的旧入口会在窗口内执行整个切换），并删除可能遗留的旧 ack（只有 hold 提供保护，删 ack 不会放开切换）→ 若旧定义仍 loaded，先 `bootout` 并等 `launchctl print` 读回确认已卸载（期间 hold 一直在）→ `enable`（读回确认**未** disabled）→ `bootstrap` 新定义 → poll 等待**完整确认**：作业已 loaded、RunAtLoad 进程已退出、0600 ack 文件（`state/boot-switch.hold-ack`）已出现 → 结束前删除 stamp 与 ack。hold 使 RunAtLoad 拉起的 boot 入口写 ack 后零副作用立即退出，注册因此安全；只有 "loaded" 而无 ack 时（进程尚未跑完）绝不删 stamp。确认超时（`MPM_BOOT_ACK_DEADLINE_SECONDS`，默认 30s）则 fail closed：`bootout` boot-switch，**仅当 print 读回确认作业已卸载**（未 loaded / service not found）才删除 stamp/ack；bootout 失败、作业仍 reported loaded、或 print 无法读回时，stamp（与 ack）**保留**在磁盘上，仍在启动的入口会读到 hold 零副作用退出。其余任何失败同样保留 stamp：只有完整 ack 或确认卸载这两个已证实安全的时点才删除。`install` **从不** kickstart、也不执行切换（切换只发生在开机入口或随后的手动 kickstart） |
| `start` | 先 `enable`（否则 disabled 的服务无法 bootstrap）→ `bootstrap`/`kickstart` → 运行、健康、TUN 验证全部通过后**立即再次 `disable`** |
| 成功 start 之后 | 已加载的作业继续运行；本机本次开机内崩溃仍由 KeepAlive 拉起；**下次开机不会加载 core**（开机走 boot-switch） |
| `stop` | `bootout` + `disable`（只针对 core；boot-switch 保持 enabled） |
| `uninstall` | `stop`（已含 core disable）后再显式 `disable` core，然后 `bootout` + `disable` boot-switch 并删除两个 plist |

`disable` 只影响后续加载，不会杀掉正在运行的作业，所以“启动成功后立即 disable”既能
保留崩溃拉起，又能保证下次开机不会直接加载 core——重启后的接管只通过 boot-switch
的安全流程发生（先等登录用户与默认路由、Always On VPN（On Demand）预检、失败回滚到
Nextin）。`plist.audit` 不再强制
`RunAtLoad=true`（允许 `true`/`false`），但仍强制
`KeepAlive = {SuccessfulExit: false}`、`ThrottleInterval=5`、无 shell、无 `caffeinate`、
无秘密/URL。

runner 的退出码语义不变：

| 情况 | runner 退出码 | launchd 行为 |
|---|---|---|
| mihomo 正常退出 `0` | `0` | 不拉起 |
| mihomo 退出 `N != 0` | `N` | 5 秒后拉起 |
| mihomo 被信号 `S` 杀死 | `128 + S`（如 SIGKILL → 137） | 5 秒后拉起 |
| runner 自身被 `SIGKILL` | 非成功退出 | 拉起；core 锁由内核释放，新实例可获取 |
| 显式 `stop` | — | `launchctl bootout` + `disable`，**不会被 KeepAlive 拉起** |
| runner 启动前检测到外部 VPN/端口/路由冲突 | `0` | 视为成功退出，**不拉起**（避免开机循环） |
| runner 拿到 core 锁失败 | `75` | 非 0，按 `ThrottleInterval` 重试（不抢锁） |

因此：崩溃一定会被拉起；管理员主动停止不会被立即拉起。runner 收到 `SIGTERM`/`SIGINT`
时转发给 mihomo 并以 `0` 退出，配合 `bootout` 完成干净停止。

上述语义由这些单测锁定（改实现前先读它们）：

| 语义 | 单测 |
|---|---|
| `KeepAlive` 结构 = `{SuccessfulExit: false}`、`RunAtLoad`、`ThrottleInterval=5` | `test_macos_plist.py::test_plist_is_parseable_and_complete` |
| 拒绝“无条件重启”（`KeepAlive: true`）与 `caffeinate` 入口 | `test_macos_plist.py::test_keep_alive_audit_rejects_unconditional_restart`、`test_macos_plist.py::test_audit_rejects_caffeinate_in_program_arguments` |
| 正常退出 `0` → 退出码 `0`（launchd 不拉起） | `test_macos_runner.py::test_clean_exit_is_zero` |
| 非 0 退出码原样透传（launchd 拉起） | `test_macos_runner.py::test_non_zero_exit_is_preserved` |
| 被信号杀死 → `128 + signum`（launchd 拉起） | `test_macos_runner.py::test_signal_death_becomes_128_plus_signal` |
| `install --no-start` 结束处于 disabled | `test_macos_lifecycle.py::test_install_no_start_leaves_service_disabled` |
| `start`：enable → bootstrap → 成功后 disable | `test_macos_lifecycle.py::test_start_enables_bootstraps_then_disables` |
| `stop`：bootout → disable | `test_macos_lifecycle.py::test_stop_bootout_and_disable`、`test_macos_launchdctl.py::test_enable_and_disable_use_the_fixed_label` |
| 正常 `stop` 后不被拉起 | `test_macos_lifecycle.py::test_stop_restores_the_proxy_and_removes_the_snapshot`、`test_macos_launchdctl.py::test_bootout_targets_the_fixed_label` |
| 启动前冲突 → runner 退出 `0` 且不 spawn | `test_macos_runner.py::test_runner_exits_zero_without_spawn_when_conflict` |
| 崩溃后 core 锁由内核释放，新实例可重新获取 | `test_macos_runner.py::test_busy_core_lock_returns_tempfail` |

`SuccessfulExit` 的语义以 `launchd.plist(5)` 为准：键存在且为 `false` 时，
“进程以 0 退出”被视为成功而**不**重启；非 0 退出（包括 runner 自身被 `SIGKILL`）
被视为失败，`ThrottleInterval` 秒后重新拉起。

### 3.1 开机策略：core 仍 disabled，开机入口走与 `mpm-switch mihomo` 相同的安全流程

core（`com.mihomo-proxy-management.core`）**永远不在开机时启用**。它的 plist 有
`RunAtLoad=true` 与 `KeepAlive{SuccessfulExit:false}`，一旦 `enable`，launchd 会在开机时
直接拉起 mihomo runner，**绕过**冲突预检，和 Nextin 抢 7890/TUN——这正是 2026-11-14
事故的形态。`install`/`start`/`stop` 对 core 的 `disable` 行为保持不变。

开机默认走 mihomo 由**第二个** system LaunchDaemon 完成：
`com.mihomo-proxy-management.boot-switch`（`BOOT_LABEL`，与 core 的 `LABEL` 分开）：

```xml
<key>RunAtLoad</key><true/>
<!-- 没有 KeepAlive：只运行一次 -->
<key>ProcessType</key><string>Interactive</string>
```

* `ProgramArguments` 是安装后的 Python + `-m mpm_macos.boot`，**不含** secret、订阅 URL、
  token；标准输出/错误写到 `logs/boot-switch.{out,err}.log`；
* **没有 `KeepAlive`**（尤其没有 `SuccessfulExit:false`）：切换失败只记录失败并退出，
  不会每 5 秒重试一次把网络来回切；
* plist 走与 core 相同的 render+audit 防线：`plist.audit` 对 boot label 额外要求
  `RunAtLoad=true`、`KeepAlive` 缺失或为 `false`、入口必须是 `mpm_macos.boot`，并且仍然
  禁止 shell、`caffeinate`、URL 与任何 secret/token；
* `install` 写 plist 后，在 0600 hold stamp（`state/boot-switch.hold`）保护下把作业
  登记进当前 launchd domain：stamp 不存在则 `O_EXCL` 创建，上次失败 install 保留的
  stamp 直接接管、**不删除**（删除再重建会打开"无 hold"窗口，让仍在启动的旧入口执行
  整个切换，Codex review boot5），并删除可能遗留的旧 ack（只有 hold 提供保护）；
  若旧定义仍 loaded，先 `bootout` 并等 `launchctl print` 读回确认已卸载（期间 hold
  一直在），确认卸载后**再次删除 ack**——旧入口可能在本次 install 第一次删 ack 之后、
  被 bootout 之前（在 hold 保护下）写过 ack，该旧 ack 绝不能留在磁盘上满足新定义的
  等待（hold stamp 本身保留，Codex review boot6）→ `enable`（读回确认**未** disabled）
  → `bootstrap` 新定义 →
  poll 等待完整确认（作业 loaded + RunAtLoad 进程已退出 + 0600 ack 文件
  `state/boot-switch.hold-ack` 已出现）→ 结束前删除 stamp 与 ack。RunAtLoad 会在
  `bootstrap` 后拉起本入口，hold stamp 使它写 ack 后**零副作用立即退出**，因此
  `install --no-start` 也不会执行切换；只有 "loaded" 不算数——进程可能尚未跑到
  stamp 检查，此时删 stamp 会让这次运行执行整个切换（Codex review boot3）。确认
  超时（`MPM_BOOT_ACK_DEADLINE_SECONDS`，默认 30s，夹在 `[5, 300]`）则 fail closed：
  `bootout` boot-switch，且**仅当 print 读回确认作业已卸载**（未 loaded /
  service not found）才删除 stamp/ack；bootout 失败、作业仍 reported loaded、或
  print 无法读回时，stamp（与 ack）保留，仍在启动的入口会读到 hold 零副作用退出
  （Codex review boot4/boot5）。其余任何失败同样保留 stamp——只有完整 ack 或
  确认卸载后才删除。
  install 结束时作业处于“已 loaded、未运行”，随后
  `launchctl kickstart system/com.mihomo-proxy-management.boot-switch` 即可非重启验收。
  重新安装会先 bootout 旧定义、等读回确认卸载，再用新定义登记（不会继续运行旧定义）。
  `uninstall` 对它 `bootout` + `disable` 并删除 plist。

开机入口 `mpm_macos.boot` 的顺序（第 0 步与前三步全部只读/零副作用）：

0. **hold stamp**：`state/boot-switch.hold`（0600）存在时，先写 0600 ack 文件
   `state/boot-switch.hold-ack`（证明这次 RunAtLoad 已看到 hold），然后立即成功退出，
   什么都不做。这是 `install` 注册作业时对 RunAtLoad 拉起的保护（见上）；install 等
   到 ack 后把 stamp 与 ack 一并删除，正常开机与手动 kickstart 不会看到它们；
1. **等控制台登录用户**（默认 180 秒，`MPM_BOOT_CONSOLE_DEADLINE_SECONDS`，夹在
   `[10, 900]`）：`stat -f '%Su %u' /dev/console` 的所有者是 `root`/`_mbsetupuser` 时说明
   还在登录窗口。超时 → **什么都不做**（不启动 mihomo、也不启动 Nextin），报告
   `console user not ready`；
2. **等默认路由**（默认 180 秒，`MPM_BOOT_NETWORK_DEADLINE_SECONDS`，同样夹在 `[10, 900]`）：
   没有默认路由时启动 TUN 只会得到一个连不上订阅的 mihomo。超时同样零副作用退出；
3. 若 mihomo 已在运行且 Nextin 未连接 → **幂等成功**，不重启 core（避免无故断网）；
4. 否则以该控制台用户的 uid/user 调用与 `mpm-switch mihomo` **完全相同**的 root 阶段
   （`switch.switch_to_mihomo_root`，不复制实现）：Always On VPN（On Demand）预检
   （`TRUE` 或读不到 → 拒绝且零副作用）→ 以用户身份断开 Nextin → 等 Disconnected +
   原 PID 退出 + 端口释放 → 3–5 秒稳定窗口 → 冲突检测 → `start`（成功后仍 `disable` core）
   → 启动后复检 Nextin → deadline 健康检查 → 60 秒探测 → 任何失败回滚到 Nextin
   （只 `scutil --nc start`，**不改** Always On VPN）。

**Always On VPN（On Demand）必须保持关闭**：它是 `scutil` 的 `OnDemandEnabled : TRUE`。
该标志为 `TRUE`（或读不到）时，开机入口**拒绝**启动 mihomo（只报告原因），因为系统会在
`scutil --nc stop` 之后约 1.3 秒把 Nextin 连回来，mihomo 与 Nextin 会同时抢路由。
本项目在任何路径上都**不会**打开或修改它——不写 plist、不调用快捷指令、不做 UI 脚本。

全程不弹 `osascript` 密码框：LaunchDaemon 已经是 root，入口直接执行 root 阶段。

**真实重启未验**：本次改动只用 host-free 单测与手动触发入口（
`sudo /usr/local/bin/mihomo-proxy-management switch boot` 或
`sudo launchctl kickstart -k system/com.mihomo-proxy-management.boot-switch`）做非重启验收，
**没有真的重启过机器**。安装（含 `--no-start`）已把 boot-switch 登记进当前 launchd
domain（已 loaded、未运行），所以装完即可直接 `kickstart`，无需重启。重启后的行为
（登录窗口等待、默认路由等待、开机自动切换）需要在
维护窗口里由网管实际重启确认。

非重启验收（失败回退演练，探测被强制判失败）：

```sh
sudo MPM_PROBE_FORCE_FAIL=1 /usr/local/bin/mihomo-proxy-management switch boot
# 或
sudo /usr/local/bin/mihomo-proxy-management switch boot --drill-fail
```

预期：报告 FAILED，机器回到 Nextin 已连接、core 已 stop + disable 的状态。

### 3.2 开机预检与安装上下文：boot 与 CLI 共用同一个绝对二进制（2026-11-15 kickstart 修复）

**真实故障**：一次真正的 `launchctl kickstart system/com.mihomo-proxy-management.boot-switch`
失败了。LaunchDaemon 的环境只有最小 `PATH=/usr/bin:/bin:/usr/sbin:/sbin`，而安装版
mihomo 在 `Layout.binary`（`/usr/local/libexec/mihomo-proxy-management/current/mihomo`），
**不在 PATH 上**。当时 `boot` 用的 `switch.build_context` 直接拿 `collect_facts()` 的
PATH 结果当事实，没有像 CLI 那样把安装的 `layout.binary` 登记进去，于是预检误报
`required command not found: mihomo`。机器已安全回退 Nextin。

**修复**：新增共享模块 `mpm_macos/context.py`，CLI（`cli.make_context`）、`mpm-switch`
（`switch.build_context`）与开机入口（`boot.main`）**全部**走同一个
`context.build_context`，从此三者构造的 facts/上下文一致：

* `context.resolve_runtime_binary(layout)` 决定唯一的运行时绝对路径：
  **只承认安装版**——`layout.binary` 必须存在、是普通文件且可执行（`os.path.isfile` +
  `os.access(X_OK)`），否则返回空。**不再回退 `MPM_MIHOMO_BINARY`**：core runner
  永远执行 `layout.binary`，若允许 override 参与运行时解析，就会出现
  “预检/`mihomo -t` 验证 A、实际启动 B”的分叉。
* `context.register_binary` 把安装版绝对路径写进 `facts.commands["mihomo"]`；
  安装版不可用就**删除** mihomo 条目（包括 PATH 上的陈旧条目与安装来源 override），
  预检随即 fail closed。运行时**从不**查 PATH，也从不读 override——两者都只是安装来源候选。
* 安装来源单独解析（`context.resolve_install_source`）：`MPM_MIHOMO_BINARY` 优先，
  否则 `shutil.which("mihomo", path=ctx.environ["PATH"])`，且必须是绝对、普通、
  可执行文件（相对 override 会 `abspath` 成绝对）。`install` 的平台预检用
  `require_mihomo=False`——它自己负责装入二进制，不能在复制前要求“运行时 mihomo 已安装”，
  否则仅 PATH 来源的首次安装会被提前拒绝；其余能力检查（arm64/root/launchctl/networksetup）
  一项不放宽。无来源且无安装版时在写入任何文件前 fail closed；复制成功后重新
  `register_binary` 登记 `layout.binary`，再做配置验证、plist 安装与启动。
* 独立 `preflight`/`configure`/`start` 在安装版缺失/不可执行时**直接 fail closed**：
  `configure` 在读写任何文件前检查安装版，`start` 在预检后再核对一次 `layout.binary`
  （facts 是构造时快照，陈旧能力不能放行 bootstrap）。即便 override 指向有效的 A，
  也不会执行或验证 A，更不会 bootstrap core；修复损坏安装请重跑 `install`
  （从 override/PATH 重新复制并重新登记）。
* 为避免循环 import，`context` 模块级只 import `preflight`/`paths`，`lifecycle.Context`
  在 `build_context` 内部 import；`lifecycle`/`plist` 则在模块级 import `context`。

**验证与启动用同一个文件**：`lifecycle._default_validator` 在 `mihomo -t` 时**重新**调用
`resolve_runtime_binary`，而不是信任某个缓存路径——运行时解析只认 `layout.binary`，
override 完全不参与，验证不可能被导向别的文件。两个 plist（core 与 boot-switch）都在
`EnvironmentVariables` 注入 `MPM_MIHOMO_BINARY=__BINARY__`（与 runner 的 `--binary`
同一个占位符，**只是路径，不含 secret/订阅 URL**）。`plist.audit` 额外强制：
`MPM_MIHOMO_BINARY` 若存在必须是 POSIX 绝对路径；core 里它还必须等于 runner 的
`--binary` 参数。runner 原样执行 plist 的 `--binary`，于是预检、`mihomo -t`、常驻启动
三者锁定同一个绝对路径。**没有**靠扩展 PATH 来掩盖问题。

**fail closed 与回退不变**：未安装/不可执行/错误路径 → 预检 `Unsupported`（退出码 4）→
`switch_to_mihomo_root` 的 except → `_root_rollback`：core stop + disable、Nextin 重新连接。
原有“网络未就绪 / Nextin 残留 / 端口释放”等测试全部保留。

**真实重启未验**：本修复同样只用 host-free 单测覆盖（见第 7 节
`test_macos_context.py` 与 boot/plist 回归），**没有真的重启过机器**，也没有在真机执行
`launchctl`/`scutil`/`osascript`。最小 PATH 下 boot 与 CLI 事实一致、未安装即 fail closed
回退 Nextin，都由注入式测试证明；真实重启后的开机自动切换仍需网管在维护窗口确认。

## 4. 过滤表达式与生产顺序对照表

模板中所有组成员都来自订阅 `filter`，**不含任何真实节点名**。区域子组保证 fallback
的候选顺序与生产一致（fallback 取列表中第一个可用项）。

生产（2209 `C:\mihomo\config.yaml`，只读核对）与模板的对应关系：

* `CURSOR`：`fallback`，interval 180，lazy false，timeout 8000，候选
  **美国×2 → 日本×3，无 DIRECT**；模板拆成 `CURSOR-US` / `CURSOR-JP` 两个区域子组，
  `CURSOR` 再按此顺序引用它们。
* `CODEBUDDY`：`fallback`，interval 60，候选
  **DIRECT → 香港 → 台湾 → 新加坡 → 台湾 → 香港 → 台湾 → 日本**；模板按网管裁定归并为
  **DIRECT → HK → TW → SG → JP**（同一区域重复出现不改变“先 DIRECT、后港台新日”的语义）。

| 组 | 类型 | filter / 成员 | 对应生产候选 |
|---|---|---|---|
| `CURSOR-US` | fallback | `(?i)美国\|美國\|\bUS\b\|USA\|United States\|🇺🇸` | CURSOR 美国（生产 2 个） |
| `CURSOR-JP` | fallback | `(?i)日本\|\bJP\b\|Japan\|🇯🇵` | CURSOR 日本（生产 3 个） |
| `CURSOR` | fallback | `[CURSOR-US, CURSOR-JP]`，interval 180，lazy false，timeout 8000 | 美国×2 → 日本×3（无 DIRECT） |
| `CODEBUDDY-HK` | fallback | `(?i)香港\|\bHK\b\|Hong ?Kong\|🇭🇰` | CODEBUDDY 香港（生产出现 2 次） |
| `CODEBUDDY-TW` | fallback | `(?i)台湾\|台灣\|\bTW\b\|Taiwan\|🇹🇼` | CODEBUDDY 台湾（生产出现 3 次） |
| `CODEBUDDY-SG` | fallback | `(?i)新加坡\|\bSG\b\|Singapore\|🇸🇬` | CODEBUDDY 新加坡（生产出现 1 次） |
| `CODEBUDDY-JP` | fallback | `(?i)日本\|\bJP\b\|Japan\|🇯🇵` | CODEBUDDY 日本（生产最后一位） |
| `CODEBUDDY` | fallback | `[DIRECT, CODEBUDDY-HK, CODEBUDDY-TW, CODEBUDDY-SG, CODEBUDDY-JP]`，interval 60 | DIRECT → 香港 → 台湾 → 新加坡 → 日本 |
| `GROKBOT-US` / `GROKBOT-SG` / `GROKBOT-JP` | fallback，url `https://grok.com/cdn-cgi/trace`，expected-status 200，interval 300，timeout 8000，lazy false，max-failed-times 3 | 生产 GROKBOT 组的区域候选（严格 US/SG/JP 过滤，与 GPT 子组同一套常量） |
| `GROKBOT` | fallback | `[GROKBOT-US, GROKBOT-SG, GROKBOT-JP, AUTO-FOREIGN]`，同 url/interval | 生产 GROKBOT 组：x.ai/grok.com 专用。**末位必须是 AUTO-FOREIGN 而非 DIRECT**——x.ai 在大陆不能直连，三个区域池全空/全死时由任意存活节点兜底。曾用路由 `x.ai → US-FAST`：US-FAST 空池时 mihomo 把组静默降级为 COMPATIBLE（≈直连），这就是 grok bot 连不上的根因（2026-11-17） |

GROKBOT 组直接来自共享模板（与生产一致），macOS 渲染器不重写它，只由 `audit()` 锁定不变量
（成员顺序、url、expected-status、AUTO-FOREIGN 兜底）；`policy` 对 GROKBOT 区域零匹配只**警告**
（有 AUTO-FOREIGN 兜底，不 fatal）。`GLOBAL` 与 Windows 一样包含 GROKBOT 供手动切换。

### macOS 专用覆盖（模板不动）

以下覆盖在 `config.py` 渲染时应用，并有对应的 `audit` 不变量：

| 项目 | macOS 渲染结果 | 原因 |
|---|---|---|
| provider `interval` | `max(3600, minutes*60)` 秒；`health-check.interval=300`、`lazy=true`、保留 `path` | 模板里“分钟”被直接写进 mihomo 的“秒”字段，曾变成 60 秒刷新；`path` 让订阅失败时仍用本地缓存 |
| `US-FAST` | `type: fallback`，`filter` 严格 US，`url` gstatic 204，interval 180，timeout 5000，lazy false，max-failed-times 3 | 原 `url-test` + `美\|us\|usa` 会匹配 Russia/Australia，且卡死节点不切走 |
| `GPT-US` / `GPT-SG` / `GPT-JP` | 三个 fallback 子组，严格 US/SG/JP filter，`url` `https://chatgpt.com/cdn-cgi/trace`，expected-status 200，interval 300，timeout 8000，lazy false，max-failed-times 3 | 对齐 2209 生产：美国优先 → 新加坡 → 日本末位，稳定优先、少切换 |
| `GPT` | `fallback [GPT-US, GPT-SG, GPT-JP]`，同 URL/interval/max-failed-times | 单一入口，规则只引用它 |
| GPT 规则 | `GEOSITE,openai,GPT`、`DOMAIN-SUFFIX,chatgpt.com,GPT`、`DOMAIN-SUFFIX,openai.com,GPT`，位于任何 `US-FAST`/`PROXY` 规则之前 | 避免 ChatGPT 走错组 |
| `PROXY` | `select`，**无 `use`**，`proxies: [AUTO-FOREIGN, US-FAST, DIRECT]` | 原 select 列出所有原始节点，且 `store-selected` 会把旧选择（台湾死节点）带过重启 |
| `profile.store-selected` | `false` | 消除旧选择残留 |
| `AUTO-FOREIGN` | `url-test`，`url` generate_204，interval 300，tolerance 100 | 保持原语义 |
| `GLOBAL` | 加入 `GPT` | 手动切换时可见 |
| `dns.fake-ip-range`（仅 TUN） | `198.19.0.1/16` | 避开 Nextin 的 198.18.0.1/16；mixed 模式仍移除 fake-ip |

严格区域过滤常量与 `mpm_common.policy` 的 `CURSOR-US`/`CURSOR-JP`/`CODEBUDDY-SG`
完全一致（US：`(?i)美国|美國|\bUS\b|USA|United States|🇺🇸`；SG：
`(?i)新加坡|\bSG\b|Singapore|🇸🇬`；JP：`(?i)日本|\bJP\b|Japan|🇯🇵`）。

无匹配校验（`mpm_common.policy`，纯函数，输入节点名列表）：

* `CURSOR-US` + `CURSOR-JP` 合计 **0** 匹配 → **报错并拒绝安装**（fail closed）；
  CURSOR 没有 DIRECT 兜底，空池会静默断流。
* `CODEBUDDY-*` 某个区域 0 匹配 → **仅警告**；`DIRECT` 在首位，仍可用。
* macOS 的 `configure`/`install` 必须调用该检查；Windows `install.ps1` 行为不变。

**为什么必须在渲染时校验**：mihomo 中 `use` + `filter` 无匹配的组会退化为
`COMPATIBLE`（等同直连），不报错、也不告警。因此 macOS 渲染时由 policy 校验对
CURSOR 无匹配直接报错；Windows `install.ps1` 不做该校验（行为不变）。

### 接入 Nextin 本地节点

在仓库根目录运行以下命令，将已提取的节点文件安装到 macOS 运行目录并重新生成配置。

```sh
sudo install -o root -g wheel -m 600 providers/nextin.yaml \
  '/Library/Application Support/mihomo-proxy-management/providers/nextin.yaml'
sudo sh macos/scripts/install.sh --no-start
```

Nextin 文件用于补充源 1。原有 `subscription` 继续按订阅刷新，现有策略组也读取本地补充节点。飞书把两批节点合并到源 1 的测速池，默认每 600 秒运行一次。

### 与 Windows 共用的 `scripts/health-check-provider.py`

Windows 计划任务（`scripts/refresh-production.ps1`、`scripts/run-xigua-ippool.ps1`）
仍以**原路径** `scripts\health-check-provider.py` 调用，命令行参数、退出码与 stdout
格式都没有变（拆分只是把逻辑抽成可导入函数）。`tests/test_health_check_cli_compat.py`
用 main 版快照对新版做 `--help` 与 mock 运行的逐字段对照。

macOS 安装版也包含该脚本。用安装时的 Python 解释器运行以下命令，对候选池执行与
Windows 刷新任务一致的三目标检查，结果写入独立的健康池。

```sh
support='/Library/Application Support/mihomo-proxy-management'
libexec='/usr/local/libexec/mihomo-proxy-management'
sudo /usr/local/libexec/mpm-python-venv/bin/python3 "$libexec/scripts/health-check-provider.py" \
  --core "$libexec/current/mihomo" \
  --candidate "$support/providers/candidate.yaml" \
  --output "$support/providers/three-sources-all-targets.yaml" \
  --target https://www.gstatic.com/generate_204 \
  --target https://chatgpt.com/cdn-cgi/trace \
  --target https://muse.ai/ --minimum-matching 1
```

规则顺序（mihomo 命中第一条即停，**不要重排**）：

```text
1  DOMAIN,ark.cn-beijing.volces.com,DIRECT
   DOMAIN-SUFFIX,superboss.cc,DIRECT
   ...
   DOMAIN-SUFFIX,cursor.com|.sh|cursorapi.com|cursor-auth.com|cursorvm.com,CURSOR
   DOMAIN-SUFFIX,jinritemai.com,DIRECT      ← 必须在 GEOSITE,tiktok 之前
   GEOSITE,tiktok,VIETNAM
   ...
   DOMAIN-SUFFIX,codebuddy.ai,CODEBUDDY
```

## 5. 启动闸门、冲突检测与显式 mixed

TUN 判定链（缺一不可）：

1. Darwin + arm64 + root；
2. `mihomo -t` 通过；
3. **没有外部 VPN/路由冲突**（见下）；
4. LaunchDaemon running；
5. `127.0.0.1:9090/version` 可用（带 secret 认证）；
6. provider healthcheck **已触发**且至少一个节点存活；并且 `AUTO-FOREIGN` 与 `GPT` 两个组
   都有存活节点（`/proxies` 的 `alive`）。`GROKBOT` 不需要单独进健康门：它的最后一位
   fallback 成员是 `AUTO-FOREIGN`，`AUTO-FOREIGN` 存活即 GROKBOT 存活；多加一个名字只会
   多一种失败形态，不会多一分覆盖；
7. 注入式探针观察到**新增的 `utun*`** 或默认路由切到 `utun*`（仅“存在 utun”不算成功，
   其他 VPN 也会创建 utun）。

启动健康门（`lifecycle.start`）用的是带总截止时间的 `evaluate_with_deadline`
（`common/python/mpm_common/controller_health.py`），不再把 healthcheck 的超时误报成
“controller 不可达”：

* `/providers/proxies/<p>/healthcheck` 只带 **2 秒**超时**触发**；该端点要等所有节点测完
  才返回，所以超时/阻塞算“已触发”，不算“不可达”；
* 随后在总 deadline（默认 **45 秒**，`MPM_HEALTH_DEADLINE_SECONDS`，夹在 `[10, 300]`）内
  轮询 `/providers/proxies` 与 `/proxies`，只要该 provider `alive_count >= 1` 即通过；
* deadline 到期仍无存活节点 → `no alive node in provider <p> after <N>s (k nodes tested)`；
  存活状态未知 → `provider '<p>' liveness is unknown after <N>s ...`；
  `/version` 也失败 → `controller unreachable: ...`。三类错误信息互不混淆；
* provider 接口失败（例如持续 HTTP 500）**不等于** controller 不可达：每次轮询失败都会用
  剩余预算再请求一次 `/version`，只有 `/version` 复核也失败才报 `controller unreachable`；
  复核成功则报 `provider state unreadable: <错误>`（一次都没读到）或沿用最后一次读到的
  `no alive node ...`（并注明此后已读不到）。复核同样计入总 deadline，不会把预算撑大；
* `status` / `health` 仍用原来的 `evaluate()`（不做 45 秒轮询），Windows/Linux 行为不变；
* 轮询间隔与时钟可注入，测试不会真的等待。

### 外部 VPN / 冲突检测

`conflict.py` 只读地检查：

* `scutil --nc list`：任何 `(Connected)` 的 VPN（名称 + bundle，例如 Nextin）；
* `lsof -nP -iTCP:7890/9090 -sTCP:LISTEN -Fpc`：端口被**非本项目**进程占用。本项目身份
  由 launchd job 的 PID（`launchctl print system/<label>` 的 `pid`）或可执行文件路径
  `/usr/local/libexec/mihomo-proxy-management/current/mihomo` 判定（`ps -o comm=`），
  不看进程名；手工启动的同名 `mihomo` 算外部冲突；
* `route -n get default`：默认路由在 `utunN` 且不是 mihomo 自己的 utun；
* `ifconfig`：utun 的 inet 落在 198.18.0.0/15（Nextin 的 198.18.0.1 或未被 mihomo
  占用的地址）。

`lifecycle.start` 在 **bootstrap 之前**调用它；有冲突直接 `FailClosed`（退出码 2），
明确提示“先运行 `mpm-switch mihomo` 或断开 Nextin”，**不启动、也不回退 mixed 改系统代理**。
`runner.py` 在 spawn mihomo 前同样检测（覆盖 launchd 开机路径）：有冲突时打印原因并
**以 0 退出**（SuccessfulExit，launchd 不会循环拉起）；检测本身出错也按“有冲突”处理
（fail-safe）。`runner --skip-conflict-check` 仅供测试。

### 失败就是 FAILED，不再自动 mixed

以下任一环节失败：bootstrap/kickstart、`wait_running`、controller readiness、共享健康
检查、组存活检查、utun 验证、**启动成功后的 `disable` 或 `print-disabled` 读回** —— 统一执行：

1. `launchctl bootout` + `launchctl disable`（`disable` 在 `finally`，bootout 失败也照做；
   两步结果分别记录）；
2. 恢复 `network-proxy-backup.json` 快照（若有）并读回校验；
3. 返回 `status: FAILED`、退出码非 0。

`disable` 失败或读回失败都按启动失败处理，**不会**把“读不到状态”当成“已确认禁用”；
只有“已 disable 且已确认不运行”才允许把机器交回 Nextin。

**不会**自动切到 mixed，也**不会**设置系统代理。只有操作者显式执行
`configure --mode mixed` + `start --mode mixed` 才使用 mixed：

1. 枚举 network service，快照 Web/Secure/SOCKS 的 enabled/server/port、bypass 域与
   “是否为带认证代理”；原子写入 `0600` 的 `network-proxy-backup.json`；
2. 渲染 mixed：`tun.enable=false`、`mixed-port=7890`、DNS 改为 `redir-host`；
3. 启动 core，完成 readiness 与健康检查（含 `AUTO-FOREIGN`/`GPT` 组存活）；
4. **仅在健康后**才用 `networksetup` 指向 `127.0.0.1:7890`，读回校验；
5. 任何一步失败都走上面的 FAILED 事务，并把配置恢复为 TUN，避免下次 `start` 误用 mixed。

原代理若启用认证，`networksetup` 无法读取/恢复密码，**默认拒绝覆盖**并给出明确错误。

## 5.1 与 Nextin 切换

一条命令切换，双向都保证“最坏情况落到 Nextin 接管”：

```sh
mpm-switch mihomo   # 先弹 macOS 密码框 -> root 断开 Nextin -> 启动 mihomo -> 60 秒探测
mpm-switch nextin   # 停 mihomo（bootout + disable + 恢复系统代理）-> 连接 Nextin
mpm-switch status   # 谁在接管：Nextin 连接状态 / mihomo launchd 状态 / 默认路由 / 系统代理
```

### 前置条件：先在 Nextin App 里关闭 Always On VPN（On Demand）

2026-11-14 20:52 的回退事故根因之一：Nextin 的 NetworkExtension 开着
`OnDemandEnabled : TRUE`，`scutil --nc stop` 之后约 1.3 秒就被系统按 Always On VPN
（On Demand）规则重连，mihomo 与 Nextin 同时抢路由。

`mpm-switch mihomo` 现在会**先检查并拒绝**，不会“边切边抢”：

* 用户阶段在弹密码框之前、在任何 stop/osascript/代理改动之前，用当前用户会话执行
  `scutil --nc show <VPN>`，解析 `OnDemandEnabled : TRUE/FALSE`（大小写不敏感、空格宽松）；
* `TRUE` → 直接拒绝（退出码非 0），提示
  “请在 Nextin App 里关闭 Always On VPN（scutil 显示为 On Demand）后再切换 /
  disable Always On VPN (On Demand) in the Nextin app first”，
  并明确说明**没有停止 VPN、没有弹出密码框、没有修改代理**；
* 读不到（命令失败、字段缺失、两行互相矛盾）→ 同样拒绝（fail closed），并给出原因；
  **例外**：`scutil` 明确报 `No service`（VPN 服务根本不存在）且当前没有任何 VPN 连接时允许继续
  —— 不存在的东西不会在背后重连；
* `FALSE` → 继续。root 阶段在 `scutil --nc stop` 之前会再读一次（走
  `launchctl asuser` 的用户会话），`TRUE` 则不 stop、直接拒绝；这种拒绝以**专用退出码
  `5`** 结束并**不进入回滚**（没有 stop、没有 `launchctl disable`、没有 `scutil --nc start`），
  用户阶段识别该码后只报告拒绝，不再尝试恢复 Nextin。即使拒绝标记文件写入失败，包装
  shell 也凭退出码跳过 `switch rollback`。

本项目**绝不修改** Always On VPN（On Demand）：不写 plist、不调用快捷指令、不做 UI 脚本。
`mpm-switch nextin` 与所有回退路径仍然只用 `scutil --nc start <VPN>` 连接并轮询核对；
读到 `OnDemandEnabled` 只记入报告的 `on_demand` 字段（`enabled` / `disabled` / `unknown`），
不做任何修改。

### stop 完成条件：状态 Disconnected、隧道插件退出、端口释放

`scutil --nc stop` 返回 0 只说明“命令成功”，`scutil --nc list` 里 `(Connected)` 消失也只说明
隧道开始拆除。2026-11-15 12:09:48 的回退事故就是在这里抢跑：`(Connected)` 消失后约 0.3 秒
代码就做了冲突检测，而 Nextin 的 `ProxyTunnel`（pid 1380）仍监听 7890，直到 48.534 才真正
退出，于是被正确判为外部占用并 fail closed。

现在 root 阶段在 stop 之后、稳定窗口之前，用**同一个 deadline**（默认 15 秒，每 0.5 秒轮询）
等待以下条件**同时**成立：

* `scutil --nc status <VPN>`（失败时回退到 `scutil --nc list` 的状态字段）为 `Disconnected`；
  `Disconnecting` / `Stopping` 都算“还在拆除”，不继续；
* 7890 与 9090 上**没有任何非本项目的监听者**——原 NE 插件（`ProxyTunnel`）已退出；
  本项目自己的 mihomo 若已在运行（端口由 launchd PID 或固定可执行文件路径识别）不算残留，
  身份无法判定的监听者按“仍被占用”处理；
* **stop 之前**在 7890/9090 上记录下来的外部监听者 PID（`ProxyTunnel` 等）**都已不存在**：
  stop 前用同一套 `lsof` 结果取 PID，之后每次轮询用 `ps -p <pid>` 核对（非零退出或无输出
  即“已退出”）。只释放端口但进程还活着的插件属于同一类抢跑——端口空了、旧网络栈还在跑；
  读不到端口列表时**不会发 stop**（无法核对的 stop 不确认）；
* 以上在同一次轮询中同时成立。

状态读不到（命令失败）不算确认；deadline 到期仍被占用（端口被占或 PID 未退出）则
**fail closed**，报 `<VPN> did not finish disconnecting within 15s (...)`（明细里会写明
`7890: ProxyTunnel (pid N)` 或 `pid N still alive`），绝不忽略 `ProxyTunnel`、也不启动 mihomo。
期间 VPN 曾经断开又连回来 → 报
`<VPN> reconnected Ns after stop while waiting for the teardown to finish (Always On VPN / On Demand?)`
并按回退处理。

### 稳定断开窗口（stop 之后必须“安静”足够久）

拆除完成后，root 阶段继续观察：

* 默认 **4 秒**（`MPM_STABLE_DISCONNECT_SECONDS`，夹在 `[3, 5]` 秒），每 **≤0.5 秒**
  以用户会话读一次状态（`scutil --nc status`，失败时回退到 `scutil --nc list` 的状态字段）；
* 窗口内**每一次**都必须读到 `Disconnected`。只要求“不在 Connected 列表里”是不够的：
  `Disconnecting` / `Stopping` 说明旧会话还在拆除、随时可能连回来，状态读不到同样不算确认；
* 读到 `Connected` → 判定“Nextin 自动重连”，**不启动 mihomo**，直接回滚（以用户身份
  `scutil --nc start` 并核对已连接），错误信息写明
  `Nextin reconnected within Ns after stop (Always On VPN / On Demand?)`；
  读到其它状态 → 同样不启动，报
  `<VPN> was not continuously disconnected during the 4s window (state stopping after 0.5s)`；
* 窗口全程为 `Disconnected` 才继续：完整冲突检测 → `start` → 探测；
* mihomo 启动成功、**健康检查/探测之前**会再检查一次 Nextin（以及冲突检测里的 VPN 项）；
  已连接则立即回退，并写明 `reconnected after mihomo start (Always On VPN / On Demand?)`。

时钟与 sleeper 都可注入，测试不会真的等待。

### 连接 VPN 的状态机（回退、`nextin`、用户阶段恢复）

`scutil --nc start` 只是“请求”：上一次会话还处于 `Disconnecting`/`Stopping` 时，
nesessionmanager 会记 `Skip` 并丢弃它（2026-11-15 12:09:48 的第一次 start 就是这样被忽略的），
而“被丢弃”和“连接慢”看起来完全一样。因此 `_root_rollback`、`switch_to_nextin` 与用户阶段
恢复统一使用同一个状态机：

* 状态是 `Disconnecting`/`Stopping` 时**不发 start**，先等到 `Disconnected`；
* start 之后若 5 秒内状态仍未进入 `Connecting`/`Connected`，**重发 start**；
* 总 deadline 60 秒，期间可多次重发；只有读到 `Connected` 才算成功，否则报告
  `could not connect <VPN>; open it manually`。

时钟与 sleeper 同样可注入，测试不会真的等待 60 秒。

* `mpm-switch mihomo` **先要密码，再断网**：普通用户阶段只通过
  `osascript ... with administrator privileges` 弹密码框，不碰 Nextin。用户取消或弹窗
  失败时 Nextin 原样保持连接，命令直接返回非零。
* 同一个 `osascript` 进程同时覆盖**密码输入**和**整个 root 阶段**，因此它以
  `timeout=None` 启动：执行器**永不**杀授权进程，也就不可能留下孤儿 root。
  2026-11-15 12:57 的失败正是执行器默认的 60 秒把密码框杀掉，而它背后的 root
  子进程还在继续跑。用户随时可以在密码框点取消，`osascript` 返回非零，按普通失败
  路径处理（此时什么都没动）。root 阶段内部每条命令仍各有自己的有界 deadline，
  所以整跳不会卡死在某条外部命令上。不再维护 `PRIVILEGED_TIMEOUT_SECONDS` /
  `ROOT_PHASE_WORST_CASE_SECONDS` 这类静态总和，也不再读
  `MPM_PRIVILEGED_TIMEOUT_SECONDS`：静态总和无法跟上真实调用链（start、两次
  stop/rollback、代理快照恢复、失败报告读回……），算错一次就是杀进程 + 孤儿 root。
  测试断言 `run_privileged` 传 `timeout=None`，且其余执行器调用仍用默认 60 秒。
* 密码框里执行的是带兜底的 shell 包装（`<cli> switch mihomo … --marker <tmp>;
  rc=$?; [ "$rc" -eq 5 ] || [ -f <tmp> ] || <cli> switch rollback …; exit $rc`）。
  marker 只在探测全部通过后由 Python 写入，所以 root 阶段崩溃、被 `SIGKILL`、或返回非零时，
  `switch rollback`（幂等）会执行。退出码 `5` 表示“Always On VPN（On Demand）预检拒绝、
  什么都没改”，包装
  shell 因此不跑 rollback，即使 marker 写入失败也一样；用户阶段凭该码（或 marker、或输出中
  的 `refused before touching anything`）识别拒绝，不再执行自己的恢复逻辑。所有参数都经过
  `shlex.quote`，AppleScript 的反斜杠/双引号转义保留。
  **边界**：这条兜底只覆盖 *root Python 子进程* 的死亡（崩溃/`SIGKILL`/非零退出）。
  本工具不再给 `osascript` 设超时，因此不会由它自己杀掉授权进程；但若超时仍被报告
  （自定义 runner 返回 124，或执行器/外部因素抛 `TimeoutExpired`），root 子进程可能已经
  跑完（marker 甚至已写入），也可能还在停止 Nextin、等稳定窗口、或马上 `bootstrap` mihomo。
  只读检查（`core_may_be_running`、`launchctl print`、Nextin 状态）
  都只是**快照**，无法证明 root 阶段已经结束，所以超时后**一律 fail closed**：用户阶段
  不执行任何写操作（没有 `scutil --nc start/stop`、没有 `launchctl`、没有 `networksetup`），
  只报告“特权阶段超时，状态未知”加上只读状态，退出码非零，并提示运行
  `mpm-switch nextin` 恢复 Nextin（该命令自己走密码框做完整回退）。绝不在“看起来没运行”的
  快照上启动 Nextin——那正是要避免的双栈竞态。
* root 阶段第一步以**调用用户身份**断开 Nextin：
  `launchctl asuser <uid> sudo -u <user> scutil --nc stop <VPN>`（默认 `Nextin`，
  可用 `MPM_VPN_NAME` 覆盖；**不删除** VPN，也不改它的自动连接配置）。root 进程
  看不到用户的 NetworkExtension 会话，所以命令与**状态轮询**都必须走 `asuser`
  （`launchctl asuser <uid> sudo -u <user> scutil --nc list`）；root 自己的
  `scutil --nc list` 看不到用户会话，不能作为断开/连接的判据。断开失败、或 15 秒内没有
  完成拆除（仍是 `Disconnecting`/`Stopping`、7890/9090 仍被占用、或 stop 前记录的外部
  监听者 PID 仍存活）则不启动 mihomo。
* 之后：完整冲突检测 → `start` → 探测。**任何**失败（含冲突检测本身抛异常）都统一收敛到
  回滚：`bootout` + `disable`（`disable` 放在 `finally`，即使 bootout 失败也执行）+
  disable 读回（`launchctl print-disabled` 必须确认 override 生效）+ 确认不再运行 +
  孤立 core 检查（端口 7890/9090 无监听、无 198.19/16 utun；检查失败按"可能仍在运行"
  处理）+ 恢复系统代理快照 + 以用户身份 `scutil --nc start <VPN>`（状态机：等
  `Disconnected`、5 秒未进入 `Connecting`/`Connected` 就重发、60 秒总 deadline）+ 等待
  `(Connected)`。
  只有结构化 StopOutcome 的 `safe` 为真（disable 成功且读回为真、已停止、无孤立 core）
  才会连接 Nextin；任何一项未知或失败都会打印明确提示，要求运行 `mpm-switch nextin`
  或手动处理，绝不制造双栈。
* 探测守护（`probe.py`）使用 `time.monotonic()` 的**绝对 60 秒 deadline**：每个 curl 的
  `-m` 取 `min(8, 剩余时间)` 的**浮点值**（`--max-time` 支持小数，不向上取整、不强制
  最少 1 秒），剩余不足 0.5 秒时不再发起新请求，到点立即结束（不会因 4 个目标 × 13 轮
  拖到几分钟，单次请求也不可能越过 deadline）。出口查询（Cloudflare trace 的 `loc`）
  同样受该 deadline 约束（最多 3 秒且不超过剩余时间）。
  `baidu.com` 必须 200/3xx；`https://www.google.com/generate_204` 必须 204；
  `https://chatgpt.com/cdn-cgi/trace` 必须 200；`https://cursor.com` 任意状态 != `000`。
  国内项失败，或国外三项全部失败 → 回滚并打印“已自动切回 Nextin”。成功则打印每项结果与
  出口（`https://www.cloudflare.com/cdn-cgi/trace` 的 `loc`）。
* fake-ip 判据不再看默认路由：对 curl 的 `remote_ip` 执行 `route -n get <ip>`，接口必须
  是**本次启动前后新增的 mihomo utun**（接口差集）。macOS 的 TUN 可能通过更具体的路由
  接管流量，而 `route get default` 仍显示物理网卡。
* 弹窗失败后普通用户阶段会检查机器状态：Nextin 未连接时提示运行 `mpm-switch nextin`；
  只有在确认 mihomo 不在运行（端口 7890/9090 无监听、无 198.19/16 utun）时才以用户身份
  `scutil --nc start`，避免双栈。
* 进程身份判定不靠进程名：端口属于本项目当且仅当占用者是 launchd job 的 PID
  （`launchctl print system/<label>` 的 `pid`）或可执行文件是
  `/usr/local/libexec/mihomo-proxy-management/current/mihomo`。手工启动的同名
  `mihomo` 仍算外部冲突。
* `mpm-switch nextin`：弹密码框执行 `mihomo-proxy-management stop`（root 阶段则直接
  调用 `stop`），stop 必须返回 `safe` 的 StopOutcome——disable 成功且
  `launchctl print-disabled` 读回为真、job 已停止、端口 7890/9090 无监听、无
  198.19/16 utun——然后才以用户身份 `scutil --nc start <VPN>`（走上面的连接状态机），
  等待 `(Connected)` 后再做一次简单探测并报告。stop 未确认（含 disable 读回失败或孤立
  core）时不会连接 Nextin，并提示先处理孤立 core 再运行 `mpm-switch nextin` 或手动处理。
  `mpm-switch nextin` 与 `mpm-switch status` 都是幂等的：重复运行会重新验证并修复
  上一次切换留下的中间状态（如 wrapper 兜底未执行的残留）。
* **开机默认走 mihomo，失败回退 Nextin**：core 在 `install` 结束、`start` 成功后、
  `stop` 之后都处于 `launchctl disable system/<core>`，**开机不会加载 core**；开机由
  `com.mihomo-proxy-management.boot-switch` 这个一次性入口接管（见 3.1）。它等到控制台
  登录用户与默认路由就绪后，走与 `mpm-switch mihomo` 相同的安全流程；失败则回退到 Nextin。
  需要手动切换时仍用 `mpm-switch mihomo` / `mpm-switch nextin`。
* 演练回滚（不会真的断网，因为探测被强制判失败）：

```sh
MPM_PROBE_FORCE_FAIL=1 mpm-switch mihomo
# 或
mpm-switch mihomo --drill-fail
```

## 6. 落地步骤清单

每一步都标注**是否需要用户点头**与**是否需要管理员权限**（macOS 上即 root /
`sudo`）；`caffeinate -i` 只是可选项。

| 步骤 | 是否需要用户点头 | 是否需要管理员权限 |
|---|---:|---:|
| 审阅代码、运行 host-free 单元测试 | 否 | 否 |
| 补齐模板/测试（本分支） | 否（已由网管裁定授权） | 否 |
| 准备 mihomo 二进制与订阅地址、controller 秘密 | 是 | 否 |
| 安装程序到 `/usr/local`（`/usr/local/bin`、`/usr/local/libexec`、`/usr/local/share`） | 是 | 是 |
| 创建 `/Library/Application Support/...` 状态目录 | 是 | 是 |
| 写入 `state/subscription.env`（0600） | 是 | 是 |
| 安装 `/Library/LaunchDaemons/...core.plist` | 是 | 是 |
| `launchctl disable system/<core>`（安装结束即执行，core 开机不自启） | 是 | 是 |
| 安装 `/Library/LaunchDaemons/...boot-switch.plist` 并 `enable`（开机入口，一次性运行） | 是 | 是 |
| 安装 `/Library/LaunchDaemons/...feishu-sync.plist` 并 `enable`+`bootstrap`（飞书测速同步，install 内自动完成） | 是（执行 install 即确认） | 是 |
| 提供 FEISHU_* 凭据（install 时 `MPM_FEISHU_*` env，或从 Windows `~/.hermes/.env` 抄值后补进 0600 文件重跑 install） | 是 | 是 |
| 非重启验收：`switch boot` / `launchctl kickstart -k system/<boot-switch>`（含 `--drill-fail`） | 是 | 是 |
| 真实重启验收（登录后自动走 mihomo、失败回退 Nextin）——**本次未做** | 是 | 是 |
| `mpm-switch mihomo`（弹密码框，自动断开 Nextin、启动并探测） | 是（执行命令即确认） | 是（osascript 弹窗） |
| `mpm-switch nextin`（停 mihomo 并连接 Nextin） | 是（执行命令即确认） | 是（osascript 弹窗） |
| 首次启动并验证 TUN/utun | 是 | 是 |
| 显式 `configure --mode mixed` + `start --mode mixed` 时保存系统代理原值 | 是 | 服务自身为 root |
| 显式 mixed 时把系统代理切到 `127.0.0.1:7890` | 是（必须显式指定 mixed） | 通常需要 |
| 停止服务并恢复系统代理 | 用户请求 stop/uninstall 即视为确认 | 通常需要 |
| 删除 LaunchDaemon | 是 | 是 |
| 删除配置/provider/日志/快照 | 是（`uninstall` 保留，`--purge` 删除） | 是 |
| `caffeinate -i` 防空闲休眠（**可选项**） | 是 | 启动服务时已有 root |

`caffeinate` 不进入常驻服务；只有手动前台运行时可用
`python3 -m mpm_macos.runner --caffeinate ...`，且只允许 `caffeinate -i`（不改电源设置、
不调用 `pmset`）。

合并本分支后**不更新 2209 生产工作区**：`C:\mihomo\config.yaml`、`subscription.env` 与
计划任务都保持原样，等网管单独安排上线窗口。

### Windows 2209 生产将来更新到本版本前

本 PR 不改动 2209；真正在 2209 上线本版本时，必须先做下面这一步，否则飞书写入路径
会以 `app_token_missing` 非 0 退出：

1. 在 `C:\Users\Administrator\.hermes\.env` 中**增加一个键**（只写键名，不写值）：
   `FEISHU_BITABLE_APP_TOKEN`；
2. 值的来源由网管单独交付，仓库、README 与提交历史中都不出现该值；
3. 建议同时在飞书侧**轮换该 app token**：旧值仍留在公开仓库的 git 历史中，
   仅从当前工作树移除并不等于作废。

飞书脚本的取值顺序是 `--app-token` > 进程环境变量 > env 文件，错误信息不会打印值。

## 7. 飞书测速同步（feishu-sync LaunchDaemon）与分组诊断

Windows 用两个计划任务同步飞书多维表：`MihomoFeishuBitableSync`（每 30 分钟全量，表1/2/3 +
`proxy-auto-switch.py`）与 `MihomoNinjaGrokFeishuSync`（每 10 分钟 `--ninja-only` 写表2，
依赖 Windows 版 Ninja Desktop）。macOS 没有任务计划器，也没有 Ninja Desktop，所以新增了
第三个 system LaunchDaemon：

```text
com.mihomo-proxy-management.feishu-sync
  ProgramArguments = <安装解释器> -m mpm_macos.feishu_sync
  StartInterval    = MPM_FEISHU_SYNC_INTERVAL_SECONDS（默认 600 秒，夹在 [300, 86400]）
  ProcessType      = Background；无 RunAtLoad、无 KeepAlive（故意省略）
```

* **做什么**：每个周期在进程内按路径加载**安装版**
  `libexec/scripts/sync-feishu-bitable.py`（与 `convert_sub.py` 同目录，靠它的
  `sys.path` 导入成立；`install` 负责复制），运行完整同步，更新
  **表1 订阅源状态 / 表2 可测速节点 / 表3 GPT可用地区组**。
* **测速不碰生产内核**：脚本自建临时 mihomo 内核（mixed-port 17890、controller 19090、
  `tun.enable: false`），逐节点对 grok.com / chatgpt.com / api.openai.com 测延迟并经
  节点出口查询实际地区；生产 7890/9090 只用于订阅抓取兜底与只读回退。
* **凭据**：三个键 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_BITABLE_APP_TOKEN`
  与核心秘密同住 0600 的 `state/subscription.env`（install 接受 `MPM_FEISHU_*` 环境变量，
  也可从 Windows 机器的 `~/.hermes/.env` 抄值后手工补进文件再重跑 install）。
  `save_secrets` 读-改-写：重跑 install **不会丢**已有的 FEISHU_* 值；凭据值永远不出现在
  命令行参数里。**凭据缺失不阻塞安装**：周期任务报告缺哪些键（退出码 3）并在下个周期重试。
* **派生键 `SUBSCRIPTION_URL_1`**：飞书脚本的订阅源约定是 `SUBSCRIPTION_URL_1..4`；
  install 每次写秘密文件时用同一个 `SUBSCRIPTION_URL` 派生 `_1` 镜像，永远不会失同步。
  只配一个源即可运行（其余源记 `url_missing`，不致命）。
* **增加第二个订阅源（可选）**：例如 Nextin App 里订的 wmsxwd（wmsxwd-3.men）机场。
  Nextin 是沙盒 App，终端读不到它的容器（TCC），订阅链接请从 wmsxwd 仪表盘复制后
  **自己**写进 0600 秘密文件（值不经过对话/git，install 重跑也不会丢）：

  ```sh
  sudoedit "/Library/Application Support/mihomo-proxy-management/state/subscription.env"
  # 加一行（订阅链接是凭证）：
  #   SUBSCRIPTION_URL_2=https://.../subscribe?token=...
  # 可选：SUBSCRIPTION_UA_2=ClashMeta/1.18.0
  # 立即抓取三个源、合并候选池（0600，只作评估，绝不替换生产 provider）：
  sudo /usr/local/bin/mihomo-proxy-management update-sources
  ```

  `update-sources` 复用安装版 `convert_sub.py` 的三源候选机制（0 全部成功 / 5 部分成功
  但已写出候选 / 1 空池）；加源后飞书测速同步在下一个周期自动把来源 2 的节点一起
  测速并写进表1/表2。`groups` 诊断在加源后重新跑一次 install/configure 才能看到新节点
  进组（provider 由 mihomo 按间隔自动刷新）。
* **失败语义**：无 RunAtLoad，bootstrap 不会触发首轮（install 后约一个周期才首次同步）；
  无 KeepAlive，失败的周期靠下一个 interval 重试，不会风暴；launchd 对运行中的作业自动
  跳过后续 tick，慢测速不会自我重叠。缺安装版 mihomo / 缺脚本 → 退出码 4（fail closed，
  绝不用替代二进制测速）。plist 审计强制：入口必须是 `mpm_macos.feishu_sync`、
  `StartInterval ≥ 300` 的整数、禁止 RunAtLoad/KeepAlive/shell/秘密/URL。
* **验收**：

```sh
# 手动跑一个周期（或干跑检查前置条件与 argv，不测速不写表）
sudo launchctl kickstart system/com.mihomo-proxy-management.feishu-sync
sudo env PYTHONPATH=/usr/local/share/mihomo-proxy-management/python \
     /usr/local/bin/python3 -m mpm_macos.feishu_sync --dry-run
# 输出与运行结果
tail /Library/Application\ Support/mihomo-proxy-management/logs/feishu-sync.out.log
tail /Library/Application\ Support/mihomo-proxy-management/logs/feishu-bitable-sync.log
```

### `groups`：分组匹配/存活诊断

回答「为什么出口总是香港 / 其他节点为什么不可用」：

```sh
sudo /usr/local/bin/mihomo-proxy-management groups [--json]
```

只读输出每个渲染组：类型、filter、**匹配节点数**（对 provider 节点名逐组计数）、控制器
侧存活成员与当前选中项（`now`）。**匹配数为 0 的组会标出
`no node matches: mihomo degrades this group to COMPATIBLE (direct)`**——空池组静默
降级成直连正是 grok/US-FAST 类问题的来源；区域名对不上订阅命名时一眼可见。
控制器不可达时只降级为匹配计数（状态 OK，不报错）。

### 节点自动分配流水线（node-pipeline LaunchDaemon，上游 run-node-pipeline 移植）

上游 `feat/node-split-refactor` 把"按节点自动分配到本机"拆成 node_provider / node_ops
两个模块 + GPT 统一稳定切换入口（数据只存飞书两表：节点测速总表、有效节点）。macOS 以
第四个 system LaunchDaemon `com.mihomo-proxy-management.node-pipeline` 移植整条链：

```text
每 MPM_NODE_PIPELINE_INTERVAL_SECONDS（默认 3600s，夹 [600, 86400]）一个 tick：
 1 python -m node_provider --collect   订阅源 -> 飞书「节点测速总表」（失败容忍，
                                      下一步仍对既有表刷新测量）
 2 python -m node_ops                 源无关隔离内核测速（空闲端口 17890 起、TUN off、
                                      不碰生产端口/组）+ 地区分类 + GPT 连续轮数门控
                                      -> 「有效节点」表 + 测量列
 3 python -m node_provider --output   有效节点表 -> providers/node-valid.yaml
                                      （mihomo home 内的 file provider，原子写，
                                      低于 min-nodes 保留上一版）
 4 apply                              有效池首次出现或消失时重渲染配置并热重载 core，
                                      池内容变化时刷新 file provider
 5 switch_gpt                         在刷新后读取 GPT 成员。按真实出口美国 → 新加坡 → 日本
                                      逐级选择，同级取 trace 延迟最低的合格节点
```

**有效节点分配模式（valid_nodes）**：渲染器检测 `providers/node-valid.yaml` 是否存在——
存在时（第 4 步翻转后）`GPT` 使用 **select 组**（`use: [node-valid]`，成员是实测过关的
节点和手动兜底 `AUTO-FOREIGN`），`US-FAST` 读取同一 Provider 并保留严格美国过滤。
文件不存在时回到订阅模式。mihomo
拒绝文件缺失的 file provider，所以渲染必须由存在性门控；audit 在两种模式下各自锁定
组形。`configure`/`install` 自动带 `valid_nodes=os.path.isfile(node-valid.yaml)`。

要点：

* 凭据：与飞书测速同步同一套 `FEISHU_APP_ID/SECRET`（0600 秘密文件）；缺失时 tick 报告
  缺键并退出 4，下个周期重试——**不阻塞 install**，也不影响核心服务。
* GPT 切换：先用控制器的组级测速缩小候选范围，再通过隔离内核确认出口
  `loc=US/SG/JP` 和 ChatGPT 主页 HTTP 200（浏览器 UA，排除 Cloudflare 挑战页）。
  试探期间保持现有选路，提交后读回确认。没有候选通过，或 Provider 刷新失败时，
  保留控制器当前选择。每轮按策略重选，人工选择可能被下一轮覆盖。
* plist 走 interval 型审计（无 RunAtLoad/KeepAlive、StartInterval>=600 整数、入口必须是
  `mpm_macos.node_pipeline`、ProcessType Background）；install 注册（enable 读回 +
  bootstrap，重装先 bootout）、uninstall 摘除、status 显示 `node_pipeline` 状态。
* 安装面：`install` 额外复制 `node_provider/`、`node_ops/` 包与
  `proxy-auto-switch.py`、`gpt_switch_entry.py`、`gpt_stability_policy.py`、
  `named_mutex_lock.py` 到 `libexec/scripts/`；状态在 `state/node/`（锁、切换状态、
  轮记录）；日志 `logs/node-pipeline.log` / `node_ops.log` / `proxy-auto-switch.log`。
* **新节点投递点**：`providers/supplements/*.yaml`（0700）——无论从什么渠道拿到新节点
  （浏览器下载的订阅文件、App 导出、人工整理），转成 provider YAML 后丢进该目录，
  下一个流水线 tick 自动全部并入飞书「节点测速总表」源 1（语义去重：抓到的优先，
  本地补空隙；文件保留不删，靠按源替换保持新鲜）。转换命令：

  ```sh
  python3 scripts/convert_sub.py -i 下载的订阅文件.txt \
      -o "/Library/Application Support/mihomo-proxy-management/providers/supplements/wmsxwd-$(date +%Y%m%d).yaml"
  ```

  `install` 会创建该目录；部署在 `state/node/` 之外的投递不受用户/属主限制（root 写入即可）。
* 源 4（西瓜加速 App）通道与上游流水线 argv 保持一致（`--from-app source4`）：macOS 上
  无 DPAPI/ShuttleClient，通道按 `profile_missing` 优雅记失败并继续（每 app 通道独立容错，
  永不断轮）。
* 抓取不着急：源抓取（第 1 步）失败只记日志不阻断测量/分配；wmsxwd 源 403 期间流水线
  照常对表内既有节点工作。
* 验收：

```sh
sudo env PYTHONPATH=/usr/local/share/mihomo-proxy-management/python \
     /usr/local/bin/python3 -m mpm_macos.node_pipeline --dry-run   # 前置检查+argv
sudo launchctl kickstart system/com.mihomo-proxy-management.node-pipeline
tail "/Library/Application Support/mihomo-proxy-management/logs/node-pipeline.log"
sudo /usr/local/bin/mihomo-proxy-management groups    # 查看 GPT 当前节点
```

## 8. 测试

```sh
# macOS 适配层单独跑
~/work/.venv-mpm/bin/python -m pytest macos/tests -q -p no:cacheprovider
# 全仓（macos/tests + linux/tests + tests/）一起收集，模块名不得冲突
~/work/.venv-mpm/bin/python -m pytest -q -p no:cacheprovider
```

* `macos/tests` 全部 host-free：subprocess/launchctl/networksetup/scutil/lsof/curl/
  fcntl/controller 均为注入，可在 Windows、Linux、macOS 上运行（含 Windows 2209 /
  Python 3.11.9）；
* 冲突检测与切换的测试不读取真实主机：`test_macos_conflict.py`（scutil/lsof/route/
  ifconfig 解析、`OnDemandEnabled` 与 `Connected`/`Disconnecting` 状态解析、launchd
  PID/可执行文件身份判定与 fail-safe）、
  `test_macos_probe.py`
  （HTTP 状态、fake-ip 经 `route -n get` + utun 差集、60 秒单调时钟 deadline、演练开关）、
  `test_macos_switch.py`（先密码后断网、取消不动 Nextin、异常恢复、shell 包装兜底回滚、
  `osascript` 传 `timeout=None`（永不杀授权进程）而其余命令保留执行器默认 60 秒、
  特权阶段被报告超时后一律 fail closed（零写操作、只报告只读状态、提示 `mpm-switch nextin`）、
  Always On VPN（On Demand）拒绝与放行及其提示文案、stop 完成条件（Connected 消失 →
  Disconnecting → 端口稍后释放 → 原 ProxyTunnel PID 退出）、端口始终不释放或 PID 始终不退出
  则 fail closed、稳定断开窗口（每次轮询都必须是 Disconnected）、启动后重连
  回退、首次 start 被忽略后重发、双向切换、回滚恢复代理快照）；
* `test_macos_context.py`（2026-11-15 kickstart 回归 + 运行时分叉回归）：LaunchDaemon 最小 PATH
  （`/usr/bin:/bin:/usr/sbin:/sbin`）+ 已安装可执行文件时，CLI / `mpm-switch` / boot
  三个入口的 facts/上下文完全一致且预检通过；运行时只认安装版，override/PATH 只是安装来源；
  未安装/不可执行/目录占位/无效 override 一律 fail closed（删除 mihomo 条目）；
  override（含相对路径）只作为安装来源解析为绝对路径；`configure` 验证的是安装版而非 override，
  安装前独立 configure 直接 fail closed（不再验证安装来源）；
  **B 缺失/不可执行、A 有效且 plist 已存在**时 `preflight`/`start`/`configure` 全部 fail closed：
  不执行/不验证 A、不 bootstrap core（陈旧 facts 仍登记 A 时 start 也拒绝）；
  `install` 能从 override 重新复制修复缺失的安装版；安装后的两个 plist 都注入绝对安装路径
  且 core 的环境变量等于 runner 的 `--binary`；
* `test_macos_boot.py`（开机入口，全部注入 clock/sleeper/executor，不真实 sleep、不调真机
  `launchctl`）：控制台用户未就绪超时零副作用、默认路由一直不就绪不启动任何东西、网络先失败
  后就绪仍继续切换、Nextin 残留/端口延迟释放时先等 teardown 再 start、探测/健康失败回退
  Nextin（core stop + disable）、`OnDemandEnabled : TRUE` 拒绝且不 start、已在 mihomo 且
  Nextin 断开时幂等成功（不 bootout/bounce）、`install --no-start` 下 core disabled 且
  boot-switch enabled（读回）且不 kickstart、`start` 成功后 core 仍 disabled、
  `uninstall` 处理 boot-switch、boot plist 审计（无 forbidden token、无
  `SuccessfulExit:false` 的 `KeepAlive`、入口必须是 `mpm_macos.boot`）；
  **kickstart 回归**：最小 PATH + 安装版可执行文件时 boot 事实登记安装绝对路径、
  预检通过并跑完整切换（不再误报 `required command not found: mihomo`）；无可用二进制时
  fail closed（退出码 4）并回退 Nextin（core 不 bootstrap、保持 disabled、Nextin 已连接）；
  boot plist 注入绝对安装路径、审计拒绝相对 `MPM_MIHOMO_BINARY`；
* `test_macos_feishu_sync.py`（飞书测速同步守护，全部 host-free）：interval 缺省/夹取、
  argv 全部落在布局内且不含秘密值、前置检查三级（缺安装版二进制 / 缺已装脚本 /
  缺 FEISHU_* 凭据，各自独立退出码与提示文案）、注入 loader 后 `run_sync` 的退出码透传
  与失败报告、`--dry-run` 不加载不执行、feishu-sync plist 渲染+审计（拒绝
  `RunAtLoad`/`KeepAlive`/小于 300 的 `StartInterval`/错入口/shell）、install 挂载
  （复制 `sync-feishu-bitable.py`、写 plist、enable 读回、bootstrap、loaded-not-running）
  与 uninstall 摘除、重跑 install 幂等且不丢凭据、`save_secrets` 保留 FEISHU_* 并派生
  `SUBSCRIPTION_URL_1`、`load_secrets` 的 `MPM_FEISHU_*` 覆盖顺序；
* `test_macos_groups.py`（`groups` 只读诊断）：区域匹配计数、空池的
  COMPATIBLE 警告、控制器不可达时降级为纯匹配计数（状态仍 OK）、`now` 选中项、
  渲染配置损坏 fail closed；
* 健康门的总 deadline 单独测在 `tests/test_controller_health_deadline.py`
  （healthcheck 超时算“已触发”、轮询到 alive 即通过、deadline 到期无 alive 的准确信息、
  controller 真不可达、存活未知、provider 不可见、`/providers/proxies` 正常但 `/proxies`
  持续 500 时区分“provider state unreadable”与“controller unreachable”、`evaluate()`
  行为不变、短超时 2 秒），全部注入 sleeper/clock；
* plist 审计按**目标平台**判定绝对路径（`posixpath`），测试用 sandbox root 在渲染
  plist 前映射为 POSIX 目标路径（`Layout.to_target_path`），因此 Windows 宿主不会
  把 `C:\...` 写进 LaunchDaemon；`test_macos_plist.py` 还覆盖 core plist 的
  `MPM_MIHOMO_BINARY` 必须等于 runner 的 `--binary`、审计拒绝相对路径或与 `--binary`
  不一致的环境变量；
* `linux/tests` 在 Windows 上整体 skip（`linux/tests/conftest.py` 的
  `pytest_collection_modifyitems`，reason 写明 Linux/POSIX-only）：这些是 Linux 宿主
  测试，在 Windows 上运行没有意义，属于平台不适用而非真实缺陷；Linux/macOS 上的行为不变；
* 测试模块统一带 `test_macos_` 前缀（`test_macos_config.py`、`test_macos_lifecycle.py`…），
  共享 fake 在 `macos_test_support.py`；这样 pytest 默认的 prepend 导入模式不会把
  `tests/`、`linux/tests/`、`macos/tests/` 的同名模块混在一起；
* `tests/test_health_check_cli_compat.py` 的“main 版对照”用例需要
  `MPM_HC_MAIN_SNAPSHOT` 指向 `git show 34b3b0e:scripts/health-check-provider.py` 的输出，
  未设置时 skip（main 版脚本不在工作树中，没有基线文件无法对照）；
* 真正读取 Darwin 宿主状态的测试才会标 `darwin_integration` 并
  `skipif(sys.platform != "darwin")`（当前无此类用例）；
* Python 3.11 为最低语法基线（2209 的 Windows 环境为 3.11.9）。

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
  launchdctl.py       只操作固定 label 的 launchctl 适配器
  plist.py            plistlib 渲染 + 静态审计
  network_proxy.py    networksetup 快照/设置/恢复/读回校验/事务状态
  locking.py          fcntl.flock（lifecycle 锁 + core 锁）
  config.py           统一模板渲染 TUN/mixed、macOS 专用组覆盖、策略校验、mihomo -t
  conflict.py         外部 VPN/端口/默认路由/198.18 地址冲突检测（纯解析 + 注入执行器）
  probe.py            切换后的 60 秒 HTTP 探测（国内必通、国外至少一项）
  switch.py           mpm-switch：Nextin <-> mihomo，失败自动回滚到 Nextin
  lifecycle.py        install/configure/start/stop/restart/status/health/uninstall
  runner.py           持有 core 锁、启动前冲突检测、可选 caffeinate -i、信号转发、退出码映射
  cli.py              命令入口（无业务逻辑）
macos/templates/com.mihomo-proxy-management.core.plist.template
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
/usr/local/bin/mihomo-proxy-management
/usr/local/bin/mpm-switch
/usr/local/libexec/mihomo-proxy-management/current/mihomo
/usr/local/libexec/mihomo-proxy-management/current/runner
/usr/local/libexec/mihomo-proxy-management/scripts/{install,start,stop,restart,status,health-check,uninstall}.sh
/usr/local/libexec/mihomo-proxy-management/scripts/convert_sub.py
/usr/local/share/mihomo-proxy-management/python/{mpm_common,mpm_macos}
/usr/local/share/mihomo-proxy-management/templates/
/Library/Application Support/mihomo-proxy-management/config.yaml          (0600)
/Library/Application Support/mihomo-proxy-management/providers/subscription.yaml
/Library/Application Support/mihomo-proxy-management/{GeoIP.dat,GeoSite.dat,Country.mmdb} (0644)
/Library/Application Support/mihomo-proxy-management/state/subscription.env (0600)
/Library/Application Support/mihomo-proxy-management/state/deployment.json  (0600)
/Library/Application Support/mihomo-proxy-management/state/network-proxy-backup.json (0600)
/Library/Application Support/mihomo-proxy-management/logs/{core.out.log,core.err.log}
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
| `install`（含 `--no-start`） | plist 写入后立即 `launchctl disable system/<label>`，并读回 `print-disabled` 确认；安装结束一定处于 disabled |
| `start` | 先 `enable`（否则 disabled 的服务无法 bootstrap）→ `bootstrap`/`kickstart` → 运行、健康、TUN 验证全部通过后**立即再次 `disable`** |
| 成功 start 之后 | 已加载的作业继续运行；本机本次开机内崩溃仍由 KeepAlive 拉起；**下次开机不会加载** |
| `stop` | `bootout` + `disable` |
| `uninstall` | `stop`（已含 disable）后再显式 `disable` |

`disable` 只影响后续加载，不会杀掉正在运行的作业，所以“启动成功后立即 disable”既能
保留崩溃拉起，又能保证重启后由 Nextin 接管。`plist.audit` 不再强制
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

### 与 Windows 共用的 `scripts/health-check-provider.py`

Windows 计划任务（`scripts/refresh-production.ps1`、`scripts/run-xigua-ippool.ps1`）
仍以**原路径** `scripts\health-check-provider.py` 调用，命令行参数、退出码与 stdout
格式都没有变（拆分只是把逻辑抽成可导入函数）。`tests/test_health_check_cli_compat.py`
用 main 版快照对新版做 `--help` 与 mock 运行的逐字段对照。

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
6. provider healthcheck 通过且至少一个节点存活；并且 `AUTO-FOREIGN` 与 `GPT` 两个组
   都有存活节点（`/proxies` 的 `alive`）；
7. 注入式探针观察到**新增的 `utun*`** 或默认路由切到 `utun*`（仅“存在 utun”不算成功，
   其他 VPN 也会创建 utun）。

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

* `mpm-switch mihomo` **先要密码，再断网**：普通用户阶段只通过
  `osascript ... with administrator privileges` 弹密码框，不碰 Nextin。用户取消或弹窗
  失败时 Nextin 原样保持连接，命令直接返回非零。
* 密码框里执行的是带兜底的 shell 包装（`<cli> switch mihomo … --marker <tmp>;
  rc=$?; [ -f <tmp> ] || <cli> switch rollback …; exit $rc`）。marker 只在探测全部
  通过后由 Python 写入，所以 root 阶段崩溃、被 `SIGKILL`、或返回非零时，
  `switch rollback`（幂等）会执行。所有参数都经过 `shlex.quote`，AppleScript
  的反斜杠/双引号转义保留。
  **边界**：这条兜底只覆盖 *root Python 子进程* 的死亡（崩溃/`SIGKILL`/非零退出）。
  如果 osascript、包装 shell 或整个授权进程组本身被杀，rollback 不会运行，机器可能
  停留在中间状态。此时下一次 `mpm-switch nextin` 或 `mpm-switch status` 可以幂等修复：
  两条命令都会重新检查 launchd 状态、disable 读回、端口 7890/9090 与 198.19/16 utun，
  并据此停掉孤立 core 或恢复 Nextin。
* root 阶段第一步以**调用用户身份**断开 Nextin：
  `launchctl asuser <uid> sudo -u <user> scutil --nc stop <VPN>`（默认 `Nextin`，
  可用 `MPM_VPN_NAME` 覆盖；**不删除** VPN，也不改它的自动连接配置）。root 进程
  看不到用户的 NetworkExtension 会话，所以命令与**状态轮询**都必须走 `asuser`
  （`launchctl asuser <uid> sudo -u <user> scutil --nc list`）；root 自己的
  `scutil --nc list` 看不到用户会话，不能作为断开/连接的判据。断开失败则不启动 mihomo。
* 之后：冲突检测 → `start` → 探测。**任何**失败（含冲突检测本身抛异常）都统一收敛到
  回滚：`bootout` + `disable`（`disable` 放在 `finally`，即使 bootout 失败也执行）+
  disable 读回（`launchctl print-disabled` 必须确认 override 生效）+ 确认不再运行 +
  孤立 core 检查（端口 7890/9090 无监听、无 198.19/16 utun；检查失败按"可能仍在运行"
  处理）+ 恢复系统代理快照 + 以用户身份 `scutil --nc start <VPN>` + 等待 `(Connected)`。
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
  198.19/16 utun——然后才以用户身份 `scutil --nc start <VPN>`，等待 `(Connected)` 后
  再做一次简单探测并报告。stop 未确认（含 disable 读回失败或孤立 core）时不会连接
  Nextin，并提示先处理孤立 core 再运行 `mpm-switch nextin` 或手动处理。
  `mpm-switch nextin` 与 `mpm-switch status` 都是幂等的：重复运行会重新验证并修复
  上一次切换留下的中间状态（如 wrapper 兜底未执行的残留）。
* **开机默认不自启**：`install` 结束、`start` 成功后、`stop` 之后都处于
  `launchctl disable system/<label>`。重启后是 Nextin 接管；需要 mihomo 时再执行
  `mpm-switch mihomo`。
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
| 安装 `/Library/LaunchDaemons/...plist` | 是 | 是 |
| `launchctl disable system/<label>`（安装结束即执行，开机不自启） | 是 | 是 |
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

## 7. 测试

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
  ifconfig 解析、launchd PID/可执行文件身份判定与 fail-safe）、`test_macos_probe.py`
  （HTTP 状态、fake-ip 经 `route -n get` + utun 差集、60 秒单调时钟 deadline、演练开关）、
  `test_macos_switch.py`（先密码后断网、取消不动 Nextin、异常恢复、shell 包装兜底回滚、
  双向切换、回滚恢复代理快照）；
* plist 审计按**目标平台**判定绝对路径（`posixpath`），测试用 sandbox root 在渲染
  plist 前映射为 POSIX 目标路径（`Layout.to_target_path`），因此 Windows 宿主不会
  把 `C:\...` 写进 LaunchDaemon；
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

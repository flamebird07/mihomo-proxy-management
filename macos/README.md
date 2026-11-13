# macOS（Darwin arm64）部署说明

本目录是 macOS 侧的全部实现：`mpm_macos` Python 包、LaunchDaemon 模板、薄包装脚本和
host-free 测试。**策略（规则、策略组、端口）只有一份**：仓库根目录的
`config/config.yaml.template`，Windows 与 macOS 共用；macOS 渲染器只切换 TUN/mixed
技术字段并注入本机秘密，不另写规则。

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
  config.py           统一模板渲染 TUN/mixed、策略校验、mihomo -t
  lifecycle.py        install/configure/start/stop/restart/status/health/uninstall
  runner.py           持有 core 锁、可选 caffeinate -i、信号转发、退出码映射
  cli.py              命令入口（无业务逻辑）
macos/templates/com.mihomo-proxy-management.core.plist.template
macos/bin/mihomo-proxy-management      薄入口（安装时注入包路径）
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

## 3. KeepAlive 与崩溃拉起语义

plist 固定为：

```xml
<key>RunAtLoad</key><true/>
<key>KeepAlive</key>
<dict><key>SuccessfulExit</key><false/></dict>
<key>ThrottleInterval</key><integer>5</integer>
<key>ProcessType</key><string>Interactive</string>
```

按 `launchd.plist(5)`，`SuccessfulExit=false` 表示：**进程以非 0 退出或被信号杀死时自动拉起，
以 0 干净退出时不再拉起**。runner 的退出码因此有明确含义：

| 情况 | runner 退出码 | launchd 行为 |
|---|---|---|
| mihomo 正常退出 `0` | `0` | 不拉起 |
| mihomo 退出 `N != 0` | `N` | 5 秒后拉起 |
| mihomo 被信号 `S` 杀死 | `128 + S`（如 SIGKILL → 137） | 5 秒后拉起 |
| runner 自身被 `SIGKILL` | 非成功退出 | 拉起；core 锁由内核释放，新实例可获取 |
| 显式 `stop` | — | `launchctl bootout` 移除 job，**不会被 KeepAlive 拉起** |
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
| 正常 `stop` 走 `launchctl bootout`，之后不被拉起 | `test_macos_lifecycle.py::test_stop_restores_the_proxy_and_removes_the_snapshot`、`test_macos_launchdctl.py::test_bootout_targets_the_fixed_label` |
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

## 5. TUN 与 mixed 回退

TUN 判定链（缺一不可）：

1. Darwin + arm64 + root；
2. `mihomo -t` 通过；
3. LaunchDaemon running；
4. `127.0.0.1:9090/version` 可用（带 secret 认证）；
5. provider healthcheck 通过且至少一个节点存活（共享健康检查
   `common/python/mpm_common/controller_health.py`）；
6. 注入式探针观察到**新增的 `utun*`** 或默认路由切到 `utun*`（仅“存在 utun”不算成功，
   其他 VPN 也会创建 utun）。

TUN 失败按事务回退。以下任一环节失败都进入**同一个**回退事务（不存在“直接抛错、
不回退”的路径）：LaunchDaemon bootstrap/kickstart 失败、`wait_running` 超时、
controller readiness 失败、共享健康检查失败、utun 验证失败。

1. 停止 core；
2. 枚举所有 network service，快照 Web/Secure Web/SOCKS 的 enabled/server/port、
   bypass 域与“是否为带认证代理”；
3. 原子写入 `0600` 的 `network-proxy-backup.json`（stage=prepared）；
4. 渲染 mixed 配置：`tun.enable=false`、`mixed-port=7890`、DNS 从 `fake-ip` 改为
   `redir-host`（无 TUN 时 fake-ip 无人接管），其余生产规则不变；
5. 启动 core，完成 controller readiness，并通过**共享健康检查**
   （`common/python/mpm_common/controller_health.py`：provider healthcheck 成功且至少
   一个节点存活）；healthcheck 或节点存活失败都按 TUN 失败处理；
6. **仅在 core 健康后**才用 `networksetup` 把 HTTP/HTTPS/SOCKS 指向 `127.0.0.1:7890`
   （mixed 健康检查失败则 fail-closed：回滚快照、不设置系统代理）；
7. 读回校验，stage 改为 applied，状态记为 `DEGRADED`；
8. `stop`/`uninstall`/下次启动前按快照逐项恢复并读回校验，成功后才删除快照。

原代理若启用认证，`networksetup` 无法读取/恢复密码，**默认拒绝覆盖**并给出明确错误，
不会声称“已完整恢复”。

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
| `launchctl bootstrap system <plist>` | 是 | 是 |
| 首次启动并验证 TUN/utun | 是 | 是 |
| TUN 失败时保存系统代理原值 | 否（只读快照） | 服务自身为 root |
| 将系统代理切到 `127.0.0.1:7890` | 是（首次回退前明确提示） | 通常需要 |
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

* `macos/tests` 全部 host-free：subprocess/launchctl/networksetup/fcntl/controller 均为注入，
  可在 Windows、Linux、macOS 上运行（含 Windows 2209 / Python 3.11.9）；
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

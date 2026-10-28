# Mihomo Proxy Management

> 一键部署 mihomo (Clash.Meta) 透明代理：填一个订阅链接，剩下的全自动。
> Drop-in mihomo (Clash.Meta) deployment — plug in one subscription URL, the rest is automatic.

适用于 Windows（管理员 PowerShell）**与 Linux 系统部署**（Ubuntu 22.04/24.04、Debian 12，
systemd，见 `docs/LINUX.md`）。
Windows (elevated PowerShell) and a Linux **system profile** (Ubuntu 22.04/24.04,
Debian 12, systemd - see `docs/LINUX.md`) are supported; other platforms are best-effort.

> ⚠️ 订阅链接 = 凭证。任何真实订阅 URL / 节点信息都不得提交进 git，也不得原样打印到
> 终端 / CI 日志。文档与示例一律使用 RFC 2606 `example.invalid` 占位。
> A subscription URL **is a credential**. Never commit it or echo it into logs; all
> docs/examples use RFC 2606 `example.invalid` placeholders.

---

## 特性 / Features

- 仅需一个 YAML 订阅链接，无需手动维护节点列表
- 运行时下载 mihomo / wintun / Geo 数据，无需把二进制提交进 git
- 自动注册计划任务，开机自启 + 崩溃自动重启
- TUN 模式透明代理，无需配置系统代理
- 内置 `AUTO-FOREIGN` / `US-FAST` / `VIETNAM` / `GLOBAL` 智能分组
- 订阅转换脚本 `scripts/convert_sub.py`：支持 ss / trojan / vless / tuic / **anytls** / **hysteria2**；AnyTLS 按 mihomo 原生 `type: anytls`（`password`/`sni`/`client-fingerprint`）原样保留，**不伪装成 vless**
- `start / stop / restart / status / test / update-subscription / uninstall` 一套脚本

---

## 订阅转换 / Subscription Conversion

`scripts/convert_sub.py` 把订阅转成 mihomo / Clash.Meta YAML。三类输入逐源自动识别：

1. **Clash / mihomo YAML**（含 `proxies:` 的映射）——节点原字段透传，嵌套结构保持不变；
2. **sing-box JSON**（顶层 `outbounds`）——映射 shadowsocks、trojan、vless、tuic、hysteria2、anytls；
3. **base64 / 纯 URI 列表 / 单个分享链接**——复用原有 URI 解析器。

sing-box 的 selector/urltest/direct/block/dns 只计入 `skipped_non_node`，不冒充代理节点；
未知类型告警、计数并跳过。TLS/SNI/ALPN/uTLS/Reality、WS/gRPC、TUIC、Hysteria2 混淆和
AnyTLS 会话参数按支持范围映射；不能忠实映射的选项（例如 detour、证书文件、未知 transport）
按 `skipped_unsupported` 告警并跳过整节点，不静默降级为 TCP 或绕过 TLS。
完整范围见 [架构说明](docs/ARCHITECTURE.md)。

支持的 URI 协议：`anytls` / `ss` / `trojan` / `vless` / `tuic` / `hysteria2`（`hy2`）。
AnyTLS **不再伪造成 vless**：`anytls://password@host:port` 输出为 mihomo 原生的
`type: anytls` + `password`，并保留 `sni` / `client-fingerprint` / `alpn` /
`skip-cert-verify` 等 TLS 参数（支持 percent-encoding 与 IPv6 字面量）。

未知项处理（两者不同，勿混淆）：

- **未知 URI 协议**（如 `vmess://`）：**始终**带「节点序号 + 白名单内协议类型」告警、计入
  `skipped_unknown` 并**跳过**，不会保留；非白名单的类型文本不回显；
- **Clash YAML 里的未知 `type`**：**默认原样保留**（透传），仅显式加 `--skip-unknown`
  才丢弃；两种情况都会统计 `input / output / skipped`。

输入健壮性：仅当输入是**纯 URI 列表**时才走 URI 回退；含 `://` 的畸形 Clash YAML
**直接 fail closed**（不会把 YAML 内部 URL 当成节点）；URI 列表中非 URI 行会计数告警，
不静默丢弃。

```bash
# SUB_URL 预先从本机环境提供；不要把真实 URL 写进命令行或历史
python scripts/convert_sub.py --url-env SUB_URL -o providers/subscription.yaml

# 或从 stdin 读 URL
echo "$SUB_URL" | python scripts/convert_sub.py --url-stdin -o providers/subscription.yaml

# 直接吃 Clash YAML（原字段透传）并列出节点
python scripts/convert_sub.py -i clash_sub.yaml --list

# 合并进现有配置（按除 name 外的完整字段去重，保留同名不同节点）
python scripts/convert_sub.py -i raw.txt --merge providers/subscription.yaml -o providers/subscription.yaml
```

### 三源候选池（不自动上线）

从 `config/subscription.env.example` 复制本地环境文件，私下填入 `SUBSCRIPTION_URL_1`、
`SUBSCRIPTION_URL_2`、`SUBSCRIPTION_URL_3`；对应的 `SUBSCRIPTION_UA_1/2/3` 可分别设置，
默认 `ClashMeta/1.18.0`。环境文件按 `KEY=VALUE` 读取，支持 UTF-8 BOM 和成对引号；
不执行变量或命令展开，不剥离值中的 `#`、`=` 或 query。不要在值后写行内注释。

以下是后续授权后的使用方式，本阶段测试不访问真实订阅：

```powershell
# 输出目录须已存在，先选择不在运行中的候选文件，勿直接覆盖生产缓存
.\scripts\update-subscription.ps1 -EnvFile .\subscription.env -Output .\providers\candidate.yaml -MinNodes 20
```

```bash
# 等价 Python 入口；三源变量已由本机环境提供，无需读取环境文件
python scripts/convert_sub.py --sources-env -o providers/candidate.yaml --min-nodes 20
# 或指定本地环境文件
python scripts/convert_sub.py --sources-env-file subscription.env -o providers/candidate.yaml
```

- 三个槽位独立抓取、识别和统计；缺失/空结果/失败的来源不借用旧缓存，不阻断其他来源。
- 合并按除 `name` 外的完整字段规范化比较，包含协议、端点、认证、TLS 和传输；字段键顺序不影响比较。
  完全相同的配置只保留第一份；同名不同配置保留并按源顺序命名为 `名称 [2]` 等，避开已有名称。
  不臆测不同格式中“省略默认值”与“显式默认值”等价，以免误删候选；可能保留行为等价的节点。
- `MIN_CANDIDATE_NODES` 默认 **1**，是保守的防空池门槛，不代表足够多；应按历史候选规模提高。
  `--min-nodes` / `-MinNodes` 优先，必须为正整数；门槛在新抓取结果去重后检查，旧 `--merge` 节点不能凑数。
- 每源日志含 format/input/output、各 skipped 分类与 failed，另报 merged/duplicates/failed_sources；
  output 是源转换后、全局去重前的数量。解析失败无法计算 input 时记 0 且 failed=1。
- 退出码 **0**：所有来源产生候选且满足门槛；**5**：部分来源失败，但成功来源达标且已原子写出
  （`--list` 只预览、不写出）；**1**：空池/低于门槛；**2**：参数或依赖问题；**3**：解析/输出失败；
  **4**：单源网络失败。三源全失败最终为 1；多源警告与统计不受 `--quiet` 抑制。不能把 5 当全成功。
- `update-subscription.ps1` 现在只调用转换器并透传退出码；旧 `-Resubscribe` / `-ShowYaml` 参数已移除。
  不再请求 controller、删除缓存、提权、启动、重载或重启 mihomo。
- 旧安装模板仍是单源 HTTP provider，**不会自动使用候选池**；不要直接覆盖其自动刷新的缓存。

### 动态多目标健康池

候选数量不是固定值。使用独立 Mihomo 实例对每个节点逐目标检测，只有同时通过基础连通性、
ChatGPT 和 muse.ai 的节点才写入新 provider；输出采用原子替换。美国节点数量可以随订阅和健康状态
增减，`--minimum-matching 1` 保证本轮至少存在一条美国健康节点，否则保留上一版文件、不覆盖生产池。

```powershell
python .\scripts\health-check-provider.py `
  --candidate C:\mihomo\providers\three-sources-candidate.yaml `
  --output C:\mihomo\providers\three-sources-all-targets.yaml `
  --target https://www.gstatic.com/generate_204 `
  --target https://chatgpt.com/cdn-cgi/trace `
  --target https://muse.ai/ `
  --minimum-matching 1
```

生产 `US-FAST` 使用 file provider 的 `use` 和美国名称过滤器读取上述健康池，不在配置中固定写死
节点数量；ChatGPT 与 `muse.ai` 的规则均指向 `US-FAST`。控制器 delay 通过后仍应做真实 HTTPS 请求复核。

Windows 生产环境可运行 `scripts/refresh-production.ps1` 串联三源更新、三目标健康交集、热加载、
分组选择恢复及应用层复核；任一步失败都会保留或恢复上一版健康 provider。
  接入本地 file provider、离线核心校验和真实健康检查需独立授权，见架构说明。

这里的“候选”仅表示完成格式转换和结构检查；未经过目标 mihomo 二进制校验，更不代表已联网可用。
AnyTLS 需原生支持它的核心版本；未知 YAML 类型仍透传，目标核心可能不接受，必须上线前校验。

安全与健壮性：

- 日志与 `--list` 不打印 URL、路径、节点原名、server 或认证字段，只用序号、白名单协议与计数；
  HTTP/URL 异常仅回显 **固定分类 / HTTP 数字状态 / 异常类型**，不回显上游 reason 或原始端口。
  单源不带 `-o` 时标准输出是完整 YAML **产物**而不是安全日志，含认证信息，不要录入日志或提交；
  三源模式要求 `-o` 或 `--list`，避免意外把凭据产物打到终端。
- 默认 UA 为 `ClashMeta/1.18.0`，可用 `--user-agent` 或环境变量 `CONVERT_SUB_UA` 覆盖。
- **PyYAML 为硬依赖**，缺失时直接报错退出（fail closed），不会手工拼接脆弱 YAML。
- 空输出 / 畸形 YAML / 无效顶层会给出明确诊断；`--list` 空结果同样非零退出；处理失败
  **不会覆盖已有输出文件**（原子写入）。

测试：

```bash
python -m unittest discover -s tests -p test_*.py
```

---

## 快速开始 / Quick Start

```powershell
# 1. 克隆
git clone https://github.com/flamebird07/mihomo-proxy-management.git
cd mihomo-proxy-management

# 2. 复制配置模板，填入你的订阅链接
copy config\subscription.env.example C:\mihomo\subscription.env
notepad C:\mihomo\subscription.env   # 填 SUBSCRIPTION_URL / SECRET

# 3. 管理员身份运行 PowerShell，执行安装
.\scripts\install.ps1
```

安装脚本会：

1. 自动检测 / 创建 `C:\mihomo` 安装目录
2. 下载最新版 mihomo.exe + wintun.dll + Geo 数据
3. 用模板生成 `C:\mihomo\config.yaml`
4. 注册 `MihomoProxy` 计划任务（开机 + 崩溃自动启动）
5. 立即拉起服务并验证

---

## 管理命令 / Management

所有脚本都以管理员身份运行。脚本路径相对仓库根。
Run scripts as Administrator. Paths are relative to the repo root.

| 脚本 / Script | 作用 / Purpose |
|---|---|
| `.\scripts\install.ps1`           | 安装 / 重新生成配置（支持 `-Force` 重新下载） |
| `.\scripts\start.ps1`             | 启动 |
| `.\scripts\stop.ps1`              | 停止 |
| `.\scripts\restart.ps1`           | 重启 |
| `.\scripts\status.ps1`            | 状态总览（进程 / 端口 / TUN / 节点 / 选组） |
| `.\scripts\test.ps1`              | 连通性测试（直连 + 走 7890 代理） |
| `.\scripts\update-subscription.ps1` | 三源候选生成（必填 `-EnvFile` / `-Output`，可选 `-MinNodes`；不重载服务） |
| `.\scripts\uninstall.ps1`         | 卸载（`-Purge` 顺带清目录） |

---

## 配置 / Configuration

### `subscription.env`

```ini
SUBSCRIPTION_URL=https://provider.example.invalid/link?clash=1   # 占位示例；真实链接是凭证
SUBSCRIPTION_INTERVAL=60       # 分钟；0 = 仅启动拉一次
SECRET=change-me               # Dashboard 密码，留空 = 无密码
INSTALL_DIR=C:\mihomo          # 默认值，可不改
```

> 该文件是**真凭证的存放地**（仅本机，已被 `.gitignore` 递归覆盖，任何目录下都叫
> `subscription.env` 一律忽略）。仓库里只有 `.example` 模板。
> This file holds real credentials; the recursive `.gitignore` covers it anywhere.

### 修改代理组 / Editing proxy groups

`config\config.yaml.template` 是模板，下面这些正则可按需调整：

```yaml
- name: US-FAST
  filter: "(?i)美|us|usa|🇺🇸"   # 匹配美区节点的命名
- name: VIETNAM
  filter: "(?i)越南|vietnam|🇻🇳"
```

修改后重新跑：

```powershell
.\scripts\install.ps1 -SkipDownload
```

---

## 文件结构 / Layout

```
mihomo-proxy-management/
├── README.md
├── LICENSE
├── .gitignore
├── config/
│   ├── config.yaml.template       # 占位符模板，install.ps1 渲染
│   └── subscription.env.example   # 配置示例（不含凭证）
├── docs/
│   ├── ARCHITECTURE.md            # 详细架构 / 故障排查（Windows）
│   ├── LINUX.md                   # Linux 系统部署：支持矩阵 / 降级 / 供应链 / 生命周期
│   └── hermes-mihomo-SKILL.md     # Hermes 技能修正稿（脱敏）
├── linux/                         # Linux 实现（唯一生命周期逻辑 = python/mpm）
│   ├── bin/mihomo-proxy-management    # 薄入口 wrapper（无业务逻辑）
│   ├── python/mpm/                 # CLI + 生命周期引擎
│   ├── supply/mihomo.lock.json     # 摘要锁定的供应链清单
│   ├── systemd/*.template          # 非机密 unit 参考模板
│   └── tests/                      # 免主机测试（run_tests.py）
├── providers/
│   └── .gitkeep
├── scripts/
    ├── lib/common.ps1             # 共享函数
    ├── convert_sub.py             # 三源候选转换（YAML / sing-box JSON / base64 / URI）
    ├── install.ps1                # 一键安装
    ├── start.ps1
    ├── stop.ps1
    ├── restart.ps1
    ├── status.ps1
    ├── test.ps1
    ├── update-subscription.ps1
    └── uninstall.ps1
└── tests/
    └── test_convert_sub.py        # convert_sub.py 单元测试
```

---

## Linux（系统 systemd）Quick Start

```bash
sudo mkdir -p /etc/mihomo-proxy-management
sudoedit /etc/mihomo-proxy-management/subscription.env   # 0600；MPM_* 键，详见 docs/LINUX.md
sudo python3 linux/tests/run_tests.py                    # 先跑免主机测试
sudo MPM_SOURCE_DIR=$PWD/linux ./linux/bin/mihomo-proxy-management install --enable
sudo ./linux/bin/mihomo-proxy-management status
```

要点 / Key points：
- TUN 不可用时**显式降级**为 mixed-port（大声的 DEGRADED 横幅 + 持久化原因），绝不静默
- 订阅 URL 只来自 0600 文件或 install/configure 进程环境变量，绝不经命令行参数
- 下载二进制按 `linux/supply/mihomo.lock.json` 的 sha256+size 逐项校验，任何不匹配都不安装、不启动
- `start` 从不隐式 `enable`；端口被其它 systemd 单元占用时拒绝启动并**点名占用者**（绝不 kill）
- 真实 CN 出口需用户自验；CI 只用本地 mock 与 `example.invalid` 金丝雀

---

## 常见问题 / FAQ

**Q: 跑 `install.ps1` 后 9090 端口没起来？**
A: 看 `C:\mihomo\logs\mihomo.log`。最常见原因：`subscription.env` 里 `SUBSCRIPTION_URL` 没替换成真实值，或机场返回的不是 mihomo 兼容格式。

**Q: 怎么换机场？**
A: 编辑 `C:\mihomo\subscription.env` 改 `SUBSCRIPTION_URL`，然后 `.\scripts\install.ps1 -SkipDownload`。

**Q: 想自定义 rules？**
A: 改 `config\config.yaml.template`，重跑 `install.ps1`。

**Q: TUN 起不来？**
A: 99% 是 wintun 驱动没装。`install.ps1` 会自动下；手动检查 `C:\mihomo\wintun.dll` 是否存在、版本是否匹配 mihomo。

---

## License

MIT

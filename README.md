# Mihomo Proxy Management

> 一键部署 mihomo (Clash.Meta) 透明代理：填一个订阅链接，剩下的全自动。
> Drop-in mihomo (Clash.Meta) deployment — plug in one subscription URL, the rest is automatic.

适用于 Windows（管理员 PowerShell）。其它平台仅作参考。
Targets Windows (elevated PowerShell). Other platforms are best-effort.

---

## 特性 / Features

- 仅需一个 YAML 订阅链接，无需手动维护节点列表
- 运行时下载 mihomo / wintun / Geo 数据，无需把二进制提交进 git
- 自动注册计划任务，开机自启 + 崩溃自动重启
- TUN 模式透明代理，无需配置系统代理
- 内置 `AUTO-FOREIGN` / `US-FAST` / `VIETNAM` / `GLOBAL` 智能分组
- `start / stop / restart / status / test / update-subscription / uninstall` 一套脚本

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
| `.\scripts\update-subscription.ps1` | 立即刷新订阅（`-Resubscribe` 强重拉，`-ShowYaml` 显示节点） |
| `.\scripts\uninstall.ps1`         | 卸载（`-Purge` 顺带清目录） |

---

## 配置 / Configuration

### `subscription.env`

```ini
SUBSCRIPTION_URL=https://your-provider.com/link?clash=1
SUBSCRIPTION_INTERVAL=60       # 分钟；0 = 仅启动拉一次
SECRET=change-me               # Dashboard 密码，留空 = 无密码
INSTALL_DIR=C:\mihomo          # 默认值，可不改
```

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
│   └── ARCHITECTURE.md            # 详细架构 / 故障排查
├── providers/
│   └── .gitkeep
└── scripts/
    ├── lib/common.ps1             # 共享函数
    ├── install.ps1                # 一键安装
    ├── start.ps1
    ├── stop.ps1
    ├── restart.ps1
    ├── status.ps1
    ├── test.ps1
    ├── update-subscription.ps1
    └── uninstall.ps1
```

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
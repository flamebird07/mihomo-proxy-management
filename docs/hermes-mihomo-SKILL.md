# Hermes Skill 修正稿 — mihomo-proxy-management（脱敏）

> 本文件是主脑验收后用于同步到 Hermes 安装副本的**最小修正稿**。
> 相对仓库外 `C:\Users\Administrator\AppData\Local\hermes\skills\devops\mihomo-proxy-management\SKILL.md`
> 只改动「协议支持 / 使用说明」两处错误，并删除明文 token 示例，替换为环境变量输入。
> 其余机器拓扑、SSH 同步、开机调度、代理组、排障内容保持不变。

## 一、修正点 1 — 节点转换脚本协议支持（原文错误）

原文（错误）：

```
## Node Conversion Script
C:\mihomo\convert_sub.py — converts subscription to Mihomo format
- Supports: ss, trojan, vless, tuic
- Does NOT support: anytls, hysteria2 (yet)
```

修正后：

```
## Node Conversion Script
scripts/convert_sub.py (repo) /
C:\Users\Administrator\AppData\Local\hermes\skills\devops\mihomo-proxy-management\scripts\convert_sub.py (Hermes 安装副本；这是唯一真实安装路径，C:\mihomo\convert_sub.py 不存在)
- 输入自动识别: Clash/mihomo YAML(含 proxies:) / base64 订阅 / 纯 URI 列表或单个分享链接
- 支持的 URI 协议: anytls, ss, trojan, vless, tuic, hysteria2 (hy2)
- AnyTLS 原生输出 type: anytls + password（保留 sni / client-fingerprint / alpn /
  skip-cert-verify），绝不伪装成 vless/uuid
- Clash YAML 里的未知 type: 默认“原样保留”（透传），仅 --skip-unknown 显式丢弃
- 未知 URI 协议（如 vmess://）: “始终”告警 + 计数并“跳过”，不保留（与 YAML 未知 type 不同）
- 畸形节点/URI 带“安全节点名 + type”告警并统计 input/output/skipped
- 仅“纯 URI 列表”才走 URI 回退；含 :// 的畸形 Clash YAML 直接 fail closed；非 URI 行计数告警，不静默丢弃
- 异常诊断只用固定分类 / HTTP 数字状态 / 异常类型，不回显上游 reason、URL query 或原始端口
- PyYAML 为硬依赖，缺失即 fail closed（不手工拼 YAML）
- 空输出 / --list 空结果非零退出；失败不覆盖已有输出（原子写入）
```

## 二、修正点 2 — 订阅 URL 明文示例（不安全）

请勿在技能/脚本/命令行中写入明文订阅 URL（含 `token=`）。改用环境变量或 stdin：

```powershell
# 推荐：URL 从环境变量读入，不落到命令行/进程列表；用 Hermes 安装副本的真实绝对路径
$env:SUB_URL = "<由本地 env 提供，勿提交>"
python "C:\Users\Administrator\AppData\Local\hermes\skills\devops\mihomo-proxy-management\scripts\convert_sub.py" --url-env SUB_URL -o providers\subscription.yaml

# 或从 stdin
$env:SUB_URL | python "C:\Users\Administrator\AppData\Local\hermes\skills\devops\mihomo-proxy-management\scripts\convert_sub.py" --url-stdin -o providers\subscription.yaml
```

脚本默认**不完整打印** URL（仅 `scheme://host/<redacted>`）；HTTP/URL 异常只回显**固定分类 /
HTTP 数字状态 / 异常类型**，不回显上游 reason、URL query、原始端口或凭据。
默认 UA 为 `ClashMeta/1.18.0`，可用 `--user-agent` 或 `CONVERT_SUB_UA` 覆盖。

## 三、保持不变的内容（摘要）

- **机器**：87（周公瑾，HTTP 订阅）/ 50（郭奉孝，本地 file provider；HTTP 订阅 IP 绑定不可用）。
- **端口**：mixed `127.0.0.1:7890`、DNS `127.0.0.1:1053`、API `127.0.0.1:9090`。
- **SSH/同步**：87 拉取并转换 → `scp` 到 50 → 两端重启。
- **调度**：Windows 计划任务 `MihomoProxy` 为唯一启动入口；先停任务→全量杀进程→由任务单实例拉起，
  避免双进程；`legacy-disabled` 下的 vbs 启动器已废弃，禁止回收。
- **代理组**：PROXY(select) / AUTO-FOREIGN(url-test) / US-FAST(url-test, 美国节点) /
  VIETNAM(select) / CODEX(select，固定国家不自动切换)。

以上均为原技能已有内容，本稿不重复展开，仅供同步时对照修正。

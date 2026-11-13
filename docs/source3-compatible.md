# 源3独立兼容内核

源3实际配置使用专有 `ninja` 协议，标准 Mihomo 不支持。独立运行副本位于
`C:\mihomo\source3-compatible`，包含兼容内核及一次性导出的节点凭据；不得提交这些文件。

首次导入命令：`python scripts/source3_compatible.py --import-ninja`。
已有导出时拒绝覆盖。导入需要本机 Ninja 配置；后续测速和运行不读取 Ninja 文件或 API。
当前节点是导出快照；供应商更换凭据或节点后需要重新导入，10 分钟刷新不会自动续订凭据。

现有任务 `MihomoNinjaGrokFeishuSync` 每 10 分钟调用
`sync-feishu-bitable.py --ninja-only`，有独立互斥锁，任务上限 8 分钟。
发现独立导出后，使用独立内核探测 Grok 首页、ChatGPT trace、OpenAI 目标及实际出口地区。
单轮测速预算 100 秒、地区探测预算 230 秒；网络故障仍可能导致任务失败，不能将旧结果冒充新结果。

飞书表2以“源3独立兼容内核”标记，替换旧“Ninja Desktop”记录，与“订阅 Mihomo”分开。
表3同样保留路径标记。正常订阅同步保留这两张表中的独立路径记录。
写入后回读核对节点、存活、地区、目标和时间，结果保存在 `last-result.json`。

独立 GPT 入口为 `127.0.0.1:17993`（HTTP/SOCKS），管理端口19993仅限本机并带认证。
实际美国出口优先，新加坡次选，日本兜底；ChatGPT和OpenAI连续两轮通过才入选，无合格节点时REJECT。
当前节点若仍属于最高可用地区且合格，则保持选择，避免同地区仅因延迟波动切换。
仅操作此独立核心的 GPT 选择器，不操作 Ninja 或原主核心。探测成功代表目标可达，不代表登录后模型推理验收。

`mihomo-bridge.yaml` 提供标准 SOCKS 桥接节点，标准 Mihomo 可通过该节点接入兼容内核。
主 GPT 组已增加“源3独立GPT”桥接成员，现有选择保持不变。桥接内部按美国、新加坡、日本选择。
主自动切换脚本读取每10分钟更新的实测地区和连续通过轮数，而不是从桥接名字推断地区；
超过20分钟、未回读验证或连续通过不足两轮的状态不能成为主动优先切换候选。
同一主内核另提供本机验证入口17995，固定经过桥接，便于不改变主GPT选择地验证请求。

关闭 Ninja 的实机测试会违反当前“不切换或断开连接”的要求，本次使用独立文件、进程、直连出口和请求结果证明独立路径，保留 Ninja 当前连接。

## 2026-11-04 验收

- 首轮手动测试32节点，Grok32存活，32实际地区。
- 自动任务实际在20:08:25启动，20:09:21结束，返回0；下次20:18:24。
- 独立回读表2：32行，31存活，31本轮地区，统一时间20:09:03.106。
- 独立回读表3：10个候选满足连续两轮连通性要求。
- 独立入口选择美国04，Grok和ChatGPT实请求HTTP200，出口美国/加利福尼亚/洛杉矶。
- OpenAI根地址返回421；原有探测规则接受此状态作为连通，不等于API或模型推理成功。
- Ninja PID8748和全部Selector选择与操作前相同。
- 50项相关测试通过；实测明细在私有运行目录的acceptance.json和last-result.json。
- 尚未把桥接节点加载进原有主GPT组；当前可用的是独立17993入口。未通过退出Ninja进行验收。

## 2026-11-04 20:22 主入口接入验收（覆盖上一节未接入状态）

- 使用 `scripts/connect_source3_main.py` 校验配置、备份、保存原选择后执行 `force=false` 配置加载。
- 主GPT新增“源3独立GPT”，当前仍为“日本｜直连2”；未重启主内核。
- 加载前172条连接加载后全部仍在；跨加载保持的回声连接连续成功。
- 主内核17995入口请求Grok和ChatGPT均HTTP200，出口美国/加利福尼亚/洛杉矶。
- 控制器实测请求链：主内核“源3独立GPT”→我们独立内核“GPT/美国04”；运行文件来自独立目录，无Ninja代理转发。
- Ninja PID和全部Selector选择未变。保留桌面App运行以保护用户当前连接，未进行退出App测试。
- 飞书表3回读时间20:22:03.170：美国5、新加坡5、日本3个合格候选；日本另两节点未达到本轮门槛，未冒充合格。
- 本次相关56项测试通过。旧自动切换测试在相同隔离环境下修改前后均28失败、46通过、4跳过，属于既有基线失败，本次没有新增。
- 私有证据：`main-bridge-install.json`、`main-bridge-probes.json`、`main-bridge-acceptance.json`。
- 保留当前选择与后续自动优先选择是两个阶段：本次没有手动将主GPT切换至源3；后续既有自动切换任务可按门槛使用桥接。

加载语义核对：[Mihomo v1.19.28 executor源码](https://github.com/MetaCubeX/mihomo/blob/v1.19.28/hub/executor/executor.go)。

## 主GPT健康检查误退修复

2026-11-04现场复现：同一源3出口，对ChatGPT `/cdn-cgi/trace` 的GET返回200，HEAD返回403。
主内核URLTest使用HEAD；全局alive和delay只说明请求未发生传输错误，
指定URL的extra.alive才包含expected-status判定。旧手动切换探测只读取正延迟，漏掉了该判定。
来源：[当前版本URLTest实现](https://github.com/MetaCubeX/mihomo/blob/v1.19.28/adapter/adapter.go)。

主GPT组和 `proxy-auto-switch.py` 的检查目标统一改为 `https://chatgpt.com/robots.txt`，
实测HEAD返回200，仍严格要求200；未放行403。自动切换探测显式传expected=200，
并回读该URL的extra.alive，缺失或false都拒绝正延迟结果。
此检查代表ChatGPT站点可达，不代表登录后模型调用完成。

应用时127条连接全部保留，持续回声连接未中断；主GPT恢复源3桥接。
部署脚本 `scripts/fix-source3-health.py` 保存私有配置备份、校验配置并以force=false加载。
相关9项测试通过，包含正延迟但HTTP检查失败、健康状态缺失、HTTP检查通过三个分支。
自动轮次验收记录另存私有目录 `health-fix-automatic.json`。

20:38:21.710自动健康检查新增记录：627ms、extra.alive=true；主GPT仍为源3独立GPT，
未回落日本。20:35:46之后至此未调用手动delay接口，仅回读状态，确认验收来自自动轮次。

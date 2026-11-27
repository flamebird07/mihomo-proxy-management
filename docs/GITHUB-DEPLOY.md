# 从 GitHub 用飞书供池部署新机 / GitHub + Feishu Bitable bootstrap

> **适用**：Apple Silicon Mac（arm64）与 Windows 10/11 x64。
> Windows 侧固定使用 `mihomo-windows-amd64-v3`（需要 CPU 支持 amd64-v3 微架构），
> 没有内置 v1/v2 备用资产。
>
> **交付分支**：`feat/github-bootstrap-deploy`（功能分支交付，尚未并入 `main`）。
> 下文所有 clone 示例都显式 checkout 这个分支；合并进 `main` 后把分支名换成 `main` 即可。
>
> **安全底线**：订阅 URL、飞书 App Secret、多维表 app_token **都是凭证**。
> 真实值只存在于本机（环境变量、stdin、或安装后的 0600 / ACL 受限文件），
> 绝不提交进 Git，也绝不写进命令行参数或 shell 历史。本文示例全部是
> `example.invalid` 与假值。

---

## 0. 三条命令（复制即用）

### Apple Silicon Mac

```bash
git clone --branch feat/github-bootstrap-deploy https://github.com/flamebird07/mihomo-proxy-management.git
cd mihomo-proxy-management
sudo ./macos/scripts/bootstrap.sh
```

运行前确认：Apple Silicon（arm64）、已安装 Git、有 Python ≥ 3.9 且可创建 venv
（引导脚本可按需准备专用 venv 与 PyYAML）。所有安装动作以 root 执行，因此需要 `sudo`。

### Windows x64（amd64-v3）

**以管理员身份打开 PowerShell**（右键 → 以管理员身份运行），再执行：

```powershell
git clone --branch feat/github-bootstrap-deploy https://github.com/flamebird07/mihomo-proxy-management.git
cd mihomo-proxy-management
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\deploy.ps1
```

第三条只对**当前进程**绕过执行策略（不是 `Set-ExecutionPolicy` 改全局设置）；
脚本内部仍会调用 `Ensure-Admin`，所以 PowerShell 本身必须已提升。
缺少 Python 3.10+ 时引导会下载官方安装器（SHA256 + Authenticode 校验）并安装 PyYAML。

详细说明见下文；只想知道「下一步做什么」可以先读 §5（运行后状态）和 §7（校验与排错）。

---

## 1. 前置条件

| 项目 | Apple Silicon Mac | Windows x64 |
|---|---|---|
| 系统 / CPU | macOS on arm64 | Windows 10/11 x64，CPU 支持 **amd64-v3** |
| 权限 | `sudo`（root 安装 LaunchDaemon 到 `/Library`、`/usr/local`） | 管理员 PowerShell（TUN、计划任务、服务） |
| Git | 必需 | 必需 |
| Python | ≥ 3.9，且 `venv` 可用；引导可按需准备专用 venv / PyYAML | ≥ 3.10；没有时引导自动安装（`-SkipPython` 可禁止自动安装） |
| 网络 | GitHub、`open.feishu.cn` 和 Python 包索引（首次准备 PyYAML） | 同左，另需 `python.org` 与 `wintun.net` |
| 目标盘 | 系统盘（路径固定，见 §5） | 默认 `C:\mihomo`，可用 `-InstallDir` 指定 |

Windows 引导从 `github.com`、`python.org`、`wintun.net` 下载；GitHub 下载可被
`MPM_GITHUB_MIRROR` 前缀镜像，但**校验值不变**——镜像只能让下载失败，不能替换内容。
两端都只要求出站 HTTPS，不需要入站端口。

> Windows 的 amd64-v3 是核心二进制本身的锁定项（`scripts/deploy/supply.lock.json` 的
> `mihomo-windows-amd64-v3`）。CPU 不支持 v3 时 mihomo 会无法执行；本部署流程不会
> 自动降级到其它指令集版本。

---

## 2. 飞书准备与授权

### 2.1 应用权限

在飞书开放平台创建企业自建应用，至少开通并**发布**以下权限：

- `bitable:app`：多维表读写（不是只读）。引导会用一次真实写入证明权限，见 §4。
- 如果交出的是知识库（wiki）链接，还需要 `wiki:wiki:readonly` 用于解析节点。

创建后拿到 **App ID / App Secret**，并确保应用版本已发布；只创建、未发布的应用会
在鉴权步骤失败。

### 2.2 把应用加到多维表里

打开目标多维表 → 右上角 `...` → **添加文档应用**，选择上面的应用并授予**可编辑**。
知识库链接还需要把应用加入对应知识库空间。

**授权对象是「应用」，不是安装者本人的浏览器账号。** 安装者能在浏览器里打开这张表，
不代表应用有权限；反之，应用被加为文档应用后，即使换一个人执行安装也能工作。

### 2.3 用到的表

| 表名 | 引导怎么用它 | 能否缺 |
|---|---|---|
| **节点测速总表** | 只读。每一行都是一个候选节点，用来生成本机节点池 `feishu-pool.yaml` | 纯飞书供池时必需；配置了订阅 URL 时不需要 |
| **部署记录** | 写入探针 + 部署台账。按「主机名」upsert 当前机器一行；表不存在时自动建表 | 必需（写权限的证明） |
| **有效节点** | 流水线测速后的派生表，引导**不写** | 无关 |

「节点测速总表」由节点流水线的采集侧写入；引导只读不改。节点池构建不做 GPT 门槛、
地区过滤或新鲜度截断——总表的 7 天墓碑已经清掉死节点，其余行全部作为候选
（结构非法的广告行、重复项会被丢弃/去重）。

### 2.4 多维表地址

支持三种写法，只认 `feishu.cn` 域：

- `https://<租户>.feishu.cn/base/<app_token>`（推荐）
- `https://<租户>.feishu.cn/wiki/<node_token>`（需要 wiki 权限，运行时会解析成 app_token）
- 直接粘贴 `<app_token>` 裸串

带账号信息（`user:pass@`）的地址会被拒绝，避免把账号打进日志。

---

## 3. 凭据输入

三个值：**多维表地址**、**App ID**、**App Secret**。优先级逐字段为：

```text
进程环境变量  >  stdin JSON  >  交互提示
```

### 3.1 命名环境变量

| 用途 | 变量名 |
|---|---|
| 多维表地址 | `MPM_FEISHU_BASE_URL`（别名 `MPM_FEISHU_BITABLE_APP_TOKEN`，只放 app_token） |
| App ID | `MPM_FEISHU_APP_ID` |
| App Secret | `MPM_FEISHU_APP_SECRET` |
| 可选订阅源 | `MPM_SUBSCRIPTION_URL`、`MPM_SUBSCRIPTION_URL_2` … `MPM_SUBSCRIPTION_URL_4` |

> 主订阅 `MPM_SUBSCRIPTION_URL` 或本机文件已有的有效 `SUBSCRIPTION_URL` 会选择订阅模式。
> 只配置 `_2` 至 `_4` 仍从飞书建池，辅助订阅可为后续采集提供数据。

macOS（sudo 默认不继承环境变量，必须显式列出）：

```bash
sudo --preserve-env=MPM_FEISHU_BASE_URL,MPM_FEISHU_APP_ID,MPM_FEISHU_APP_SECRET \
  ./macos/scripts/bootstrap.sh
```

Windows（在同一个管理员 PowerShell 会话里设置后再运行）：

```powershell
$env:MPM_FEISHU_BASE_URL = 'https://example.feishu.cn/base/EXAMPLEappToken123456'
$env:MPM_FEISHU_APP_ID     = 'cli_example_app_id'
$secret = Read-Host 'Feishu App Secret' -AsSecureString
$env:MPM_FEISHU_APP_SECRET = [Net.NetworkCredential]::new('', $secret).Password
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\deploy.ps1
```

### 3.2 stdin JSON（值不进环境、不进 argv）

JSON 必须是**一个对象**，键名固定为 `feishu_url` / `app_id` / `app_secret`，
值必须是字符串；未知键忽略，空值视为未提供。文件只放本机，用完删除：

```json
{
  "feishu_url": "https://example.feishu.cn/base/EXAMPLEappToken123456",
  "app_id": "cli_example_app_id",
  "app_secret": "example-app-secret-not-real"
}
```

macOS：

```bash
sudo ./macos/scripts/bootstrap.sh < feishu-credentials.json
```

`sudo` 从终端读取密码，不占用 stdin；若终端不在或已被占用，先执行 `sudo -v` 再重定向。
若所用引导包装没有把 stdin 转给子进程，改用 §3.3 的直接 CLI 调用。

Windows：`deploy.ps1` 启动的 Python 子进程会继承 stdin，但 PowerShell 管道到 `.ps1`
再转发存在编码/缓冲差异。**可靠做法是直接调用 CLI**（CLI 的导入链依赖 PyYAML，
需要本机已有 Python 3.10+ 与 PyYAML；可先 `python -m pip install pyyaml`，
或复用引导装好的解释器）：

```powershell
# 1) 选解释器：用引导打印过的绝对路径，或新开管理员 PowerShell 后从 PATH 取
$py = (Get-Command python.exe -CommandType Application).Source
$env:MPM_PYTHON = $py
$env:MIHOMO_DIR = 'C:\mihomo'    # 自定义安装目录时改成同一绝对路径
$OutputEncoding  = [Text.Encoding]::UTF8

# 2) 把 JSON 文件作为 stdin 交给同一个 Python 流程
Get-Content -LiteralPath .\feishu-credentials.json -Raw -Encoding UTF8 |
  & $py .\scripts\deploy\__main__.py --platform windows
```

`Get-Content … | & python` 使用非 TTY 的标准输入，CLI 才会读 stdin JSON；在终端里
直接运行（不重定向）则进入交互提示。JSON 值保持 ASCII 可完全避开编码问题。

### 3.3 直接调用 CLI（两端兼容命令）

当包装脚本不方便转发 stdin、或需要精确控制参数时，直接调用同一个 Python 入口。
它和包装脚本走完全相同的流程与路径：

macOS（用引导准备好的专用 venv 解释器；`MPM_PYTHON` 是引导选定的解释器）：

```bash
sudo /usr/local/libexec/mpm-python-venv/bin/python3 \
  scripts/deploy/__main__.py --platform macos < feishu-credentials.json
```

Windows（PowerShell 不支持 `<` 输入重定向，用管道；上一条命令已是直接调用）：

```powershell
Get-Content -LiteralPath .\feishu-credentials.json -Raw -Encoding UTF8 |
  & $py .\scripts\deploy\__main__.py --platform windows
```

说明：

- macOS 引导的默认解释器是 `/usr/local/libexec/mpm-python-venv/bin/python3`
  （`scripts/deploy/platforms.py` 的 `MACOS_DEFAULT_PYTHON`，可被 `MPM_PYTHON` 覆盖）。
  引导可按需准备 venv 与 PyYAML。CLI 的导入链（`bootstrap.py` → `feishu_pool` →
  `node_provider.writer`）本身就 import PyYAML，所以**包括 `--dry-run` 在内都必须用
  带 PyYAML 的解释器**；安装阶段同样需要。尚未准备时先按引导的方式建 venv 并
  `pip install pyyaml`，或使用已装 PyYAML 的 Python 3.9+。
- Windows 引导会把选定的绝对解释器放进子进程环境 `MPM_PYTHON`，Install 阶段复用它；
  直接调用时请自己设置 `MPM_PYTHON`，否则 Install 阶段会按 PATH/常见目录重新挑选。
- 直接调用时安装目录由 `MIHOMO_DIR` 决定（Windows），请与后续管理命令使用同一个值。

### 3.4 凭据落地位置

引导在**访问校验、供应链、节点池都成功之后**才写本机凭据文件，并保留文件里原有的
其它键与注释（合并写入）：

| 平台 | 文件 | 权限 |
|---|---|---|
| Windows | `%MIHOMO_DIR%\subscription.env`（默认 `C:\mihomo\subscription.env`） | 写入前去掉继承、仅授予 SYSTEM/Administrators 完全控制；随后注册飞书任务时才给部署账号追加只读 |
| macOS | `/Library/Application Support/mihomo-proxy-management/state/subscription.env` | 0600、root:wheel |

写入内容包括 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_BITABLE_APP_TOKEN`
（解析后的真实 app_token，守护进程无需再解析 wiki）以及可选的 `SUBSCRIPTION_URL*`。首次引导生成本机控制器 `SECRET`，重跑时保留已有值。

---

## 4. 运行、`--dry-run` 与各开关的准确语义

### 4.1 流程顺序

```text
凭据解析 -> 地址解析 -> 飞书访问校验(读 + 写探针) -> 供应链下载/校验/暂存
        -> 节点池生成(可选) -> 写本机凭据文件 -> 调用平台安装器
```

任何一步失败立即停止，后续步骤不再执行；供应链失败会删除本轮已暂存的文件，
不会留下半套二进制。

### 4.2 `--dry-run` 的范围

`--dry-run`（Windows 参数名 `-DryRun`）覆盖 Python 引导流程。两个系统的入口包装在运行它前，
可能先准备解释器、venv 或 PyYAML。若需完全避免本机写入，用已有解释器直接调用 §3.3 的 Python 入口。

**会做**：解析地址；飞书应用鉴权；读多维表元数据与表列表；读「节点测速总表」行数；
构建节点池并检查 `--min-nodes`；读取供应链锁并打印将下载的资产 key。
**不会做**：不执行写探针（`write_ok` 保持 False，含义是「未验证写权限」，不是
「确认无权限」）；不下载、不解压、不暂存；不写节点池文件；不写凭据文件；不创建任何
目录；不调用安装器。

因此 dry-run 成功只证明**读权限与节点池门槛**（退出码 0），不能证明可写、也不能得到
可用安装。dry-run 仍会访问飞书网络，并读取仓库内的 `supply.lock.json`。

### 4.3 各开关

| 开关（Mac 引导 / Windows） | 语义 |
|---|---|
| `--dry-run` / `-DryRun` | 见 §4.2；写探针跳过，判定只看读权限 |
| `--no-start` / `-NoStart` | **照常**安装二进制、渲染配置、注册计划任务/LaunchDaemon，只是**不启动服务进程**。Windows 会把它透传成 `install.ps1 -SkipStart`；核心任务仍在开机时启动 |
| `--skip-supply` / `-SkipSupply` | 跳过下载/校验/暂存，复用已有的暂存目录。若暂存里缺少必需文件，安装阶段仍会失败（Windows：`mihomo.exe`、`GeoIP.dat`、`GeoSite.dat`、`geoip.metadb`；Mac：暂存的 `mihomo`） |
| `--skip-install` / `-SkipInstall` | 走到「写凭据文件」为止，不改配置、不注册服务/任务。适合只验证飞书访问与供池 |
| `--min-nodes N` / `-MinNodes N` | 纯飞书供池时节点池的最低节点数（默认 1）。低于门槛退出码 5，旧池保留。主订阅已配置时本项不生效 |
| `-InstallDir <路径>`（仅 Windows） | 自定义安装目录，引导会规范化为绝对路径，经 `MIHOMO_DIR` 传给后续所有阶段 |

Windows 传 `-NoStart` 时，如果安装文件内容未变，运行中的既有核心可以继续运行。
如果需要更换文件，引导会先停止既有核心，保留开机任务。

### 4.4 退出码

`deploy/__main__.py` 的退出码就是流程的退出码：

| 码 | 含义 |
|---|---|
| 0 | 成功（dry-run 时为「读校验 + 门槛通过」） |
| 2 | 用法 / 凭据缺失或不可用 / 地址非法 / 供应链锁不可读；交互中断为 130 |
| 3 | 飞书访问校验失败（鉴权、读表、写探针） |
| 4 | 供应链失败（下载、大小/摘要、解压、暂存） |
| 5 | 节点池失败（表缺失/读失败、无行、低于 `min-nodes`） |
| 6 | 写凭据文件失败或平台安装器返回非 0 |

`scripts/feishu_pool.py` 单独运行时：0 成功，2 用法/凭据/app_token，3 总表缺失或
读失败，4 低于门槛（保留旧文件），5 写文件失败。

---

## 5. 运行后状态与重试

### 5.1 Windows

安装目录（默认 `C:\mihomo`）：

```text
C:\mihomo\config.yaml                     # proxy-providers.subscription = file: ./providers/feishu-pool.yaml
C:\mihomo\providers\feishu-pool.yaml      # 飞书节点池
C:\mihomo\subscription.env                # 本机凭据（不在 Git 内）
C:\mihomo\logs\                           # mihomo / sync / pipeline 日志
```

用了自定义 `-InstallDir` 时，引导只在**当前进程**设置 `MIHOMO_DIR`，不会改系统环境变量；
之后在新开的 PowerShell 里运行管理脚本前，请先 `$env:MIHOMO_DIR = 'D:\mihomo'`
（或给支持该参数的脚本显式传 `-InstallDir`），否则 `Get-InstallDir` 会退回 `C:\mihomo`。

计划任务：

| 任务 | 身份 | 频率 | 说明 |
|---|---|---|---|
| `MihomoProxy` | `SYSTEM` / Highest | 开机（延迟 15s）+ 崩溃重启 | 核心服务，独立于两个飞书任务 |
| `MihomoFeishuBitableSync` | **当前部署账号** Interactive / Limited | 默认每 30 分钟 | 先同步后切换；任一阶段非 0 即任务失败 |
| `MihomoNodePipeline` | 继承上述 sync 任务身份 | 每小时（注册后 +5 分钟开始） | collect → pool → ops → provide |

两个飞书任务**不是 SYSTEM、不是 Highest**，注册前会用 `icacls` 给部署账号追加
`subscription.env` 只读、`logs`/`providers` 的 `Modify`（含继承）权限。它们以
Interactive 身份运行，**只有该用户登录时才执行**；核心 `MihomoProxy` 不依赖登录。
如果手工注册的 sync 任务是提升/SYSTEM 身份，pipeline 注册会拒绝继承并失败。

### 5.2 macOS

```text
/Library/Application Support/mihomo-proxy-management/config.yaml
/Library/Application Support/mihomo-proxy-management/providers/feishu-pool.yaml
/Library/Application Support/mihomo-proxy-management/state/subscription.env   (0600)
/Library/Application Support/mihomo-proxy-management/logs/
```

4 个 LaunchDaemon（root）：core、boot-switch、feishu-sync（默认 600s）、node-pipeline
（默认 3600s）。`--no-start` 只影响服务是否立刻启动，守护任务仍会安装/注册。

### 5.3 重试

- 失败后修复原因，**原样重跑同一条命令**即可；流程在安装前失败时不会留下半套安装，
  供应链失败会回滚本轮暂存。
- 重复运行会跳过内容未变的文件复制；
  计划任务以 `-Force` 重新注册；凭据文件按键合并。
- 纯飞书机器在「节点测速总表」为空时会在节点池步骤以退出码 5 停止，这属于预期：
  先让具备订阅/本机补充的机器把总表填起来（或人工写入有效行），再重跑引导。
- 运行期飞书暂时不可用时，守护任务按各自周期重试；`FEISHU_BITABLE_APP_TOKEN`
  缺失会让流水线以退出码 2 每轮失败，直到重跑引导或补齐凭据文件。

---

## 6. 节点池刷新机制与取数失败

- 构建规则（`scripts/feishu_pool.py`）：总表每一行都是候选；丢弃广告/结构非法行，
  语义重复去重，重名加 `[2]`；输出原子写入，先写临时文件再替换。
- **Windows**：每次 `MihomoNodePipeline` tick 的 `pool` 步骤重跑 `feishu_pool.py`，
  重建 `providers/feishu-pool.yaml`；内容哈希记在 `logs/pool.sha256`，只有成功
  `PUT /providers/proxies/subscription` 热加载后才更新标记，失败下轮重试。
- **macOS**：node-pipeline 每轮刷新同一路径的节点池，成功热加载后在流水线状态目录写入 `pool.sha256`。
- **取数失败保留旧池**：总表读失败（退出码 3）、低于门槛（4）、写失败（5）都不会
  覆盖已有 `feishu-pool.yaml`；热加载失败时文件可能已更新，但运行中的核心继续用旧内容，
  标记不推进，下一轮再试。
- **纯飞书跳过空采集**：没有 `SUBSCRIPTION_URL_n`、也没有本机可采集补充输入时，
  pipeline 记 `SKIP collect` 后照常执行 pool/ops/provide（不是失败）。有订阅或本机
  补充输入时照常采集；采集的非 0 退出码一律按失败处理。
- 池模式识别只认受管路径：`proxy-providers.subscription` 必须是 `type: file` 且指向
  `./providers/feishu-pool.yaml`。Windows 也接受安装目录下的同名绝对路径。

手工重建（在克隆目录内；`--dry-run` 只打印统计不写文件）：

```powershell
# Windows
python .\scripts\feishu_pool.py --env-file C:\mihomo\subscription.env `
  -o C:\mihomo\providers\feishu-pool.yaml --min-nodes 1
```

```bash
# macOS（使用引导准备的解释器；路径含空格，必须加引号）
sudo /usr/local/libexec/mpm-python-venv/bin/python3 scripts/feishu_pool.py \
  --env-file "/Library/Application Support/mihomo-proxy-management/state/subscription.env" \
  -o "/Library/Application Support/mihomo-proxy-management/providers/feishu-pool.yaml" \
  --min-nodes 1
```

---

## 7. 飞书权限校验与常见错误

### 7.1 推荐的成套校验

1. 先跑 `--dry-run`：应看到 `bootstrap dry-run supply assets=…`、
   `bootstrap dry-run pool nodes=N`，最后 `bootstrap: OK`（stderr）。退出码 0。
2. 再跑一次只验证、不安装的完整写探针：

   ```bash
   # macOS
   sudo ./macos/scripts/bootstrap.sh --skip-supply --skip-install
   ```

   ```powershell
   # Windows
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\deploy.ps1 -SkipSupply -SkipInstall
   ```

   它会真实写入「部署记录」本机行、生成节点池、写凭据文件，但不装服务。
   成功后多维表里应出现/更新一行「部署记录」（主机名 = 本机名）。
3. 正式运行（去掉 `--skip-*`）。读、写、供池、安装全部通过才算完成。

`verify.py` 的提示与问题串一一对应：只报告失败步骤和稳定类别（`step:kind`），
不打印响应正文、token 或 secret；出现 `kind(detail)` 时 detail 也只保留
`[A-Za-z0-9_=,-]` 且不超过 40 字符的诊断码。

### 7.2 常见错误表

| 现象（stderr / problems） | 含义 | 处理 |
|---|---|---|
| `missing credentials: …`（退出 2） | 三个值没有凑齐 | 设置 §3.1 环境变量、stdin JSON 或交互输入 |
| `credentials on stdin are unusable: stdin_invalid`（2） | stdin 不是一个对象、字段不是字符串或不是 JSON | 检查 JSON 格式与键名 |
| `invalid Feishu Bitable address: empty/not_a_url/unsupported_scheme/not_feishu_host/has_userinfo/unknown_path/bad_token`（2） | 地址不合法 | 粘贴 `/base/<app_token>` 或 `/wiki/<node_token>`；只支持 `feishu.cn`；去掉账号信息 |
| `access:wiki:*` + wiki 提示（3） | 知识库节点解析失败 | 开通 `wiki:wiki:readonly`，或直接换成多维表 `/base/…` 地址 |
| `access:auth:*` + 凭据提示（3） | App ID/Secret 错误或应用未发布 | 核对凭据、发布应用版本 |
| `access:base:*` / `access:tables:*` + `添加文档应用` 提示（3） | 应用没有被加进这张表，或只有只读 | 在多维表「… → 添加文档应用」加入本应用，授予可编辑；确认 `bitable:app` 读写 |
| `access:write:*` + 编辑权限提示（3） | 写探针失败（写「部署记录」被拒） | 把应用设为可编辑者；确认不是只读权限 |
| `access:nodes:*`（软失败） | 「节点测速总表」读失败 | 不影响已配置订阅的安装；纯飞书供池会在下一步以 5 失败 |
| `pool:no_nodes_in_table` / `pool:table_missing`（5） | 总表缺失或没有可用行 | 先填总表（或等其它机器采集）后重跑；确认表名完全一致 |
| `pool:below_min_nodes`（5） | 可用节点少于 `--min-nodes` | 补数据，或在明确知情时下调门槛；旧池不会被覆盖 |
| `bootstrap: unexpected SupplyError`（2） | 仓库供应链锁缺失/损坏（锁在下载前读取，早于 `supply:*` 分类） | 确认 checkout 完整、没有本地改动 |
| `supply:download_failed`（4） | 下载失败 | 检查网络/代理；GitHub 可设 `MPM_GITHUB_MIRROR`（校验不变） |
| `supply:size_mismatch/digest_mismatch`（4） | 内容与锁定值不符 | 不要重试绕过；确认锁与上游发布版本 |
| `supply:member_missing/member_ambiguous/archive_invalid/archive_too_large`（4） | 压缩包成员或解压异常 | 报告上游资产变化；检查磁盘/内存 |
| `supply:stage_failed`（4） | 写入暂存目录失败 | 检查暂存目录权限与磁盘空间 |
| `install:exit_N` / `install:<异常类名>`（6） | 平台安装器失败 | 看安装器 stderr 与安装日志；修好后重跑 |
| `secrets:<kind>`（6） | 写凭据文件失败 | 检查目录权限/磁盘；Windows 上确认不是文件占位 |
| Windows 提示 Python 3.10+ 未找到 | `-SkipPython` 且环境无合格解释器 | 去掉 `-SkipPython` 让引导安装，或先装 Python 3.10+ |
| Windows 提示 PyYAML 安装失败 | pip 网络/索引问题 | 设 `PIP_INDEX_URL`、检查代理后重跑 |
| Windows 任务注册被拒绝（提升身份） | sync 任务被手工注册成提升/SYSTEM | 以当前用户 Limited 重新注册 `MihomoFeishuBitableSync` 后重跑引导 |
| 运行期 `app_token_missing: FEISHU_BITABLE_APP_TOKEN`（流水线退出 2） | 凭据文件缺 app_token | 重跑引导（会写入解析后的 token），或补齐该键 |

---

## 8. 安全与边界声明

- 凭据只经环境变量 / stdin / 隐藏交互输入进入流程，**永不出现在命令行参数**；
  日志与 `--json` 摘要经过脱敏，不输出 secret、URL 原文或真实 app_token。
- 真实凭据只落在本机凭据文件（Windows ACL 受限、macOS 0600），仓库内只有模板；
  `subscription.env` 被 `.gitignore` 递归忽略。
- 已用临时目录、合成节点及锁定版本验证配置；Windows 计划任务与 ACL 的实际注册，
  以及 macOS 生产 LaunchDaemon 安装，需要目标机器验收。
- 本指南不包含任何真实租户、表格地址、App ID/Secret 或订阅链接。

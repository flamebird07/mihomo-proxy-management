# 源1桌面App提取检查

2026-11-04，只读检查，尚未成功导出完整节点。

- 截图对应本机窗口wmsxwd；安装版本1.44.1，目录 `C:\Program Files (x86)\wmsxwd`。
- 主程序PID27624，AtlasCore_amd64.exe子进程PID33060。
- 内核包含Mihomo接口和Atlas专用加密模块；本机控制器19239对/version、/configs、/proxies均返回401。
- App配置目录 `C:\Users\Administrator\AppData\Roaming\io.wmsxwd.app\wmsxwd`。
- shared_preferences.json仅有地区/地理数据库更新/引导信息，无节点配置。
- 日志文件使用AT LCTR02对应的二进制加密头（实际连续字节41544c4354523032）。
- `.secure_storage.dat`为base64包裹的加密内容；没有把认证或账号内容输出。
- 静态符号确认atlas.Decrypt、ParseHandoffFrame、SetKeys、deriveKey；主函数含io.ReadAll和ParseWithBytes路径，不能据此假定存在明文配置文件。
- 常见AES存储解密尝试未取得有效明文；没有完整服务器/协议/认证参数可用于独立拨号。
- 本机providers缓存未找到截图所示“美国01-×1-客户端”等实际客户端节点。

结论：不能将截图的名称、延迟或内核API存在视为提取成功。尚需定位App传给AtlasCore的密钥/配置交接格式，或找到App受支持的导出入口。未绕过401读取控制器、未改变源1App选择、未重启任何现有代理、未改系统代理。现有主系统仍使用源3独立GPT，系统代理127.0.0.1:7890。

## 后续静态分析

- 通过解析Dart对象字符串，找到可解开内置资源的AES参数。解开c.dat（API、请求头、订阅参数配置）以及a.dat/b.dat（HTML资源）。这不等于解开账号存储或节点配置，敏感资源只保存在私有工作目录。
- 通过Windows允许的进程有限查询，确认AtlasCore实际启动参数为 `-d C:\Users\Administrator/.config/wmsxwd -f -`。完整配置由标准输入传入。
- 工作目录config.yaml仅311字节、proxies为空、控制器指向默认9090；它不是当前19239控制器的实际配置，不能将9090主系统返回的节点归为源1提取结果。
- Atlas解密函数调用AES.NewCipher和cipher.NewCTR；使用帧中16字节IV。交接帧解析调用HMAC校验并将32字节和16字节材料传给SetKeys。
- Windows进程读取权限检查明确失败：OpenProcess(QUERY_INFORMATION|VM_READ, PID33060)返回错误5。未提权或换工具重试此权限。

已准备 `scripts/run-source1-readonly-extract.ps1` 和 `scripts/extract_source1_readonly.py`，需用户自行在管理员PowerShell运行。
脚本只读取安装路径吻合的AtlasCore，不注入、不停止进程、不改网络；只寻找保留的完整YAML，最多导出一个带“客户端”名称的节点，拒绝无认证字段、回环地址或依赖dialer-proxy的节点。
单节点文件位于 `C:\mihomo\source1-extraction\single-node.private.yaml`，结果文件为 `readonly-extraction-result.json`。
如果内核已回收配置文本，脚本可能找不到节点，会如实报告exported=0。提取器的2项离线解析测试通过，管理员环境的真实提取尚未运行。
当前仍没有完整节点，尚未执行源1独立出口/Grok/GPT验证，也没有批量导出。

## 首次管理员运行后的修订

首次运行扫描44MB，未找到旧提取器要求的行首YAML。该结果不能证明没有节点；旧提取器遗漏JSON和残留加密帧。
新版支持嵌入JSON代理数组、非行首YAML以及ATLS帧。静态分析确定帧头为ATLS（不是日志的ATLCTR02），后接16字节CTR IV。
`atlas-read-layout.json`记录当前安装内核的SHA256和解密状态RVA；只在二进制哈希吻合时读取32字节内核密钥，密钥仅在提取器内存中使用，不输出、不保存。
新版结果包含格式标记计数和key_ready，便于区分解析遗漏、密钥未初始化或文本已回收。最多导出一个完整客户端节点，仍不保存内存转储。
4项离线测试通过，新增嵌入JSON和ATLS帧还原测试；需用户再次在管理员窗口运行同一PowerShell脚本，真实提取尚待验证。

## 美国客户端样本实测

管理员运行成功：在4MB扫描范围中从ATLS帧恢复35个完整节点，本次仅导出美国01-×1-客户端。
协议SS，cipher为2022-blake3-aes-128-gcm；标准Mihomo v1.19.28可直接加载，不需要AtlasCore。
独立端口17996/19996测试完成后进程和临时目录均清理，未操作源3。

- 实际出口：美国/加利福尼亚/圣何塞。
- ChatGPT trace GET200；robots.txt HEAD200。
- Grok首页GET403，响应头cf-mitigated:challenge；干净隔离浏览器同为403，标题Just a moment…。这是验证页，不能认定Grok已正常可用，也不能据此称节点连接失败。
- 完整验证门槛尚未通过，尚未批量导出。

已增加单样本地区选择：管理员运行同一脚本并传 `-SampleRegion SG`，只导出新加坡客户端样本至single-node-新加坡.private.yaml，不覆盖美国样本。5项解析测试通过。
验证命令：`python scripts/validate-source1-sample.py --node-file single-node-新加坡.private.yaml`。

## 新加坡客户端样本实测

已回读single-node-新加坡.private.yaml，节点为新加坡01-×1-客户端、SS2022。
标准Mihomo隔离拨号成功，实际出口新加坡；ChatGPT trace GET和robots.txt HEAD均200。
Grok首页GET403，明确返回cf-mitigated:challenge；隔离浏览器同样403且显示Just a moment…。不把验证页算作正常使用成功，也不把它等同于节点无法拨号。
同轮通过源3独立桥接访问相同Grok首页得到200、无challenge响应头，确认不是所有路径都遇到这一结果。
新加坡样本未满足Grok/GPT/出口全部通过的批量导出前提，因此未批量导出。
主GPT选择与系统代理在操作前后完全一致；临时源1内核和浏览器均已退出。
结果：私有目录sg-final-acceptance.json。用户要求后续尽量自主处理，现有文件验证和排查无需其操作；仍须遵守Windows已明确拒绝的内存读取权限，不另寻执行器绕过。

## 用户明确授权继续批量提取

最新指令取消“Grok通过后才能批量导出”的阶段门槛。已加入显式 `-All` / `--all`：
从含客户端节点的同一配置中导出全部完整、无回环依赖的节点，去除相同重复项，拒绝同名冲突；不覆盖已有导出。
目标文件为私有目录all-nodes.private.yaml。此前观察到35个完整节点，但磁盘仅保存两个单节点样本，不能把35计作已批量导出。
7项提取器测试通过；批量内存读取仍需管理员窗口运行，当前会话不能绕过此前的Windows错误5。
管理员命令：`& 'C:\mihomo-proxy-management\scripts\run-source1-readonly-extract.ps1' -All`。

## 批量导出完成与独立加载验收

已回读all-nodes.private.yaml：35个节点、35个唯一名称，必需字段完整。
协议统计：Trojan19、SS3、Hysteria2 9、AnyTLS3、VLESS1。
标准Mihomo配置校验通过，随后在独立目录/端口启动标准核心，控制器确认35个节点全部加载。
验证结束后已停止该临时内核并清理临时目录；主GPT仍为源3独立GPT，系统代理仍为127.0.0.1:7890，前后快照一致。
此次确认的是完整导出和标准内核兼容性，不代表35个节点全部存活，也未将它们加入主GPT或飞书自动同步。
实际拨号验证仍以美国/新加坡样本记录为准。验收文件：私有目录bulk-load-validation.json。

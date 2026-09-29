# MuseAutoApprove 接入

采用 [bytehola/muse-guardian 的 MuseAutoApprove](https://github.com/bytehola/muse-guardian/tree/681759cf9633ea740af5e64037b667b52bb44a1b/MuseAutoApprove)，安装脚本固定已审阅的源码提交，首次 npm 安装生成 lockfile，恢复复用持久缓存。它只处理当前 Muse VM 的 sentinel `egress.approvals` 队列中可识别的网络审批；**不会续期 HTTPS_PROXY，不会阻止 VM 重启**。审批 daemon 和 axonhub 一样依赖平台唤醒后的恢复。

用户已要求自动审批时直接接入，不重复询问是否安装；缺少账号凭据或平台操作权限时才补齐。默认 `allow_once` 自动批准本次网络请求；用户要求按域名永久允许时设置 `AXH_MAA_DECISION=allow_always`。不自动批准 reader grant、执行命令、文件上传等非网络授权。适配层只接受包含明确网络目标字段的审批，未知 schema 保持 pending；在真实 Muse 版本上核对脱敏后的字段，再适配，不根据自由文本猜审批类型。

## 安装与接管

1. 使用持久路径里的 Node **22+** 与 npm；记录 `AXH_NODE_BIN` 的绝对路径到 stack/env。已有 Node 符合要求就复用，不盲目装系统包。它依赖 sodium-native，本机架构必须有可用的预编译包或编译工具；安装失败保留日志，不能宣称 MAA 可用。
2. 在技能目录运行主脚本 install 后：

```bash
bash "$AXH_HOME/axh.sh" maa-install
```

默认安装到 `$AXH_HOME/MuseAutoApprove`，data/ 和 log/ 权限 700；不会覆盖现有账号、cookies 或 VM 配置。已装在其他目录时先核对旧进程及其启动入口，再停旧启动器，复制现有 data/ 到新目录（保留 600 权限），避免双实例审批。不要直接复制未核实的旧 PID；启动器会校验 cmdline 后清掉失效或复用的 PID。

3. 复用已有可用会话或通过本机私密输入配置 `data/credentials.json`：

```bash
python3 - <<'PY'
import getpass, json, os
from pathlib import Path
os.umask(0o077)
p = Path(os.environ['AXH_HOME']) / 'MuseAutoApprove/data/credentials.json'
email = input('Muse email: ')
password = getpass.getpass('Muse password: ')
p.write_text(json.dumps({'email': email, 'password': password}))
p.chmod(0o600)
PY
```

无交互终端时让用户通过可用的秘密输入渠道落盘，不要求把密码贴到公开对话，不打印 credentials/cookies/token 文件。上游支持自动登录和会话重连，但平台的 MFA、验证码或协议变化需要真实验证，不能承诺永久登录。

4. 核对当前 VM ID 后编辑 `$AXH_HOME/env`：

```bash
AXH_MAA_ENABLED=1
AXH_MAA_DECISION=allow_once
MUSE_VM_ID=<当前部署所在VM的ID>
AXH_NODE_BIN=/持久路径/bin/node
# AXH_MAA_PROXY=edge       # 默认直连；访问 muse.ai 必须经平台代理才打开
```

VM ID 必须来自当前平台 VM 信息，不使用“首个 VM”自动猜测；重建后确认该 ID 是否改变。恢复只保留用户确认的绑定，不在错误账号/VM 上审批。

`AXH_MAA_PROXY=edge` 时 MUSE_PROXY 指向本地 axproxy，WS 重连与周期 token touch 的新连接都会读取最新 proxy.json。凭据变化时仅在 daemon 不存活或成功轮询心跳过期时整体重启 MAA supervisor，清除退避；健康 daemon 的 WS 保持。若直连 Muse 可用，保持默认，减少对轮换代理的依赖。初次下载/登录若需要审批，可能仍需要用户手动放行一次；不能让尚未启动的审批器批准自己的启动依赖。

5. 通过 `start` 或新会话 `session-restore` 拉起。适配层保护上游输出中的代理地址和 token 字段。不要绕过适配层用含真实 MUSE_PROXY 的上游 `--check`，上游会打印完整代理 URL。

## 验收、停止、恢复

- `status` 的 `MAA successful poll: recent` 表示 daemon 存活且近期成功读取过审批列表，**不表示 UI 中所有审批都已覆盖**；`run/maa.ok` 过期会触发健康失败。
- 检查 `logs/maa.log` 和 `MuseAutoApprove/log/daemon-log.ndjson` 中的 connected、heartbeat、decided；不使用错误日志持续增长代替成功心跳。
- 用用户已授权的测试域名触发一次外联审批，核实实际决定和作用 VM；没有真实审批样本时只能报告“已安装/已连接，自动批准未实测”。出现 skipped unknown/non-network 时检查脱敏字段，不清空过滤条件。
- `axh.sh stop` 停整套；单独停止 MAA 可创建 `MuseAutoApprove/data/muse-daemon.stop`，上游下一轮退出，监督器不再拉起。手动恢复 MAA 时才移除该标记。
- 子进程崩溃由 supervisor 恢复，成功审批轮询超过 120 秒未更新由 watchdog 累计失败处理；VM 全灭由平台新会话恢复。
- 实测范围分开报告：依赖加载、离线适配层测试、真实登录、真实审批、真实重建。账号失效和平台 API 改版不能靠重启证明修复。

## 审批覆盖与诊断

2026-09-30 追加的现场复盘：30 条审批历史均含 `registered_command` 和 `host/port/scheme`，无 destination_domain/类型字段；其中一条命令触发的 HTTPS 审批待批 21 秒，daemon 在期间两轮均看到 pending=0。该记录支持“这条命令审批不在当时 daemon 可见队列中”，不能靠缩短轮询或放宽字段过滤解决，也不能据此推断所有版本的完整平台路由规则。

当前覆盖边界：

| 观察到的对象 | 处理 |
|---|---|
| sentinel pending 中有明确网络目标、无命令字段的受支持 schema | 按 allow_once / 已配置的 allow_always 决策 |
| 含 registered_command，即使同时有 host 或 destination_domain | 显式排除命令授权，不自动决定 |
| 只有 host/port/scheme、类型和来源尚未确认 | 保持 pending；不能单凭 host 把它升级为网络授权 |
| UI 仍在待批，但 sentinel pending 为空 | 排查队列/可见范围，而不是把健康心跳当审批成功 |

`status` 展示当前 daemon 运行期的队列可见数、可处理数、命令/其他跳过数及成功决定 RPC 次数。`run/maa.coverage.json` 只存数量与时间，不记录命令、目标或凭据。计数是诊断证据，不是端到端审批验收；返回成功的决定还需用历史中的结果确认。

用户问“为什么没自动批”时：

1. 用当前 VM 绑定核对 daemon 可见的 pending，以及只读 `egress.approval.history` 中对应事件；通过已有 RPC 客户端查询，原始返回保存在 600 临时文件，不把命令原文或凭据打印到对话。
2. 对比 requested_at/decided_at 与轮询时间；历史含 registered_command 时明确说明命令审批不在当前功能范围。不能用已被用户手动允许的记录冒充 daemon 成功。
3. 若确有同 ID 出现在 pending 而被过滤，提取字段名、类型和脱敏结构再适配；若 pending 始终不可见，仅给 networkDestination() 加 host 字段不会接通另一个队列。
4. 没有真实可复现的审批样本就报告“轮询正常，自动决策未验证”；不要反复发任意外联请求直到平台偶然弹窗。

若后续要求命令自动审批，必须先取得真实通道接口与样本，并单独确定授权范围、目标/参数白名单和命令副作用规则。当前用户对网络外联自动批准的授权，不等于任意命令执行授权；本技能不猜测新 endpoint，不批量放开命令权限。

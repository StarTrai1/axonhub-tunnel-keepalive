# MuseAutoApprove 接入

采用 [bytehola/muse-guardian 的 MuseAutoApprove](https://github.com/bytehola/muse-guardian/tree/681759cf9633ea740af5e64037b667b52bb44a1b/MuseAutoApprove)，安装脚本固定已审阅的源码提交，首次 npm 安装生成 lockfile，恢复复用持久缓存。审批行为保持上游原生：轮询当前 Muse VM 的 `egress.approvals`，对返回的所有审批 ID 决策；**不会续期 HTTPS_PROXY，不会阻止 VM 重启**。审批 daemon 和 axonhub 一样依赖平台唤醒后的恢复。

用户已要求采用上游默认自动审批方式时直接接入，不重复确认。默认 `allow_always + destination_domain`，失败回退 `allow_once`；只批本次可显式设置 `AXH_MAA_DECISION=allow_once`。这意味着队列内审批均交给上游处理，不额外按 `destination_domain`、`host`、类型或 `registered_command` 字段过滤。`destination_domain` 是决策 scope，不是待审批记录必有的字段；带命令来源的网络审批也不能仅凭 `registered_command` 被排除。

不再注入 `maa-adapter.cjs`、包装 RPC 或改写上游返回值；健康监测从进程外只读上游日志。默认策略会请求按目标域永久允许；服务端 scope 不支持时上游会尝试单次允许，包括上游提及的 reader grant 场景。不要把它描述为“仅处理本地过滤过的网络类型”，也不新增其他审批通道或通用命令执行 API。

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

4. 核对当前 VM ID 后编辑 `$AXH_HOME/env` 中真正生效的赋值行，不能只改注释模板；避免同名多行导致后面的值覆盖前面的值：

```bash
AXH_MAA_ENABLED=1
AXH_MAA_DECISION=allow_always
MUSE_VM_ID=<当前部署所在VM的ID>
AXH_NODE_BIN=/持久路径/bin/node
# AXH_MAA_PROXY=edge       # 默认直连；访问 muse.ai 必须经平台代理才打开
```

VM ID 必须来自当前平台 VM 信息，不使用“首个 VM”自动猜测；重建后确认该 ID 是否改变。恢复只保留用户确认的绑定，不在错误账号/VM 上审批。

`AXH_MAA_PROXY=edge` 时 MUSE_PROXY 指向本地 axproxy，WS 重连与周期 token touch 的新连接都会读取最新 proxy.json。凭据变化时仅在 daemon 不存活或成功轮询心跳过期时整体重启 MAA supervisor，清除退避；健康 daemon 的 WS 保持。若直连 Muse 可用，保持默认，减少对轮换代理的依赖。初次下载/登录若需要审批，可能仍需要用户手动放行一次；不能让尚未启动的审批器批准自己的启动依赖。

5. 通过 `start` 或新会话 `session-restore` 拉起。实际启动为：

```bash
node muse-daemon.cjs --loop 10000 --always --scope destination_domain --fallback-once
```

脚本保留 supervisor、停止标记、当前 VM 校验与 edge 模式的本地转发代理；没有 `--require` 注入。direct 模式清掉 MUSE_PROXY，edge 模式只传无凭据回环 URL；不把短期代理密码直接交给 daemon。上游日志保留原生格式，仅本机私密存储；不要回显原始账号、token、完整命令或含凭据的代理 URL，也不要用带真实代理密码的原生 `--check`。

## 旧版迁移

先用旧脚本 `stop`，从新版技能目录执行 `install` 复制全部脚本，保留 MuseAutoApprove 的账号/session/data、既有 owner、tunnel token 和正式域名。将 env 的有效策略设为 `AXH_MAA_DECISION=allow_always`；安装器保留已有 env，因此旧的显式 `allow_once` 不会自动改变。旧 `maa-adapter.cjs` 即使还留在部署目录也不再加载，`maa.ok`/`maa.coverage.json` 不再参与健康判断。

确认没有外部启动器或 NODE_OPTIONS 继续注入旧 adapter。先从新平台会话执行 `set-proxy`（edge 模式），再 `start`；此前手动创建的 MAA 单独停止标记只有在用户要求恢复 MAA 时移除。查看本轮 `daemon_start` 的 `decision/scope/fallbackOnce`，核对真实进程参数无 `--require`，不要仅靠 env 文件判定切换完成。

## 验收、停止、恢复

- `status` 的 `MAA successful poll: recent` 要求 daemon 存活，且本轮启动后最近 120 秒内有原生 `heartbeat` 或 `pending_found`；连接成功、日志 mtime 更新、错误日志增长都不算成功轮询。
- `maa-status.py` 只读 `log/daemon-log.ndjson` 最近 1 MiB，按 `run/maa.started` 排除旧进程记录；展示实际策略、最近待批数量、decided/decided_fallback、approved 状态和错误数。窗口计数不是进程全生命周期统计，pending=0 不是审批已完成。
- 用用户已授权的实际外联请求核对 `pending_found → decided/decided_fallback` 的同一 approval_id，再与 `egress.approval.history` 的最终状态比对。不应把其他审批者的决定记在 MAA 名下。日志报 allow_always 或 status=approved 不证明永久规则已经落盘，尤其 applied_rules=0 时需单独验证。
- `axh.sh stop` 停整套；单独停止 MAA 可创建 `MuseAutoApprove/data/muse-daemon.stop`。上游下一轮退出，监督器不再拉起；手动恢复 MAA 时才移除该标记。
- 子进程崩溃由 supervisor 恢复，成功轮询过期由 watchdog 累计失败处理；VM 全灭仍由平台新会话恢复。代理更新只对新连接生效；WS 存活不代表之后 token touch 无需新凭据。

## 现场复盘与排障边界

2026-09-30 切换复盘提供了新的成功证据：去掉自研 adapter、切到上游默认策略后，daemon 发现并批准了 4 条积压审批。旧版要求 destination_domain、排除 registered_command 的做法偏离上游，已撤除；不再以该字段猜测授权类型。

同一次直连测试的审批含 `host`、`registered_command`，无 destination_domain，约 7 秒后获批；daemon 日志无对应 ID，历史中决定者身份未确认。因此可以报告该次外联获批，**不能归功于 MAA，也不能仅凭数字 decided_by 断言是平台策略引擎**。此前统计勘误：30/30 有 host，25/30 有 registered_command；21 秒窗口属于 hatch.metaaivm.com 的调查请求，不是本机公开域名。

用户问“为什么没自动批”时，先比对当前 VM、同一时间窗的 daemon 日志和只读审批历史：

1. 有 pending_found 但无成功决定：查看 decide_error/fallback_error，核对实际 decision、scope 和 fallback 参数。
2. 历史有请求而 daemon 无同 ID：检查队列可见范围、请求链路与其他审批者；不要恢复 schema 过滤，或猜测并调用另一个审批 endpoint。
3. daemon 决策与历史不一致：报告竞态或决策归属尚未确认，保留时间戳与脱敏记录。
4. 新域名请求未产生审批：不能据此验收自动批准；正常请求可能已被规则覆盖或经不同出站路径。

复盘中“经代理普通网站不弹卡片、直连出现卡片”是该环境观察；代理/直连是否对应固定隔离通道仍待受控验证，不能把“所有含 registered_command 的记录永远不进队列”写成平台契约。未取得真实样本时报告“已连接并轮询，自动批准未实测”，不靠反复任意外联制造审批。

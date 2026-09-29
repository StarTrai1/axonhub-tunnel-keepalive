---
name: axonhub-tunnel-keepalive
description: 在 Linux 主机、Muse 沙盒或容器中部署 StarTrai1/axonhub fork 发行包，通过 Cloudflare Tunnel 提供 HTTPS，并配置进程监督、看门狗、平台新会话恢复与 MuseAutoApprove 外联审批；处理 Muse 需要经 HTTP CONNECT 代理连接 Cloudflare edge 的部署故障。
---

# AxonHub + Cloudflare Tunnel

部署 [StarTrai1/axonhub fork Releases](https://github.com/StarTrai1/axonhub/releases) 的 Linux amd64/arm64 包。AxonHub 默认只监听 `127.0.0.1:8090`，Cloudflare 终结公网 HTTPS。状态、数据、令牌、代理配置、本地出站转发代理和可选的 MuseAutoApprove 和全部恢复脚本放在 `$AXH_HOME`（默认 `~/axonhub-stack`，必须是持久目录）。

## 工作顺序

先检查已有实例、`$AXH_HOME/env`、owner 初始化状态和平台的持久盘位置；复用已有数据与授权。只询问缺失的域名/隧道令牌、owner 信息。已初始化的实例不要重新初始化。

- 正式使用：Cloudflare 中创建 Cloudflared Tunnel，将 Public Hostname 的 service 指向 `http://127.0.0.1:8090`；准备 tunnel token。
- 临时测试：没有域名/令牌时用 `AXH_MODE=quick`，随机地址可能随重启改变，无 SLA。
- Muse 部署必须接入 [平台恢复与凭据刷新](references/muse.md)；用户要求自动审批时接入 [MuseAutoApprove](references/muse-auto-approve.md)，沿用上游默认 allow_always + destination_domain、失败回退单次；不注入自研 schema 过滤，按原生日志和历史核实实际审批。
- Muse、出站受限、直连失败或报告 `hard_fail=true`：读取 [Muse 代理与重建](references/muse.md)。不要仅根据直连 precheck 断言环境无法部署。

从技能目录执行；升级已有脚本时先用旧脚本 `stop`，再安装新脚本，以便旧监督进程退出：

```bash
export AXH_HOME="$HOME/axonhub-stack"
bash scripts/axh.sh install          # 把全部辅助脚本一起持久化，补装发行包
bash "$AXH_HOME/axh.sh" set-token    # 从 stdin 读；粘贴后 Ctrl-D，不把令牌放 argv
```

编辑 `$AXH_HOME/env`（保留已有设置，避免重复追加；已有正式域名必须保留 named tunnel token，不退回 quick）：

```bash
AXH_HOSTNAME=axonhub.example.com
# AXH_PLATFORM_REQUIRED=1           # Muse：检查平台新会话巡检是否持续运行
# AXH_MODE=quick                     # 无 token 的临时部署才开启
# AXH_CF_PROTOCOL=http2              # 仅 UDP 7844 不通时
# AXH_CF_TRANSPORT=proxy             # Muse CONNECT shim；先按 muse.md 配置并诊断
AXONHUB_SERVER_SSE_KEEP_ALIVE_ENABLED=true
# AXONHUB_VERSION=v...               # 可选：固定 fork 中实际发布的 release
```

```bash
bash "$AXH_HOME/axh.sh" start
```

未初始化时脚本会暂缓启动隧道。通过本地 `/admin/system/status` 检查 `isInitialized`；必要时用 `curl --noproxy '*' -fsS -X POST http://127.0.0.1:8090/admin/system/initialize -H 'Content-Type: application/json' --data-binary @-` 从 stdin 提交 JSON：`ownerEmail`、`ownerPassword`、`ownerFirstName`、`ownerLastName`、`brandName`。不要在日志或命令参数里留下密码。

```bash
bash "$AXH_HOME/axh.sh" start        # owner 已初始化后隧道才会启动
bash "$AXH_HOME/axh.sh" boot         # 工作中的 cron；否则自动使用 60s 看门狗循环
bash "$AXH_HOME/axh.sh" status
bash "$AXH_HOME/axh.sh" health
curl -fsS https://axonhub.example.com/health
```

验收必须区分：本地 `/health` 成功、owner 已初始化、cloudflared `/ready` 成功、实际公网 HTTPS 成功。quick 模式也必须用输出的 URL 请求 `/health`；分配了 URL 不代表隧道连通。后台入口 `https://<域名>/`；Service URL 必须填写完整的 `127.0.0.1:8090`，不能只填 `8090`，也不能回源到自己的公网域名。SDK base URL 为 `https://<域名>/v1`。

## 保活与恢复

| 层 | 实现 | 能恢复的故障 |
|---|---|---|
| 进程监督 | `supervise`，mkdir 锁、PID + cmdline 校验、崩溃退避 | AxonHub、cloudflared、shim、axproxy、已启用的 MAA 崩溃 |
| 看门狗 | 正常运行的 cron；否则 `watchdog-loop` 每 60 秒执行一次 watchdog | 监督器丢失；本地 health / tunnel ready 连续 3 次失败后重启子进程 |
| 重建恢复 | 本地 `restore`；平台钩子/每分钟任务用新 exec 调 `session-restore` | 系统目录和全部进程丢失后重建运行环境 |

`nohup`、cron、systemd 都不能保证 Muse 不休眠或不回收；循环也不能在整机销毁后自行运行。Muse 的外部巡检和开机钩子按 [muse.md](references/muse.md) 接入。不保留持久目录时，还需要从已有备份恢复数据；外部巡检本身不能恢复消失的数据库。

保留这些不变量：
- `stop` 写维护标记；watchdog 和 `restore` 都不得撤销它，只有 `start` 恢复服务。
- 不用 `pkill -f`；根据 pidfile 和真实子进程命令行判断，namespace 脚本最后 `exec cloudflared`。
- 监督器锁不向子进程传递持锁 fd；二进制通过 rename 替换，日志原地截断。
- token 使用 `--token-file`；env、token、proxy.json、MAA 凭据权限 600；不输出代理认证信息。
- 既有实例的数据、owner、配置不因补装而重置；不执行 `cloudflared service install`。

代理模式下 AxonHub 的 HTTP(S)_PROXY 与 MAA 的 MUSE_PROXY（edge 模式）指向本地 `127.0.0.1:18080`；axproxy 每次新建连接读取 proxy.json，避免业务进程持有轮换密码。首次升级需重启业务进程一次以切换地址，后续凭据变化无需重启健康连接。

Muse 不能以一次公网 200 作为保活验收。需核实新会话定时任务连续运行、凭据变化后新 CONNECT 使用新值、VM 全部进程丢失后外部恢复；没有平台执行能力时明确报告“本地保活已装，重启恢复/凭据续接未闭环”。轮换周期以现场证据为准，不把用户报告中的 1–2 小时或 1–2 分钟固化为平台契约。

已有上游二进制不会因 `install` 自动替换。切换现有部署：先 `stop` 并备份数据，从新版技能目录执行 `install` 更新脚本，检查 env 中固定的 `AXONHUB_VERSION` 在 fork 中存在（否则改为 fork 标签或移除以使用 latest），再执行持久脚本的 `upgrade` 和 `start`。fork 压缩包独立缓存于 `cache/StarTrai1-axonhub/`，不复用旧来源的同名包。

## 运维与验证

| 命令 | 含义 |
|---|---|
| `install` / `upgrade` | 补装 / 下载新版二进制；脚本更新需从新的技能目录执行 install |
| `set-token` / `set-proxy` | 保存 token / 当前会话代理；shim/axproxy 每次 CONNECT 重读 proxy.json；MAA 健康则保持，不健康且凭据变化才重置监督器退避 |
| `doctor` | Muse 路径的 CONNECT、edge TLS 诊断；proxy-ns 另检查 namespace；不代表隧道已注册 |
| `start` / `stop` / `status` | 启动 / 维护停机 / 查看健康和看门狗最后执行时间 |
| `health` | 退出码 0 健康或维护中，1 需恢复，2 无法检查 |
| `session-restore` | 平台新 exec：先接收本轮代理，再健康检查和有界恢复；不能由旧看门狗冒充新会话调用 |
| `muse-hooks <hooks目录>` | 注册专用唤醒钩子并生成平台任务说明；必须核验真实平台运行记录 |
| `maa-install` | 安装已审阅的 MuseAutoApprove 及依赖，账号和启用步骤见参考文件 |
| `boot` / `restore` | 装看门狗与可用的开机入口 / 幂等补装并恢复，维护中保持停止 |

可在 `env` 设置 `AXH_PROXY_LOCAL_PORT`（默认 18080）、`AXH_DIRECT_HOSTS`（只填已验证可直连域名）、`AXH_PUBLIC_PROBE=direct|local-proxy`（默认直连，不隐式用旧凭据）、`AXH_PORT`、`AXH_METRICS_PORT`、`AXH_WATCHDOG_BACKEND=auto|cron|loop`、`AXONHUB_VERSION` 和 AxonHub 的 `AXONHUB_*` 配置。升级前 `stop` 并备份 `data/`（SQLite WAL 不做普通热拷贝），升级后 `start`。发行包存在 checksums 时验证；取不到 checksums 会记录警告，交付时如实说明验证缺口。

在目标环境做一次有边界的故障演练：终止已核实的子进程观察监督恢复；终止监督器观察看门狗恢复；重复 restore 检查单实例；stop 后确认不被拉回。真实重启/重建另行验证，未做不能声称已通过。仓库离线回归：`python3 -m unittest discover -s tests -v`。

本地真实二进制验证：`python3 tests/smoke_cloudflared.py /path/to/cloudflared`（需 openssl，已用 2026.9.3 验证）。使用本地假 edge 和测试凭据，检查 CONNECT、SNI、HTTP/2 帧，不注册真实隧道。

排障先看 `$AXH_HOME/logs/{axonhub,tunnel,shim,watchdog,axh}.log`：无 cron 不必等待 apt；HTTP/2 仍失败走 Muse 诊断；公网 1033 看 tunnel ready，502 看 origin 路由与本地服务。不要用取消 TLS 校验来掩盖 CONNECT/TLS 失败。

排查业务 407 必须复现实际 AI 请求，跨凭据轮换复测；401 只能证明到达服务商，不能证明对话成功。MAA 的 pending=0/心跳新鲜只证明队列轮询，不代表用户的审批卡片已覆盖；先按 [审批诊断](references/muse-auto-approve.md#现场复盘与排障边界) 核对同一审批 ID 的原生日志与历史。

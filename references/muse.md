# Muse：代理 edge 连接与重建

## 适用条件与数据路径

社区方案处理的是“cloudflared 直连 edge 失败，但平台提供的 egress HTTP(S) 代理能够 CONNECT 到真实 edge”的环境：

```text
cloudflared --protocol http2 --edge 127.0.0.1:7844
  → 127.0.0.1:7844 shim
  → HTTP CONNECT 经平台代理访问 region1/2.v2.argotunnel.com:7844
  → Cloudflare edge（原始 TLS 字节透传，cloudflared 校验证书）
```

默认 `AXH_CF_TRANSPORT=proxy` 直接把 edge TCP 地址设为回环地址，不依赖本机 DNS 或 mount 权限；TLS SNI 与连接地址分开配置，仍由 cloudflared 验证 `h2.cftunnel.com`。代理在自己的网络侧解析真实 edge 域名。shim 轮换两个 region，建立 CONNECT 失败时尝试另一个；不承诺一条连接中途断开后无损迁移，重新注册由 cloudflared 和监督器负责。

先核实 Python **3.11+** 和代理允许 CONNECT 到 edge 的 **7844**。代理 URL 使用 HTTPS 或监听 443，不等于它允许目标 7844。仅允许目标 443 的代理无法通过这个 shim 自动获得 Tunnel 连接，不能把 edge 端口擅自改成 443。

## 部署或修复已有实例

1. 保留已有 `data/`、`env`、`tunnel.token`。用旧 `axh.sh stop` 停止旧监督器；若之前人工装过 `watchdog-loop.sh`，核实其 pidfile/cmdline 后终止旧循环并移除它的重复启动入口，避免新旧两套同时巡检。
2. 从新技能目录执行 `AXH_HOME="$HOME/axonhub-stack" bash scripts/axh.sh install`。必须从完整技能目录安装全部辅助脚本；恢复不依赖 `/etc/cloudflared` 中遗留的文件。
3. 利用平台已经提供的 `HTTPS_PROXY`（也支持小写，HTTP_PROXY 或显式 AXH_EDGE_PROXY）：

```bash
export AXH_HOME="$HOME/axonhub-stack"
bash "$AXH_HOME/axh.sh" set-proxy
# 编辑 env，设置 AXH_CF_TRANSPORT=proxy；无需设置 AXH_CF_PROTOCOL，proxy 模式强制 http2。
bash "$AXH_HOME/axh.sh" doctor
```

`set-proxy` 从调用脚本前的环境取值，原子保存到权限 600 的 `proxy.json`；旧 proxy.env 仅作迁移兼容。shim 每次 CONNECT 重读文件，不再使用常驻进程启动时的旧密码。没有本轮输入时返回 2，不把旧文件重新标记成新凭据。文件时间仅证明接收时间，不能证明平台代理签发/到期时间。不要把真实代理地址、token 或 owner 密码提交到 Git。

4. doctor 通过后执行 `start`、`boot`、`status`。已初始化的 owner 不要重建；没有 crontab 时 boot 自动启用 60 秒循环，不调用 apt-get。以 `run/watchdog.tick` 的时间推进证明它实际在跑。
5. 检查 `/ready` 和公网 `https://<实际域名>/health`。未通过时只能报告“已配置代理路径、尚未连通”，不能报告部署成功。

如需社区原始路径，设 `AXH_CF_TRANSPORT=proxy-ns`：包装脚本每次启动生成 hosts overlay，仅在私有 mount namespace 中挂载，系统 `/etc/hosts` 不变；显式 `--edge region1...:7844 --edge region2...:7844` 避免 SRV/DoT 查询阻断。此模式需要 unshare、mount 与允许 mount namespace 的权限（通常是 root/CAP_SYS_ADMIN；root 本身不保证可用）。doctor 会追加隔离挂载检测。

两种模式都使用当前 cloudflared 存在的隐藏 `--edge` 参数，升级后须重新验证。它们共用本地 listener，不能把两个 region 候选误称为两个已注册的独立连接。

## 如何解读诊断

| 结果 | 结论与下一步 |
|---|---|
| namespace `Operation not permitted` | proxy-ns 缺少挂载权限；改用默认 proxy 模式，不改系统 hosts；代理 CONNECT 条件仍需满足 |
| CONNECT 403/407 | 代理拒绝目标 / 认证未通过，核对平台允许范围和当前代理凭据 |
| CONNECT TimeoutError | 代理连接或 CONNECT 响应超时；只能确认该路径失败，不能凭此断言按 TLS 指纹拦截 |
| CONNECT OK，edge TLS 失败 | 代理之后的 edge/TLS 路径未通过；保留 TLS 校验，核对代理策略、证书、SNI 等证据 |
| TLS OK，ready NO | Python 探针不等于 cloudflared；看 tunnel.log 的注册、token、握手和版本错误 |
| ready yes，公网失败 | 检查 Cloudflare Public Hostname、DNS、origin 的 `http://127.0.0.1:8090` 和本地 health |

cloudflared 的 connectivity prechecks 在当前官方源码中仅用于诊断，不作为启动闸门。`hard_fail=true` 不足以证明代理路径失败；同样，CONNECT 200 不足以证明 TLS 成功，TLS 成功也不足以证明 tunnel 注册成功。

doctor 模仿 cloudflared 的 HTTP/2 TLS 设置：验证证书与 `h2.cftunnel.com` SNI，不要求 ALPN。当前 cloudflared 的此协议未设置 ALPN；把“没有协商出 h2 ALPN”作为失败条件会误判。

如果代理端的域名解析也有问题，可在 env 中设置 `AXH_EDGE_TARGETS=<当前官方 region1 IP>,<当前官方 region2 IP>`，逗号分隔、**不带端口**。先核对 Cloudflare 当前地址表并确认代理允许 IP CONNECT；不要把社区旧 IP 永久硬编码。shim 始终连接目标 7844，TLS SNI/证书校验不变。

## AxonHub 业务出站的本地代理

2026-09-29 的现场复盘报告凭据约 60–75 秒轮换，且服务商 POST 真实失败。这个时间是现场测量值，不是所有 Muse 环境的固定契约。文件更新只会改变后续读取文件的程序，不会更新既有 Go/Node 进程的环境；Go 标准库的环境代理函数还使用 sync.Once 缓存配置（见 [transport.go](https://go.dev/src/net/http/transport.go)）。

代理模式自动监督 `axproxy`，先启动本地 listener，再启动 AxonHub/MAA。`AXH_MAA_PROXY=edge` 即使配合 direct 隧道模式，也会启用 axproxy：

```text
AxonHub / MAA / cloudflared 的 HTTP API
  → 无凭据本地代理 http://127.0.0.1:18080
  → 每条新连接读取 proxy.json
  → 经上游 HTTP(S) 代理 CONNECT 真实目标
  → 服务商 HTTPS/TLS、Muse WS/token API（加密字节透传）
```

`AXH_PROXY_LOCAL_PORT` 可改端口，仅绑定回环，不能把它发布成公网服务。AxonHub 大小写 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 均指向这里；MAA edge 模式的 MUSE_PROXY 也用它。首次从旧脚本迁移需停止旧服务、安装全部脚本并启动一次，后续轮换不重启健康业务进程。检查 AxonHub channel 的显式代理配置和已有 NO_PROXY，避免绕过环境代理。

代理接受 HTTPS CONNECT 和绝对 URI 的 HTTP 请求。普通 HTTP 会先 CONNECT 到目标 HTTP 端口，再转成 origin-form 请求；上游须允许该目标端口，拒绝时明确失败，不假设可以从 80 换到 443。HTTP 请求携带 `Connection: close`，HTTPS/WS/SSE 隧道保持字节透传与背压，不拦截证书、不记录请求体或 URL。

遇到上游 CONNECT 407，仅当重读文件发现凭据已改变时，再尝试一次 CONNECT；此时业务请求尚未发送。凭据不变、其他错误或业务数据已经发送后均不重放请求。代理拒绝在客户端表现为 502，`logs/axproxy.log` 保留脱敏的上游 CONNECT 状态码。它不能自行生成新密码，平台新会话供应中断时仍会失败。

`AXH_DIRECT_HOSTS` 是可选补充：只填已实测直连可达的域名，默认不硬编码 GitHub 或服务商域名；其他目标仍走 axproxy。修改 bypass 配置需重启客户端一次，不能把修改 shell 环境误认为运行中的 Go/Node 已更新。

## 407 与公网探针排障

- 默认 `status` 的公网探针明确 `--noproxy '*'`，与凭据链路分离。若目标环境禁止直连，设 `AXH_PUBLIC_PROBE=local-proxy`，输出会标明经本地代理；不隐藏此依赖。
- 先按时间戳和请求来源区分 catalog/更新检查、MAA 建连、真实 AI 调用。复测用户实际 POST/流式对话，并在下一个凭据版本重复；“日志无新增”不代表业务已恢复。
- `/health`、`/ready`、axproxy TCP listener 分别证明对应组件存活，不证明服务商请求成功。无 API key 的 401 只能证明请求到达服务商，不能报告真实对话通过。
- `axh.log` 和本技能 tick 使用 UTC ISO 8601；上游自己的日志按其时区换算，不清空日志掩盖历史问题。
- MAA 心跳新鲜且 daemon 存活时，凭据变化不打断其 WS；不健康时 `restart_sup maa` 等旧 supervisor 退出后启动新的 supervisor，清除累计退避。停止标记和整栈维护模式仍优先。

## 新会话刷新 + VM 重建恢复（部署必须完成）

本地 watchdog 的子进程只继承旧环境，重启 shim 也不会产生新密码。外部恢复不能只在 `health` 失败时运行：隧道仍健康时也要接收新会话凭据，供下一次重连使用。

在新平台 exec 中执行（替换成实际绝对持久路径）：

```bash
AXH_HOME=/实际持久路径/axonhub-stack bash /实际持久路径/axonhub-stack/axh.sh session-restore
```

`session-restore` 顺序：尊重维护标记 → 单实例 → 记录本轮 exec/boot ID → 保存新环境代理 → 检查服务 → 必要时 restore/watchdog → 对未 ready 的 tunnel 进行一次有界重启和复查。正常长连接不因密码变化被杀；MAA 的 edge 模式也经 axproxy，以便 WS 重连和五分钟 token touch 使用新凭据；健康时不重启，不健康且凭据变化时整体重启监督器清除退避。固定域名存在却缺 token 会报错，绝不自动切 Quick Tunnel。

**只能由新平台会话调用。** 不先 source stack/env、proxy.env，也不要从旧终端、旧 watchdog、旧 supervisor 调用来伪装新环境。优先读取本轮 exec 注入的 HTTPS_PROXY；如果平台根本不提供新值，明确报告 `NEEDS_FRESH_PROXY`，不能声称凭据已经续期。自动审批登录 token、CF tunnel token 和 egress proxy 密码是三种不同凭据。

### 平台钩子与定时任务

参照社区已展示的 Muse hooks 格式，使用独立 ID `axonhub-session-recovery`，不覆盖已有 home-init/init.sh：

1. 用平台工具确认实际的 hooks/definitions、hooks/scripts 目录、hook runtime 协议及注册权限；通常在 `/home/hatch/hooks`，不能仅靠猜路径新建目录后声称已注册。
2. 执行 `bash "$AXH_HOME/axh.sh" muse-hooks /实际/hooks`。它写专用 JSON 和 launcher，使用 `HATCH_HOOK_RUNTIME` 的 silent/wake；每 60 秒检测，首次启动、健康失败或新会话记录过期时 wake 平台 Agent。**该钩子不读取自己的旧代理作为新凭据，也不在沙盒内循环模拟平台调度。**
3. 通过当前平台实际提供的任务接口，创建/更新每分钟任务 `axonhub-session-recovery`，内容采用生成的 `$AXH_HOME/platform-task.txt`；禁止重叠，最多执行一轮有界恢复。不要把生成 txt 当作已创建平台任务，也不要仅向未知目录写 Markdown 冒充任务 API。
4. 平台新 Agent 收到唤醒或任务后，必须发起全新的 exec，调用 session-restore。0 健康/维护静默；1 报告脱敏失败证据，不在本轮无限重试；2 新凭据或环境不可用，下轮再试，连续三轮报告。恢复后从平台新会话访问正式域名的 HTTPS /health。
5. 编辑 env 加 `AXH_PLATFORM_REQUIRED=1`；验证任务/钩子启用、至少两个真实平台运行记录、`run/session.tick` 持续更新与 `session.boot` 对应当前 boot ID。hook/任务存在但无执行记录，只能报告已配置未验收。

若平台只提供开机 init.sh，保留既有逻辑并合并恢复入口；启动钩子可先运行本地 restore，但**仍需要每分钟的新会话任务更新代理**。缺少平台能力或授权时提供生成文件和实际缺口；不要退回“nohup 在跑所以永久保活成功”。本地检查器不能让平台在它被杀后自行启动。

### 验证故障闭环

- 在测试环境中终止这套应用的全部已核实进程，让平台任务恢复，检查 owner/data/token/正式域名不变；不是重启宿主机，不碰其他应用。
- 用两个不泄露的凭据版本验证新会话写入后，已运行 shim/axproxy 的新 CONNECT 使用新值，同时既有连接保持。
- 真实 VM 重启/重建需当前部署授权和平台运行记录；没有实际重建过就分开报告，不把模拟进程全灭等同于重建验收。
- 保存脱敏证据：boot ID、uptime、最后完成的 watchdog tick、新会话 tick、CONNECT 状态、/ready、公网 /health。TLS EOF 单独不能证明密码到期，代理 407/新旧凭据对照才更有指向性。

这些机制是重建后恢复，不会阻止平台回收。`data/`、全部脚本、二进制、proxy.json、tunnel.token、MAA 的 data/node_modules/lockfile 均须持久化；无持久盘要先有数据备份。MuseAutoApprove 按 [专用说明](muse-auto-approve.md) 接入；不安装无关探针、Hermes 或 SSH。

## 依据

- [muse-reverse-ssh](https://github.com/dengyie/awesome-skills/tree/main/muse-reverse-ssh)：监督、持久化与外部恢复的分层设计。
- [muse-guardian](https://github.com/bytehola/muse-guardian)：平台巡检、开机钩子和持久目录恢复；这里只复用与 AxonHub 部署有关的原则。
- [Cloudflare firewall requirements](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)：7844、region 地址和 SNI。
- [cloudflared edge dialer](https://github.com/cloudflare/cloudflared/blob/master/edgediscovery/dial.go)：edge TCP 使用 net.Dialer，不自动读取 HTTP_PROXY。
- [cloudflared tunnel command](https://github.com/cloudflare/cloudflared/blob/master/cmd/cloudflared/tunnel/cmd.go)：显式 edge 参数与诊断性 prechecks。

- [cloudflared tunnel configuration](https://github.com/cloudflare/cloudflared/blob/master/cmd/cloudflared/tunnel/configuration.go)：TLS ServerName 来自协议设置，与 EdgeAddrs 分离。
- [cloudflared protocol settings](https://github.com/cloudflare/cloudflared/blob/master/connection/protocol.go)：HTTP/2 的 SNI 与 ALPN 设置。

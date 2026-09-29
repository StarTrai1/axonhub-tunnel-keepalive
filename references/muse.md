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
2. 从新技能目录执行 `AXH_HOME="$HOME/axonhub-stack" bash scripts/axh.sh install`。必须一起安装三个脚本；恢复不依赖 `/etc/cloudflared` 中遗留的文件。
3. 利用平台已经提供的 `HTTPS_PROXY`（也支持小写，HTTP_PROXY 或显式 AXH_EDGE_PROXY）：

```bash
export AXH_HOME="$HOME/axonhub-stack"
bash "$AXH_HOME/axh.sh" set-proxy
# 编辑 env，设置 AXH_CF_TRANSPORT=proxy；无需设置 AXH_CF_PROTOCOL，proxy 模式强制 http2。
bash "$AXH_HOME/axh.sh" doctor
```

`set-proxy` 将选定地址以 shell 安全引用写到权限 600 的 `proxy.env`，不回显凭据；cron/systemd 可重新读取。平台轮换代理地址或认证后，从新环境再次执行 set-proxy，再 stop/start。不要把真实代理地址、token 或 owner 密码提交到 Git。

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

## 沙盒重建后的第三层恢复

所有脚本、程序、数据和敏感配置保存在 `$AXH_HOME`。`restore` 可重建本地监督器、看门狗、可用的 cron/systemd 开机入口；proxy-ns 的 hosts overlay 会在下一次隧道启动时重建。不调用 `cloudflared service install`，因而不依赖社区提到的 `/etc/cloudflared` 清理行为，也不用搬运系统目录中的 token。

平台侧配置两个外部触发，使用部署时确定的**绝对路径和用户身份**，避免平台 root HOME 指向另一个目录：

- 开机钩子：`AXH_HOME=/实际持久路径/axonhub-stack bash /实际持久路径/axonhub-stack/axh.sh restore`。
- 每分钟外部巡检：运行同一路径的 `health`；0 静默，1 执行一次 restore 后再次检查并报告持续失败，2 保留诊断而不盲目重装。平台必须避免同一轮无限重试。

若当前 Muse 提供 `home-init`/`init.sh` 或平台定时任务接口，读取已有配置后合并入口，保留其他任务；不要直接覆盖现有 init.sh，也不要猜测平台 API。平台配置需要其实际授权时按界面完成；没有此能力时提供上述准确命令并标明“整机重建自动恢复未接入”。外部巡检能恢复丢失的看门狗循环；本地循环自身不能抵抗全部进程被回收。

这些机制是服务自恢复，不是让平台永不休眠的保证。本技能不安装无关探针、Hermes、SSH 或自动审批工具。

## 依据

- [muse-reverse-ssh](https://github.com/dengyie/awesome-skills/tree/main/muse-reverse-ssh)：监督、持久化与外部恢复的分层设计。
- [muse-guardian](https://github.com/bytehola/muse-guardian)：平台巡检、开机钩子和持久目录恢复；这里只复用与 AxonHub 部署有关的原则。
- [Cloudflare firewall requirements](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)：7844、region 地址和 SNI。
- [cloudflared edge dialer](https://github.com/cloudflare/cloudflared/blob/master/edgediscovery/dial.go)：edge TCP 使用 net.Dialer，不自动读取 HTTP_PROXY。
- [cloudflared tunnel command](https://github.com/cloudflare/cloudflared/blob/master/cmd/cloudflared/tunnel/cmd.go)：显式 edge 参数与诊断性 prechecks。

- [cloudflared tunnel configuration](https://github.com/cloudflare/cloudflared/blob/master/cmd/cloudflared/tunnel/configuration.go)：TLS ServerName 来自协议设置，与 EdgeAddrs 分离。
- [cloudflared protocol settings](https://github.com/cloudflare/cloudflared/blob/master/connection/protocol.go)：HTTP/2 的 SNI 与 ALPN 设置。

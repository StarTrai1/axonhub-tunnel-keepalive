---
name: axonhub-tunnel-keepalive
description: 在没有公网 IP 的 Linux 主机/沙盒/容器上部署 looplj/axonhub（AI 网关）官方 linux 发行包，用 Cloudflare Tunnel 提供 HTTPS 访问，并带三层保活（进程监督 + 看门狗 + 重建恢复）。只要用户提到部署/安装/保活/自启/重建后恢复 axonhub，或想把本地服务通过 Cloudflare Tunnel / cloudflared 暴露成 https 域名，或说"沙盒重启后服务就没了"，就使用本技能，即使他没有明确说"skill"或"保活"。
---

# AxonHub + Cloudflare Tunnel 保活部署

```
浏览器/SDK ──HTTPS──▶ Cloudflare 边缘 ◀──出站长连接── cloudflared ──▶ 127.0.0.1:8090 axonhub
```

主机只出站，不需要公网 IP 和开放端口；HTTPS 由 Cloudflare 终结；axonhub 只监听回环地址。全部逻辑在一个脚本 `scripts/axh.sh`（安装、监督、看门狗、恢复），状态全在 `$AXH_HOME`（默认 `~/axonhub-stack`，**必须放持久盘**）。

## 保活原理（三层）

| 层 | 机制 | 对付的故障 |
|---|---|---|
| L1 监督 | `supervise`：mkdir 原子锁 + 重启循环 + 崩溃退避 | 进程崩溃（秒级） |
| L2 看门狗 | cron 每分钟：补拉缺失的监督进程；`/health`、cloudflared `/ready` 连续 3 次失败则杀子进程重启 | 进程还在但服务卡死（≤3 分钟） |
| L3 恢复 | `restore` 幂等 + `boot`（@reboot/systemd）+ 外部巡检调 `health` | 整机重启/沙盒重建，cron 与进程全丢 |

踩过的坑，已写进脚本，改动时别破坏：
- **锁用 `mkdir`，不用 `flock`**：flock 的 fd 会被子进程继承，监督进程被杀后孤儿永久占锁。
- **认进程只用 pidfile + `/proc/<pid>/cmdline`**，不用 `pkill -f`（会误杀自己的 shell；测试时 `pgrep -f` 同样会匹配到检查命令本身）。
- **进程活着 ≠ 服务活着**：以 axonhub `/health` 和 cloudflared `/ready`（已连上 CF 边缘）为准。
- **升级用 rename 替换二进制**（`mv -f`），运行中也安全；日志原地截断，不 `mv`，避免 fd 指向已删除文件。
- **令牌走 `--token-file`**，不进命令行；日志不重定向到 `/dev/null`。
- **`stop` 写维护标记**，看门狗不再拉起，避免"停不掉"。

## 动手前向用户确认

1. 域名已托管在 Cloudflare？没有 → 只能用 quick 模式（随机 `trycloudflare.com` 地址，重启即变，无 SLA，仅测试）。
2. 让用户在 Cloudflare Zero Trust → Networks → Tunnels 创建 **Cloudflared** 类型隧道，复制令牌（整条安装命令也行）；在该隧道的 **Public Hostname** 里把域名指向 **HTTP `localhost:8090`**。
3. axonhub owner 的邮箱、密码。机器重建后 `$AXH_HOME` 是否保留？（不保留就必须配外部巡检，见步骤 6）

令牌和密码只写入 600 权限文件，不回显、不写进记忆或日志。

## 步骤

```bash
# 1. 落盘并安装（在技能目录下执行；下载 axonhub 最新 linux 包并校验 sha256、下载 cloudflared）
export AXH_HOME=~/axonhub-stack; mkdir -p $AXH_HOME && cp scripts/axh.sh $AXH_HOME/ && bash $AXH_HOME/axh.sh install

# 2. 写入隧道令牌（从 stdin 读，可直接粘贴 Cloudflare 给的整条命令）
bash $AXH_HOME/axh.sh set-token          # 粘贴后 Ctrl-D
echo 'AXH_HOSTNAME=axonhub.example.com' >> $AXH_HOME/env    # 用于公网 HTTPS 自检
echo 'AXONHUB_SERVER_SSE_KEEP_ALIVE_ENABLED=true' >> $AXH_HOME/env   # 默认关闭；流式响应保活，防 CF 524

# 3. 启动，并在暴露前先初始化 owner（/admin/system/initialize 无鉴权，谁先到谁当 owner）
bash $AXH_HOME/axh.sh start              # 此时隧道被闸门拦住，日志会写 "tunnel withheld"
curl -s -X POST http://127.0.0.1:8090/admin/system/initialize -H 'Content-Type: application/json' -d @- <<'J'
{"ownerEmail":"<邮箱>","ownerPassword":"<密码>","ownerFirstName":"<名>","ownerLastName":"<姓>","brandName":"AxonHub"}
J
bash $AXH_HOME/axh.sh start              # 已初始化 → 隧道启动（或等看门狗下一轮）

# 4. 装保活：cron 每分钟看门狗 + @reboot；有 systemd 且是 root 时再加 unit
bash $AXH_HOME/axh.sh boot

# 5. 验证
bash $AXH_HOME/axh.sh status             # 期望全部 ok/yes，public https: ok
curl -i https://axonhub.example.com/health
```

用户随后访问 `https://<域名>/` 登录后台，配置 channel 与 API key；SDK 的 base URL 为 `https://<域名>/v1`。

6. **重建场景（只有持久盘保留时）**：cron 和 systemd 会随重建消失，必须由外部触发——平台开机钩子执行 `bash $AXH_HOME/axh.sh restore`；外部每分钟巡检 `bash $AXH_HOME/axh.sh health`：退出码 0 静默，1 → 跑 `restore`，2 → 本轮跳过。
7. **演练（必做，用日志证据说话）**：杀 axonhub 子进程 → 数秒内被拉回；`kill -9` 监督进程 → 一分钟内看门狗拉回且无重复进程；`restore` 反复执行不产生第二个实例。

## 命令

| 命令 | 作用 |
|---|---|
| `install` | 装/补装二进制（幂等，缓存在 `cache/`，可离线复用） |
| `set-token` | 从 stdin 提取令牌写入 600 文件 |
| `start` / `stop` | 启动 / 停止并进入维护模式 |
| `status` | 监督进程、健康、是否已初始化、隧道就绪、公网 HTTPS |
| `health` | 退出码 0 健康 / 1 需恢复 / 2 无法判断（供外部巡检） |
| `watchdog` | 看门狗（cron 调用） |
| `boot` / `restore` | 写 cron+systemd / 一键幂等恢复 |
| `upgrade` | 升级两个二进制并重启子进程（升级前先 `stop`，再备份 `data/`：sqlite 是 WAL，别热拷贝） |

`$AXH_HOME/env` 可设：`AXH_PORT`、`AXH_HOSTNAME`、`AXH_MODE=quick`、`AXH_CF_PROTOCOL=http2`、`AXONHUB_VERSION`（锁定版本；latest 目前是 beta）、任意 `AXONHUB_*`（如换 Postgres：`AXONHUB_DB_DIALECT`、`AXONHUB_DB_DSN`）。

## 排障

| 现象 | 原因 → 处理 |
|---|---|
| `tunnel ready: NO`，日志有 `Failed to dial a quic connection` | UDP 7844 被封 → env 加 `AXH_CF_PROTOCOL=http2`；仍不通则出站 `*.argotunnel.com:7844` 被防火墙拦 |
| 公网 502 / 1033 | Public Hostname 服务不是 `http://localhost:8090`，或隧道未连上 |
| 流式响应中途 524 | 没开 SSE keep-alive（见步骤 2） |
| `tunnel withheld` | 实例未初始化，做步骤 3；确需绕过才设 `AXH_ALLOW_UNINIT=1` |
| 看门狗不生效 | 无 cron 守护进程或无 `crontab` → 用外部巡检（步骤 6） |
| 下载失败 / checksum 报错 | GitHub 不通：把 `axonhub_<版本>_linux_<架构>.zip` 放进 `cache/` 并设 `AXONHUB_VERSION` |
| 重复进程、端口被占 | 用 `status` 看监督进程；杀进程只走 `stop`，别用 `pkill -f` |

## 安全与边界

- 别在 Cloudflare Access 里覆盖 API 路径（`/v1` 等），SDK 无法通过登录页；如需保护后台，先确认具体路径。
- 仅支持 linux amd64 / arm64。cron 安装与 systemd unit 未在真实环境验证过，交付前请在目标机器上跑一遍步骤 7。

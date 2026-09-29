#!/usr/bin/env bash
# axh.sh — AxonHub + Cloudflare Tunnel：安装 / 保活 / 恢复（幂等）
# 用法: axh.sh install | set-token | set-proxy | doctor | start | stop | status | health | boot | restore | session-restore | muse-hooks DIR | maa-install | upgrade
# 所有状态都在 $AXH_HOME（默认 ~/axonhub-stack），把它放在持久盘上。
set -u
umask 077
AXH_USER_HOME="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}"
export AXH_HOME="${AXH_HOME:-$AXH_USER_HOME/axonhub-stack}"
mkdir -p "$AXH_HOME"/{bin,data,run,logs,cache} && chmod 700 "$AXH_HOME"
ENV_FILE="$AXH_HOME/env"
export AXH_PROXY_INPUT="${AXH_EDGE_PROXY:-${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}}"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
load_proxy() {
  local assignment
  if [ -f "$AXH_HOME/proxy.json" ]; then
    assignment=$(python3 "$AXH_HOME/axh_proxy.py" shell) || return 2
    eval "$assignment"  # Our own helper quotes the value with shlex.quote.
  elif [ -f "$AXH_HOME/proxy.env" ]; then
    set -a; . "$AXH_HOME/proxy.env"; set +a  # Previous-release compatibility.
  fi
  if [[ "${AXH_CF_TRANSPORT:-direct}" = proxy* ]] && [ -n "${AXH_EDGE_PROXY:-}" ]; then
    export HTTPS_PROXY="$AXH_EDGE_PROXY" HTTP_PROXY="$AXH_EDGE_PROXY"
    export https_proxy="$AXH_EDGE_PROXY" http_proxy="$AXH_EDGE_PROXY"
  fi
  return 0
}
if ! load_proxy; then
  case "${1:-}" in set-proxy|session-restore|stop) ;; *) exit 2;; esac
fi
PORT="${AXH_PORT:-8090}"; MPORT="${AXH_METRICS_PORT:-20241}"
export AXH_PROXY_LOCAL_PORT="${AXH_PROXY_LOCAL_PORT:-18080}"
BIN="$AXH_HOME/bin"; DATA="$AXH_HOME/data"; RUN="$AXH_HOME/run"; LOGS="$AXH_HOME/logs"; CACHE="$AXH_HOME/cache"
TOKEN_FILE="$AXH_HOME/tunnel.token"; SELF="$AXH_HOME/axh.sh"
MAA="$AXH_HOME/MuseAutoApprove"
export PATH="$AXH_USER_HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOGS/axh.log"; }
die() { echo "ERROR: $*" >&2; exit 1; }
L() { echo "http://127.0.0.1:$1"; }

# ---------- 进程校验：永远 pidfile + /proc/<pid>/cmdline，不用 pkill -f ----------
is_ours() { [ -n "${1:-}" ] && [ -r "/proc/$1/cmdline" ] && tr '\0' ' ' < "/proc/$1/cmdline" | grep -qF -- "$2"; }
sup_pid() { cat "$RUN/$1.lock/pid" 2>/dev/null; }
sup_alive() { is_ours "$(sup_pid "$1")" "axh.sh supervise $1"; }
child_match() { case "$1" in axonhub) echo "$BIN/axonhub";; tunnel) echo "$BIN/cloudflared";; shim) echo "$AXH_HOME/cloudflared-edge-shim.py";; maa) echo "$MAA/muse-daemon.cjs";; axproxy) echo "$AXH_HOME/axh-forward-proxy.py";; esac; }
kill_child() {  # 只杀校验过的子进程；僵尸 cmdline 为空，自然判定为已退出
  local n=$1 p m i; p=$(cat "$RUN/$n.child" 2>/dev/null); m=$(child_match "$n")
  is_ours "$p" "$m" || return 0
  kill -TERM "$p" 2>/dev/null
  for i in 1 2 3 4 5 6 7 8 9 10; do is_ours "$p" "$m" || return 0; sleep 1; done
  kill -KILL "$p" 2>/dev/null; return 0
}

# ---------- 单实例锁：原子 mkdir（不用 flock：fd 会被子进程继承，锁被孤儿永久占住）----------
lock_release() { [ "$(sup_pid "$1")" = "$$" ] && rm -rf "$RUN/$1.lock"; }
lock_acquire() {  # $1=锁名 $2=cmdline 特征
  local d="$RUN/$1.lock" pid i
  for i in 1 2 3; do
    if mkdir "$d" 2>/dev/null; then echo $$ > "$d/pid"; return 0; fi
    pid=$(cat "$d/pid" 2>/dev/null)
    if [ -z "$pid" ]; then   # 对方正处在 mkdir→写 pid 的微小窗口，避让
      [ $(( $(date +%s) - $(stat -c %Y "$d" 2>/dev/null || echo 0) )) -lt 5 ] && return 1
    elif is_ours "$pid" "$2"; then return 1; fi
    mv -T "$d" "$d.stale.$$" 2>/dev/null && rm -rf "$d.stale.$$"   # rename 原子，只有一方能收走陈旧锁
    sleep 1
  done
  return 1
}

# ---------- 被监督的服务 ----------
proxy_mode() { [[ "${AXH_CF_TRANSPORT:-direct}" = proxy || "${AXH_CF_TRANSPORT:-direct}" = proxy-ns ]]; }
run_shim() { exec python3 "$AXH_HOME/cloudflared-edge-shim.py"; }
forward_wanted() { proxy_mode || [ "${AXH_MAA_PROXY:-direct}" = edge ]; }
forward_url() { L "$AXH_PROXY_LOCAL_PORT"; }
run_axproxy() { exec python3 "$AXH_HOME/axh-forward-proxy.py"; }
forward_ready() {
  is_ours "$(cat "$RUN/axproxy.child" 2>/dev/null)" "$(child_match axproxy)" &&
    python3 "$AXH_HOME/axh-forward-proxy.py" --check >/dev/null 2>&1
}
ensure_forward() {
  forward_wanted || return 0
  start_sup axproxy
  local i; for i in $(seq 1 20); do forward_ready && return 0; sleep 0.1; done
  log "local forward proxy not ready; clients will retry through supervision"; return 1
}
client_proxy_env() {
  forward_wanted || return 0
  export HTTPS_PROXY="$(forward_url)" HTTP_PROXY="$(forward_url)" ALL_PROXY="$(forward_url)"
  export https_proxy="$HTTPS_PROXY" http_proxy="$HTTP_PROXY" all_proxy="$ALL_PROXY"
  # Extra bypass hosts must be verified reachable; do not assume GitHub or an AI provider is direct-accessible.
  export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}${no_proxy:+,$no_proxy}${AXH_DIRECT_HOSTS:+,$AXH_DIRECT_HOSTS}"
  export no_proxy="$NO_PROXY"
}
maa_wanted() { [ "${AXH_MAA_ENABLED:-0}" = 1 ] && [ ! -e "$MAA/data/muse-daemon.stop" ]; }
maa_healthy() { is_ours "$(cat "$RUN/maa.child" 2>/dev/null)" "$(child_match maa)" && python3 "$AXH_HOME/maa-status.py" --health; }
run_maa() {
  local old args=(--loop 10000 --always --scope destination_domain --fallback-once)
  [ -f "$MAA/muse-daemon.cjs" ] || die "run maa-install first"
  old=$(cat "$MAA/data/muse-daemon.pid" 2>/dev/null)
  if is_ours "$old" "$MAA/muse-daemon.cjs"; then die "existing MuseAutoApprove: stop previous launcher before adopting"; fi
  [ -n "${MUSE_VM_ID:-}" ] || die "current MUSE_VM_ID required"
  rm -f "$MAA/data/muse-daemon.pid"
  date +%s%3N > "$RUN/maa.started"
  case "${AXH_MAA_DECISION:-allow_always}" in
    allow_always) ;;
    allow_once) args=(--loop 10000 --decision allow_once --no-fallback);;
    *) die "AXH_MAA_DECISION must be allow_once or allow_always";;
  esac
  load_proxy || return 2
  case "${AXH_MAA_PROXY:-direct}" in
    edge) export MUSE_PROXY="$(forward_url)";;
    direct) unset MUSE_PROXY;;
    *) die "AXH_MAA_PROXY must be direct or edge";;
  esac
  cd "$MAA" || return 1
  exec "${AXH_NODE_BIN:-node}" "$MAA/muse-daemon.cjs" "${args[@]}"
}
run_axonhub() {
  client_proxy_env
  cd "$DATA" || exit 1   # axonhub 默认在 cwd 建 sqlite 库
  export AXONHUB_SERVER_HOST="${AXONHUB_SERVER_HOST:-127.0.0.1}" AXONHUB_SERVER_PORT="$PORT"
  exec "$BIN/axonhub"
}
run_tunnel() {
  load_proxy || return 2
  client_proxy_env
  if [ -n "${AXH_HOSTNAME:-}" ] && [ ! -s "$TOKEN_FILE" ]; then die "named hostname configured but tunnel token missing; refusing quick fallback"; fi
  local c=(tunnel --no-autoupdate --metrics "127.0.0.1:$MPORT" --protocol "${AXH_CF_PROTOCOL:-auto}")
  if proxy_mode; then
    # Explicit loopback edge avoids DNS and privileged mounts. TLS/SNI remain
    # cloudflared's responsibility (h2.cftunnel.com, independent of dial address).
    c=(tunnel --no-autoupdate --metrics "127.0.0.1:$MPORT" --protocol http2 --edge-ip-version 4
       --edge 127.0.0.1:7844)
    if [ "${AXH_CF_TRANSPORT:-}" = proxy-ns ]; then
      c=(tunnel --no-autoupdate --metrics "127.0.0.1:$MPORT" --protocol http2 --edge-ip-version 4
         --edge region1.v2.argotunnel.com:7844 --edge region2.v2.argotunnel.com:7844)
    fi
  fi
  if [ -s "$TOKEN_FILE" ]; then c+=(run --token-file "$TOKEN_FILE")
  else c+=(--url "$(L "$PORT")"); fi
  if [ "${AXH_CF_TRANSPORT:-}" = proxy-ns ]; then exec bash "$AXH_HOME/cloudflared-ns.sh" "$BIN/cloudflared" "${c[@]}"
  else exec "$BIN/cloudflared" "${c[@]}"; fi
}
tunnel_wanted() { [ -s "$TOKEN_FILE" ] || [ "${AXH_MODE:-}" = quick ] || [ -n "${AXH_HOSTNAME:-}" ]; }
initialized() { curl --noproxy '*' -fs -m 5 "$(L "$PORT")/admin/system/status" 2>/dev/null | grep -q '"isInitialized"[[:space:]]*:[[:space:]]*true'; }
tunnel_allowed() { tunnel_wanted && { initialized || [ "${AXH_ALLOW_UNINIT:-0}" = 1 ]; }; }

supervise() {  # 前台重启循环；崩溃退避，运行 ≥30s 才算稳定
  local n=$1 up start rc fails=0 sleeper=""
  case "$n" in axonhub|tunnel|shim|maa|axproxy) ;; *) die "unknown service $n";; esac
  lock_acquire "$n" "axh.sh supervise $n" || { echo "$n supervisor already running"; exit 0; }
  trap '[ -z "$sleeper" ] || kill "$sleeper" 2>/dev/null; kill_child "$n"; lock_release "$n"; exit 0' TERM INT
  trap "lock_release $n" EXIT  # Capture validated name; function-local n vanishes on return.
  log "$n supervisor up (pid $$)"
  while [ "$(sup_pid "$n")" = "$$" ] && [ ! -e "$RUN/maintenance" ]; do
    [ "$n" != maa ] || maa_wanted || break
    kill_child "$n"                       # 清孤儿，否则新进程会因端口被占而反复失败
    start=$(date +%s)
    "run_$n" >> "$LOGS/$n.log" 2>&1 &     # 子进程放后台再 wait，trap 才能及时响应
    echo $! > "$RUN/$n.child"
    wait $!; rc=$?
    up=$(( $(date +%s) - start )); log "$n exited rc=$rc after ${up}s"
    if [ "$up" -lt 30 ]; then fails=$((fails+1)); else fails=0; fi
    sleep $(( fails > 5 ? 60 : 2 + fails * 5 )) & sleeper=$!
    wait "$sleeper"; sleeper=""
  done
}

start_sup() {
  sup_alive "$1" && return 0
  local s=""; command -v setsid >/dev/null && s=setsid
  nohup $s bash "$SELF" supervise "$1" >> "$LOGS/supervisor.log" 2>&1 < /dev/null &
}
restart_sup() {  # Fresh supervisor starts with zero backoff; never clear maintenance.
  local n=$1 p i
  [ ! -e "$RUN/maintenance" ] || return 0
  p=$(sup_pid "$n")
  if is_ours "$p" "$SELF supervise $n"; then
    kill -TERM "$p" 2>/dev/null || true
    for i in $(seq 1 150); do is_ours "$p" "$SELF supervise $n" || break; sleep 0.1; done
    if is_ours "$p" "$SELF supervise $n"; then log "$n supervisor did not stop; restart deferred"; return 1; fi
  fi
  [ ! -e "$RUN/maintenance" ] || return 0
  log "$n supervisor restart with reset backoff"
  start_sup "$n"  # Its own startup cleans orphan children and stale locks.
}
ensure() {  # 拉起缺失的 supervisor；未初始化的实例不经隧道暴露（/system/initialize 无鉴权，先到先得）
  [ -e "$RUN/maintenance" ] && return 0
  ensure_forward || true
  if maa_wanted; then start_sup maa; fi
  start_sup axonhub
  if tunnel_allowed; then
    if proxy_mode; then start_sup shim; fi
    start_sup tunnel
  elif tunnel_wanted; then log "instance not initialized: tunnel withheld"; fi
}
check() {  # 打印不健康的组件名；全部健康返回 0。"进程活着"不等于"服务活着"
  local bad=""
  curl --noproxy '*' -fs -m 5 "$(L "$PORT")/health" >/dev/null 2>&1 || bad="$bad axonhub"
  if forward_wanted && ! forward_ready; then bad="$bad axproxy"; fi
  if tunnel_wanted && ! initialized && [ "${AXH_ALLOW_UNINIT:-0}" != 1 ]; then bad="$bad initialization"; fi
  if [ -n "${AXH_HOSTNAME:-}" ] && [ ! -s "$TOKEN_FILE" ]; then bad="$bad token-missing"; fi
  if maa_wanted; then
    maa_healthy || bad="$bad maa"
  fi
  if tunnel_allowed; then
    if proxy_mode; then is_ours "$(cat "$RUN/shim.child" 2>/dev/null)" "$(child_match shim)" || bad="$bad shim"; fi
    curl --noproxy '*' -fs -m 5 "$(L "$MPORT")/ready" >/dev/null 2>&1 || bad="$bad tunnel"
  fi   # /ready = 已连上 CF 边缘，CONNECT 200 或 quick URL 都不能替代它
  echo "${bad# }"; [ -z "$bad" ]
}
recent() { local stamp; stamp=$(stat -c %Y "$1" 2>/dev/null) || return 1; [ $(( $(date +%s) - stamp )) -le "$2" ]; }
wait_health() { local i; for i in $(seq 1 "${1:-60}"); do curl --noproxy '*' -fs -m 3 "$(L "$PORT")/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
rotate() {  # 原地截断，不 mv：supervisor 持有的 O_APPEND fd 仍然有效
  local f; for f in "$LOGS"/*.log; do [ -f "$f" ] && [ "$(stat -c %s "$f")" -gt 5242880 ] \
    && { tail -n 2000 "$f" > "$f.tmp" && cat "$f.tmp" > "$f"; rm -f "$f.tmp"; }; done; return 0
}

# ---------- 子命令 ----------
cmd_install() {
  command -v curl >/dev/null || die "need curl"
  local src helper
  src=$(cd "$(dirname "$0")" && pwd)
  for helper in cloudflared-edge-shim.py cloudflared-ns.sh axh_proxy.py axh-forward-proxy.py maa-status.py maa-install.sh muse-hooks.py muse-hook.sh; do
    [ -f "$src/$helper" ] || die "missing $src/$helper; copy all scripts together"
    if [ "$src" != "$AXH_HOME" ]; then
      install -m700 "$src/$helper" "$AXH_HOME/$helper.new" && mv -f "$AXH_HOME/$helper.new" "$AXH_HOME/$helper" || die "cannot install $helper"
    fi
  done
  [ "$0" != "$SELF" ] && [ -f "$0" ] && { cp -f "$0" "$SELF.new" && mv -f "$SELF.new" "$SELF"; }
  local A V zip tmp sums axon_cache="$CACHE/StarTrai1-axonhub"
  case "$(uname -m)" in x86_64|amd64) A=amd64;; aarch64|arm64) A=arm64;; *) die "unsupported arch $(uname -m)";; esac
  if [ ! -x "$BIN/axonhub" ] || [ "${AXH_UPGRADE:-0}" = 1 ]; then
    V="${AXONHUB_VERSION:-$(curl -fsSI -m 20 https://github.com/StarTrai1/axonhub/releases/latest | tr -d '\r' | awk -F/ 'tolower($1)~/^location/{print $NF}')}"
    [ -n "$V" ] || die "cannot resolve StarTrai1/axonhub version (set AXONHUB_VERSION to a tag published in that fork)"
    mkdir -p "$axon_cache"
    zip="$axon_cache/axonhub_${V#v}_linux_${A}.zip"
    if [ ! -s "$zip" ]; then
      curl -fsSL --retry 3 -m 600 -o "$zip.part" "https://github.com/StarTrai1/axonhub/releases/download/$V/$(basename "$zip")" || die "download axonhub failed"
      mv -f "$zip.part" "$zip"
    fi
    sums=$(curl -fsSL -m 30 "https://github.com/StarTrai1/axonhub/releases/download/$V/checksums.txt" 2>/dev/null | grep -F "$(basename "$zip")")
    if [ -n "$sums" ]; then ( cd "$axon_cache" && echo "$sums" | sha256sum -c - >/dev/null ) || { rm -f "$zip"; die "axonhub checksum mismatch"; }
    else log "WARN: checksums.txt unreachable, using cached zip unverified"; fi
    tmp=$(mktemp -d); { unzip -q -o "$zip" axonhub -d "$tmp" 2>/dev/null || python3 -m zipfile -e "$zip" "$tmp"; }
    [ -f "$tmp/axonhub" ] || die "unzip failed (need unzip or python3)"
    install -m755 "$tmp/axonhub" "$BIN/axonhub.new" && mv -f "$BIN/axonhub.new" "$BIN/axonhub"   # rename 替换：运行中也安全
    rm -rf "$tmp"; ls -t "$axon_cache"/axonhub_*.zip | tail -n +2 | xargs -r rm -f; echo "$V" > "$CACHE/axonhub.version"
    log "installed StarTrai1/axonhub $V"
  fi
  if [ ! -x "$BIN/cloudflared" ] || [ "${AXH_UPGRADE:-0}" = 1 ]; then
    curl -fsSL --retry 3 -m 300 -o "$BIN/cloudflared.new" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$A" || die "download cloudflared failed"
    chmod +x "$BIN/cloudflared.new"; "$BIN/cloudflared.new" --version >/dev/null 2>&1 || die "cloudflared binary invalid"
    mv -f "$BIN/cloudflared.new" "$BIN/cloudflared"; log "installed cloudflared"
  fi
  [ -f "$ENV_FILE" ] || { cat > "$ENV_FILE" <<'E'
# 按需取消注释。AXONHUB_* 会原样传给 axonhub（键名 = 配置路径大写、点换下划线）。
# AXH_HOSTNAME=axonhub.example.com            # 隧道公开主机名，status 用它做公网 HTTPS 自检
# AXH_MODE=quick                              # 无域名/令牌的临时方案，固定域名存在时不回退
# AXH_PLATFORM_REQUIRED=1                    # Muse: health 必须看到近期平台新会话执行记录
# AXH_MAA_ENABLED=1                          # maa-install + 凭据/当前 MUSE_VM_ID 就绪后启用
# AXH_MAA_DECISION=allow_always               # 上游默认：按域永久允许，失败回退单次；可选 allow_once
# MUSE_VM_ID=...                              # 当前 VM ID，重建后核实
# AXH_NODE_BIN=/absolute/path/to/node         # Node 22+，重建后仍存在的路径
# AXH_MAA_PROXY=edge                         # 默认直连；需要平台代理才设置 edge
# AXH_PROXY_LOCAL_PORT=18080                 # AxonHub/MAA 使用本地转发代理，无固定上游凭据
# AXH_DIRECT_HOSTS=api.github.com,raw.githubusercontent.com  # 仅现场直连验证通过后启用
# AXH_PUBLIC_PROBE=direct                    # 默认独立直连；显式 local-proxy 可经 axproxy 验证
# AXH_CF_PROTOCOL=http2                       # UDP 7844 被封时改 http2（走 TCP 7844）
# AXH_CF_TRANSPORT=proxy                      # Muse: HTTP/2 + CONNECT shim，无需 mount 权限
# AXH_CF_TRANSPORT=proxy-ns                   # 可选：社区 hosts + mount namespace 路径
# AXH_EDGE_TARGETS=region1.v2.argotunnel.com,region2.v2.argotunnel.com
# AXH_WATCHDOG_BACKEND=loop                   # auto 默认优先工作中的 cron，否则每 60s 循环
# AXONHUB_SERVER_SSE_KEEP_ALIVE_ENABLED=true  # 流式响应保活，避免 CF 100s 无字节触发 524
# AXONHUB_SERVER_SSE_KEEP_ALIVE_INTERVAL=15s
# AXONHUB_DB_DIALECT=postgres                 # 默认 sqlite（$AXH_HOME/data）；换库时同时设置 AXONHUB_DB_DSN
E
  chmod 600 "$ENV_FILE"; }
  echo "installed: $("$BIN/axonhub" version 2>&1 | tail -1) / $("$BIN/cloudflared" --version | head -1)"
}
cmd_set_token() {  # 从 stdin 读：可直接粘贴 Cloudflare 面板的整条 "cloudflared ... install eyJ..." 命令
  local t; t=$(grep -oE 'eyJ[A-Za-z0-9_=+/-]{20,}' | head -1); [ -n "$t" ] || die "no tunnel token found on stdin"
  ( umask 077; printf '%s' "$t" > "$TOKEN_FILE" ); echo "token saved (${#t} chars, mode 600)"
}
cmd_set_proxy() {
  command -v python3 >/dev/null || die "proxy transport needs Python 3.11+"
  local result
  [ -n "$AXH_PROXY_INPUT" ] || { echo "fresh session proxy missing; saved credentials were not re-stamped" >&2; return 2; }
  result=$(python3 "$AXH_HOME/axh_proxy.py" save) || return 2
  load_proxy || return 2
  if [ "$result" = 'proxy updated' ] && [ "${AXH_MAA_PROXY:-direct}" = edge ] && maa_wanted &&
     [ ! -e "$RUN/maintenance" ] && ! maa_healthy; then
    ensure_forward || return 1
    restart_sup maa || return 1
  fi
  echo "$result (mode 600; new CONNECTs reload it; existing tunnels preserved)"
}
cmd_doctor() {
  command -v python3 >/dev/null || die "proxy transport needs Python 3.11+"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "proxy transport needs Python 3.11+"
  local rc=0
  if [ "${AXH_CF_TRANSPORT:-}" = proxy-ns ]; then
    bash "$AXH_HOME/cloudflared-ns.sh" --check && echo "private hosts namespace: OK" || { echo "private hosts namespace: FAIL (requires permitted mount namespace / CAP_SYS_ADMIN; use proxy transport if unavailable)"; rc=1; }
  else echo "edge route: explicit 127.0.0.1:7844 (mount namespace not required)"; fi
  python3 "$AXH_HOME/cloudflared-edge-shim.py" --probe || rc=1
  echo "probe is diagnostic; success still requires cloudflared /ready and public HTTPS /health"
  return "$rc"
}
cmd_start() {
  case "${AXH_CF_TRANSPORT:-direct}" in direct|proxy|proxy-ns) ;; *) die "AXH_CF_TRANSPORT must be direct, proxy or proxy-ns";; esac
  rm -f "$RUN/maintenance"; [ -x "$BIN/axonhub" ] || die "run install first"
  ensure_forward || true
  start_sup axonhub; wait_health 60 || log "axonhub not healthy after 60s (see logs/axonhub.log)"
  ensure; sleep 2; cmd_status
}
cmd_stop() {
  local n p i; touch "$RUN/maintenance"   # 维护模式：watchdog 不再拉起
  for n in tunnel shim axonhub maa axproxy; do p=$(sup_pid "$n"); is_ours "$p" "axh.sh supervise $n" && kill -TERM "$p"; done
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do sup_alive axonhub || sup_alive tunnel || sup_alive shim || sup_alive maa || sup_alive axproxy || break; sleep 1; done
  for n in tunnel shim axonhub maa axproxy; do kill_child "$n"; done
  echo "stopped (maintenance mode; 'start' to resume)"
}
public_probe() {
  case "${AXH_PUBLIC_PROBE:-direct}" in
    direct) curl --noproxy '*' -fs -m 10 "https://$AXH_HOSTNAME/health";;
    local-proxy) curl --noproxy '' --proxy "$(forward_url)" -fs -m 10 "https://$AXH_HOSTNAME/health";;
    *) return 2;;
  esac
}
cmd_status() {
  local n u
  for n in axonhub tunnel; do printf '%-9s supervisor: %s\n' "$n" "$(sup_alive "$n" && echo up || echo down)"; done
  echo "transport          : ${AXH_CF_TRANSPORT:-direct}"
  if forward_wanted; then echo "axproxy   ready     : $(forward_ready && echo yes || echo NO) (local TCP only)"; fi
  if proxy_mode; then echo "shim      supervisor: $(sup_alive shim && echo up || echo down)"; fi
  echo "watchdog  backend  : $(cat "$RUN/watchdog.backend" 2>/dev/null || echo '<not installed>')"
  echo "watchdog  last tick: $(cat "$RUN/watchdog.tick" 2>/dev/null || echo '<none>')"
  echo "platform last exec: $(cat "$RUN/session.tick" 2>/dev/null || echo '<not observed>')"
  echo "boot id           : $(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)"
  if proxy_mode; then python3 "$AXH_HOME/axh_proxy.py" status; fi
  if maa_wanted; then
    echo "MAA successful poll: $(maa_healthy && echo recent || echo MISSING/STALE)"
    python3 "$AXH_HOME/maa-status.py"
  fi
  echo "axonhub   health    : $(curl --noproxy '*' -fs -m 5 "$(L "$PORT")/health" >/dev/null 2>&1 && echo ok || echo FAIL)"
  echo "axonhub   initialized: $(initialized && echo yes || echo NO)"
  if tunnel_wanted; then
    echo "tunnel    mode      : $([ -s "$TOKEN_FILE" ] && echo named || { [ -n "${AXH_HOSTNAME:-}" ] && echo 'named (token missing)' || echo quick; })"
    echo "tunnel    ready     : $(curl --noproxy '*' -fs -m 5 "$(L "$MPORT")/ready" >/dev/null 2>&1 && echo yes || echo NO)"
    u=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOGS/tunnel.log" 2>/dev/null | grep -v '^https://api\.' | tail -1)
    if [ ! -s "$TOKEN_FILE" ] && [ -z "${AXH_HOSTNAME:-}" ]; then echo "tunnel    url       : ${u:-<pending>}"; fi
    [ -n "${AXH_HOSTNAME:-}" ] && echo "public    https     : $(public_probe >/dev/null 2>&1 && echo ok || echo FAIL) (https://$AXH_HOSTNAME; ${AXH_PUBLIC_PROBE:-direct})"
  else echo "tunnel    : not configured (run set-token, or AXH_MODE=quick in env)"; fi
  return 0
}
cmd_health() {  # 退出码即契约：0 健康 / 1 需要恢复 / 2 无法判断。供外部巡检调用
  command -v curl >/dev/null || { echo "UNKNOWN: no curl"; exit 2; }
  [ -e "$RUN/maintenance" ] && { echo "MAINTENANCE"; exit 0; }
  [ -x "$BIN/axonhub" ] || { echo "REBUILD: binaries missing"; exit 1; }
  local bad backend; bad=$(check)
  backend=$(cat "$RUN/watchdog.backend" 2>/dev/null)
  if [ "$backend" = loop ] && ! loop_alive; then bad="$bad watchdog-loop"; fi
  if [ "$backend" = cron ] && ! pgrep -x cron >/dev/null && ! pgrep -x crond >/dev/null; then bad="$bad cron"; fi
  if [ -n "$backend" ] && ! recent "$RUN/watchdog.tick" 180; then bad="$bad watchdog-stale"; fi
  if [ "${AXH_PLATFORM_REQUIRED:-0}" = 1 ] && ! recent "$RUN/session.tick" 180; then bad="$bad platform-stale"; fi
  [ -n "$bad" ] || { echo OK; exit 0; }
  echo "DEGRADED: $bad"; exit 1
}
cmd_watchdog() {  # cron 每分钟：单实例 → 补装 → 拉起缺失 supervisor → 连续 3 次不健康则杀子进程重启
  [ -e "$RUN/maintenance" ] && exit 0
  lock_acquire watchdog "axh.sh watchdog" || exit 0; trap 'lock_release watchdog' EXIT
  rotate
  [ -x "$BIN/axonhub" ] && [ -x "$BIN/cloudflared" ] || { log "binaries missing, reinstalling"; cmd_install >> "$LOGS/axh.log" 2>&1; }
  ensure
  local bad n c; bad=" $(check) "
  for n in axonhub tunnel shim maa axproxy; do
    case "$bad" in
      *" $n "*) c=$(( $(cat "$RUN/$n.bad" 2>/dev/null || echo 0) + 1 )); echo "$c" > "$RUN/$n.bad"; log "$n unhealthy ($c)"
                if [ "$c" -ge 3 ] && sup_alive "$n"; then
                  log "$n wedged, restarting child"; kill_child "$n"
                  if [ "$n" = tunnel ] && proxy_mode; then kill_child shim; fi
                  echo 0 > "$RUN/$n.bad"
                fi;;
      *) echo 0 > "$RUN/$n.bad";;
    esac
  done
  date -u '+%Y-%m-%dT%H:%M:%SZ' > "$RUN/watchdog.tick"  # Finished iteration, not merely entered.
}
loop_alive() { is_ours "$(sup_pid watchdog-loop)" "$SELF watchdog-loop"; }
cmd_watchdog_loop() {
  lock_acquire watchdog-loop "$SELF watchdog-loop" || return 0
  local sleeper=""
  trap '[ -z "$sleeper" ] || kill "$sleeper" 2>/dev/null; lock_release watchdog-loop; exit 0' TERM INT
  trap 'lock_release watchdog-loop' EXIT
  log "watchdog loop up (pid $$, interval 60s)"
  while [ "$(sup_pid watchdog-loop)" = "$$" ]; do
    # Separate process reloads env on each tick and owns its own watchdog lock.
    bash "$SELF" watchdog >> "$LOGS/watchdog.log" 2>&1
    sleep 60 & sleeper=$!; wait "$sleeper"; sleeper=""
  done
}
cmd_boot() {  # 不安装系统包；cron 不可用时直接使用持久目录中的循环。
  local backend="${AXH_WATCHDOG_BACKEND:-auto}" cron_ok=0 p s=""
  case "$backend" in auto|cron|loop) ;; *) die "AXH_WATCHDOG_BACKEND must be auto, cron or loop";; esac
  if [ "$backend" != loop ] && command -v crontab >/dev/null; then
    # crontab 存在不代表 daemon 存活。只做有时间上限的启动尝试。
    if ! pgrep -x cron >/dev/null && ! pgrep -x crond >/dev/null; then
      timeout 10 service cron start >/dev/null 2>&1 || true
    fi
    if pgrep -x cron >/dev/null || pgrep -x crond >/dev/null; then
    ( crontab -l 2>/dev/null | grep -vF -e "axh.sh watchdog" -e "axh.sh restore"
      printf '* * * * * AXH_HOME=%q /bin/bash %q watchdog >> %q 2>&1\n' "$AXH_HOME" "$SELF" "$LOGS/watchdog.log"
      printf '@reboot AXH_HOME=%q /bin/bash %q restore >> %q 2>&1\n' "$AXH_HOME" "$SELF" "$LOGS/boot.log" ) | crontab - && cron_ok=1
    fi
  fi
  if [ "$cron_ok" = 1 ]; then
    echo cron > "$RUN/watchdog.backend"
    if loop_alive; then p=$(sup_pid watchdog-loop); kill -TERM "$p"; fi
    echo "watchdog: cron installed, daemon running (verify last tick after one minute)"
  else
    [ "$backend" != cron ] || echo "WARN: cron unavailable; using loop"
    # Remove our old minute job on an explicit backend switch; keep @reboot.
    if [ "$backend" = loop ] && command -v crontab >/dev/null; then
      ( crontab -l 2>/dev/null | grep -vF "$SELF watchdog" ) | crontab - || true
    fi
    echo loop > "$RUN/watchdog.backend"
    if ! loop_alive; then
      command -v setsid >/dev/null && s=setsid
      nohup $s bash "$SELF" watchdog-loop >> "$LOGS/watchdog.log" 2>&1 < /dev/null &
    fi
    echo "watchdog: 60s loop (no cron dependency; platform hook still needed after rebuild)"
  fi
  if [ -d /run/systemd/system ] && [ "$(id -u)" = 0 ] && timeout 5 systemctl show-environment >/dev/null 2>&1; then
    cat > /etc/systemd/system/axonhub-stack.service <<U
[Unit]
Description=AxonHub + Cloudflare Tunnel keepalive
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
Environment="AXH_HOME=$AXH_HOME"
ExecStart=/bin/bash "$SELF" restore
[Install]
WantedBy=multi-user.target
U
    systemctl daemon-reload && systemctl enable axonhub-stack.service >/dev/null 2>&1 && echo "systemd boot hook enabled; runtime supervised by axh.sh"
  fi
  return 0
}
cmd_restore() {
  lock_acquire restore "$SELF restore" || { echo "restore already running"; return 0; }
  trap 'lock_release restore' EXIT
  local rc=0
  cmd_install || return 1
  cmd_boot
  [ ! -e "$RUN/maintenance" ] || { echo "MAINTENANCE: use start to resume"; return 0; }
  if maa_wanted; then
    bash "$AXH_HOME/maa-install.sh" >> "$LOGS/maa-install.log" 2>&1 || { log "MAA dependency restore failed; see maa-install.log"; rc=1; }
  fi
  cmd_start
  return "$rc"
}   # 幂等，可反复执行；自动恢复不能撤销用户的 stop。
cmd_session_restore() {  # Fresh platform exec only, never the local loop.
  [ -e "$RUN/maintenance" ] && { echo MAINTENANCE; return 0; }
  lock_acquire session "$SELF session-restore" || { echo "session recovery already running"; return 2; }
  trap 'lock_release session' EXIT
  local proxy_rc=0 i bad boot_id
  date -u '+%Y-%m-%dT%H:%M:%SZ' > "$RUN/session.tick"
  boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)
  if [ "$(cat "$RUN/session.boot" 2>/dev/null)" != "$boot_id" ]; then
    log "platform session observed new boot id=$boot_id"; echo "$boot_id" > "$RUN/session.boot"
  fi
  if proxy_mode; then cmd_set_proxy || proxy_rc=$?; fi
  if bash "$SELF" health >/dev/null 2>&1 && [ "$proxy_rc" = 0 ]; then echo OK; return 0; fi
  bash "$SELF" restore >> "$LOGS/session.log" 2>&1 || return 1
  bash "$SELF" watchdog >> "$LOGS/session.log" 2>&1 || return 1
  if ! curl --noproxy '*' -fs -m 3 "$(L "$MPORT")/ready" >/dev/null 2>&1 && tunnel_allowed; then
    kill_child tunnel  # One bounded retry; preserve healthy existing connections.
  fi
  for i in $(seq 1 15); do
    bad=$(check)
    [ -n "$bad" ] || break
    sleep 2
  done
  if [ "$proxy_rc" != 0 ]; then echo "NEEDS_FRESH_PROXY: local recovery attempted; platform must supply a fresh exec environment"; return 2; fi
  bash "$SELF" health
}
cmd_upgrade() { AXH_UPGRADE=1 cmd_install && { kill_child axonhub; kill_child tunnel; sleep 3; cmd_status; }; }

case "${1:-}" in
  install) cmd_install;; set-token) cmd_set_token;; set-proxy) cmd_set_proxy;; doctor) cmd_doctor;; start) cmd_start;; stop) cmd_stop;; status) cmd_status;;
  health) cmd_health;; watchdog) cmd_watchdog;; boot) cmd_boot;; restore) cmd_restore;; upgrade) cmd_upgrade;;
  supervise) supervise "${2:-}";;
  watchdog-loop) cmd_watchdog_loop;;
  session-restore) cmd_session_restore;;
  maa-install) bash "$AXH_HOME/maa-install.sh";;
  muse-hooks) python3 "$AXH_HOME/muse-hooks.py" "${2:?hooks directory required}";;
  *) sed -n '2,4p' "$0"; exit 1;;
esac

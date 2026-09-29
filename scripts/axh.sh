#!/usr/bin/env bash
# axh.sh — AxonHub + Cloudflare Tunnel：安装 / 保活 / 恢复（单文件、幂等）
# 用法: axh.sh install | set-token | start | stop | status | health | watchdog | boot | restore | upgrade
# 所有状态都在 $AXH_HOME（默认 ~/axonhub-stack），把它放在持久盘上。
set -u
export HOME="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}"   # cron/systemd 下 HOME 可能缺失
AXH_HOME="${AXH_HOME:-$HOME/axonhub-stack}"
mkdir -p "$AXH_HOME"/{bin,data,run,logs,cache} && chmod 700 "$AXH_HOME"
ENV_FILE="$AXH_HOME/env"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
PORT="${AXH_PORT:-8090}"; MPORT="${AXH_METRICS_PORT:-20241}"
BIN="$AXH_HOME/bin"; DATA="$AXH_HOME/data"; RUN="$AXH_HOME/run"; LOGS="$AXH_HOME/logs"; CACHE="$AXH_HOME/cache"
TOKEN_FILE="$AXH_HOME/tunnel.token"; SELF="$AXH_HOME/axh.sh"
export PATH="$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

log() { echo "[$(date '+%F %T')] $*" >> "$LOGS/axh.log"; }
die() { echo "ERROR: $*" >&2; exit 1; }
L() { echo "http://127.0.0.1:$1"; }

# ---------- 进程校验：永远 pidfile + /proc/<pid>/cmdline，不用 pkill -f ----------
is_ours() { [ -n "${1:-}" ] && [ -r "/proc/$1/cmdline" ] && tr '\0' ' ' < "/proc/$1/cmdline" | grep -qF -- "$2"; }
sup_pid() { cat "$RUN/$1.lock/pid" 2>/dev/null; }
sup_alive() { is_ours "$(sup_pid "$1")" "axh.sh supervise $1"; }
child_match() { case "$1" in axonhub) echo "$BIN/axonhub";; tunnel) echo "$BIN/cloudflared";; esac; }
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

# ---------- 被监督的两个服务 ----------
run_axonhub() {
  cd "$DATA" || exit 1   # axonhub 默认在 cwd 建 sqlite 库
  export AXONHUB_SERVER_HOST="${AXONHUB_SERVER_HOST:-127.0.0.1}" AXONHUB_SERVER_PORT="$PORT"
  exec "$BIN/axonhub"
}
run_tunnel() {
  local c=(tunnel --no-autoupdate --metrics "127.0.0.1:$MPORT" --protocol "${AXH_CF_PROTOCOL:-auto}")
  if [ -s "$TOKEN_FILE" ]; then exec "$BIN/cloudflared" "${c[@]}" run --token-file "$TOKEN_FILE"   # 令牌不进 argv
  else exec "$BIN/cloudflared" "${c[@]}" --url "$(L "$PORT")"; fi                                  # quick 模式
}
tunnel_wanted() { [ -s "$TOKEN_FILE" ] || [ "${AXH_MODE:-}" = quick ]; }
initialized() { curl -fs -m 5 "$(L "$PORT")/admin/system/status" 2>/dev/null | grep -q '"isInitialized":true'; }
tunnel_allowed() { tunnel_wanted && { initialized || [ "${AXH_ALLOW_UNINIT:-0}" = 1 ]; }; }

supervise() {  # 前台重启循环；崩溃退避，运行 ≥30s 才算稳定
  local n=$1 up start rc fails=0
  case "$n" in axonhub|tunnel) ;; *) die "unknown service $n";; esac
  lock_acquire "$n" "axh.sh supervise $n" || { echo "$n supervisor already running"; exit 0; }
  trap 'kill_child "$n"; lock_release "$n"; exit 0' TERM INT
  trap 'lock_release "$n"' EXIT
  log "$n supervisor up (pid $$)"
  while [ "$(sup_pid "$n")" = "$$" ] && [ ! -e "$RUN/maintenance" ]; do
    kill_child "$n"                       # 清孤儿，否则新进程会因端口被占而反复失败
    start=$(date +%s)
    "run_$n" >> "$LOGS/$n.log" 2>&1 &     # 子进程放后台再 wait，trap 才能及时响应
    echo $! > "$RUN/$n.child"
    wait $!; rc=$?
    up=$(( $(date +%s) - start )); log "$n exited rc=$rc after ${up}s"
    if [ "$up" -lt 30 ]; then fails=$((fails+1)); else fails=0; fi
    sleep $(( fails > 5 ? 60 : 2 + fails * 5 ))
  done
}

start_sup() {
  sup_alive "$1" && return 0
  local s=""; command -v setsid >/dev/null && s=setsid
  nohup $s bash "$SELF" supervise "$1" >> "$LOGS/supervisor.log" 2>&1 < /dev/null &
}
ensure() {  # 拉起缺失的 supervisor；未初始化的实例不经隧道暴露（/system/initialize 无鉴权，先到先得）
  [ -e "$RUN/maintenance" ] && return 0
  start_sup axonhub
  if tunnel_allowed; then start_sup tunnel
  elif tunnel_wanted; then log "instance not initialized: tunnel withheld"; fi
}
check() {  # 打印不健康的组件名；全部健康返回 0。"进程活着"不等于"服务活着"
  local bad=""
  curl -fs -m 5 "$(L "$PORT")/health" >/dev/null 2>&1 || bad="$bad axonhub"
  if tunnel_allowed; then curl -fs -m 5 "$(L "$MPORT")/ready" >/dev/null 2>&1 || bad="$bad tunnel"; fi   # /ready = 已连上 CF 边缘
  echo "${bad# }"; [ -z "$bad" ]
}
wait_health() { local i; for i in $(seq 1 "${1:-60}"); do curl -fs -m 3 "$(L "$PORT")/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
rotate() {  # 原地截断，不 mv：supervisor 持有的 O_APPEND fd 仍然有效
  local f; for f in "$LOGS"/*.log; do [ -f "$f" ] && [ "$(stat -c %s "$f")" -gt 5242880 ] \
    && { tail -n 2000 "$f" > "$f.tmp" && cat "$f.tmp" > "$f"; rm -f "$f.tmp"; }; done; return 0
}

# ---------- 子命令 ----------
cmd_install() {
  command -v curl >/dev/null || die "need curl"
  [ "$0" != "$SELF" ] && [ -f "$0" ] && { cp -f "$0" "$SELF.new" && mv -f "$SELF.new" "$SELF"; }
  local A V zip tmp sums
  case "$(uname -m)" in x86_64|amd64) A=amd64;; aarch64|arm64) A=arm64;; *) die "unsupported arch $(uname -m)";; esac
  if [ ! -x "$BIN/axonhub" ] || [ "${AXH_UPGRADE:-0}" = 1 ]; then
    V="${AXONHUB_VERSION:-$(curl -fsSI -m 20 https://github.com/looplj/axonhub/releases/latest | tr -d '\r' | awk -F/ 'tolower($1)~/^location/{print $NF}')}"
    [ -n "$V" ] || die "cannot resolve axonhub version (set AXONHUB_VERSION=v1.0.0-beta10)"
    zip="$CACHE/axonhub_${V#v}_linux_${A}.zip"
    if [ ! -s "$zip" ]; then
      curl -fsSL --retry 3 -m 600 -o "$zip.part" "https://github.com/looplj/axonhub/releases/download/$V/$(basename "$zip")" || die "download axonhub failed"
      mv -f "$zip.part" "$zip"
    fi
    sums=$(curl -fsSL -m 30 "https://github.com/looplj/axonhub/releases/download/$V/checksums.txt" 2>/dev/null | grep -F "$(basename "$zip")")
    if [ -n "$sums" ]; then ( cd "$CACHE" && echo "$sums" | sha256sum -c - >/dev/null ) || { rm -f "$zip"; die "axonhub checksum mismatch"; }
    else log "WARN: checksums.txt unreachable, using cached zip unverified"; fi
    tmp=$(mktemp -d); { unzip -q -o "$zip" axonhub -d "$tmp" 2>/dev/null || python3 -m zipfile -e "$zip" "$tmp"; }
    [ -f "$tmp/axonhub" ] || die "unzip failed (need unzip or python3)"
    install -m755 "$tmp/axonhub" "$BIN/axonhub.new" && mv -f "$BIN/axonhub.new" "$BIN/axonhub"   # rename 替换：运行中也安全
    rm -rf "$tmp"; ls -t "$CACHE"/axonhub_*.zip | tail -n +2 | xargs -r rm -f; echo "$V" > "$CACHE/axonhub.version"
    log "installed axonhub $V"
  fi
  if [ ! -x "$BIN/cloudflared" ] || [ "${AXH_UPGRADE:-0}" = 1 ]; then
    curl -fsSL --retry 3 -m 300 -o "$BIN/cloudflared.new" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$A" || die "download cloudflared failed"
    chmod +x "$BIN/cloudflared.new"; "$BIN/cloudflared.new" --version >/dev/null 2>&1 || die "cloudflared binary invalid"
    mv -f "$BIN/cloudflared.new" "$BIN/cloudflared"; log "installed cloudflared"
  fi
  [ -f "$ENV_FILE" ] || { cat > "$ENV_FILE" <<'E'
# 按需取消注释。AXONHUB_* 会原样传给 axonhub（键名 = 配置路径大写、点换下划线）。
# AXH_HOSTNAME=axonhub.example.com            # 隧道公开主机名，status 用它做公网 HTTPS 自检
# AXH_MODE=quick                              # 无域名/令牌的临时方案：随机 trycloudflare.com 地址，重启即变
# AXH_CF_PROTOCOL=http2                       # UDP 7844 被封时改 http2（走 TCP 7844）
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
cmd_start() {
  rm -f "$RUN/maintenance"; [ -x "$BIN/axonhub" ] || die "run install first"
  start_sup axonhub; wait_health 60 || log "axonhub not healthy after 60s (see logs/axonhub.log)"
  ensure; sleep 2; cmd_status
}
cmd_stop() {
  local n p i; touch "$RUN/maintenance"   # 维护模式：watchdog 不再拉起
  for n in tunnel axonhub; do p=$(sup_pid "$n"); is_ours "$p" "axh.sh supervise $n" && kill -TERM "$p"; done
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do sup_alive axonhub || sup_alive tunnel || break; sleep 1; done
  kill_child tunnel; kill_child axonhub; echo "stopped (maintenance mode; 'start' to resume)"
}
cmd_status() {
  local n u
  for n in axonhub tunnel; do printf '%-9s supervisor: %s\n' "$n" "$(sup_alive "$n" && echo up || echo down)"; done
  echo "axonhub   health    : $(curl -fs -m 5 "$(L "$PORT")/health" >/dev/null 2>&1 && echo ok || echo FAIL)"
  echo "axonhub   initialized: $(initialized && echo yes || echo NO)"
  if tunnel_wanted; then
    echo "tunnel    mode      : $([ -s "$TOKEN_FILE" ] && echo named || echo quick)"
    echo "tunnel    ready     : $(curl -fs -m 5 "$(L "$MPORT")/ready" >/dev/null 2>&1 && echo yes || echo NO)"
    u=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOGS/tunnel.log" 2>/dev/null | grep -v '^https://api\.' | tail -1)
    [ -s "$TOKEN_FILE" ] || echo "tunnel    url       : ${u:-<pending>}"
    [ -n "${AXH_HOSTNAME:-}" ] && echo "public    https     : $(curl -fs -m 10 "https://$AXH_HOSTNAME/health" >/dev/null 2>&1 && echo ok || echo FAIL) (https://$AXH_HOSTNAME)"
  else echo "tunnel    : not configured (run set-token, or AXH_MODE=quick in env)"; fi
}
cmd_health() {  # 退出码即契约：0 健康 / 1 需要恢复 / 2 无法判断。供外部巡检调用
  command -v curl >/dev/null || { echo "UNKNOWN: no curl"; exit 2; }
  [ -e "$RUN/maintenance" ] && { echo "MAINTENANCE"; exit 0; }
  [ -x "$BIN/axonhub" ] || { echo "REBUILD: binaries missing"; exit 1; }
  local bad; bad=$(check) && { echo OK; exit 0; }
  echo "DEGRADED: $bad"; exit 1
}
cmd_watchdog() {  # cron 每分钟：单实例 → 补装 → 拉起缺失 supervisor → 连续 3 次不健康则杀子进程重启
  [ -e "$RUN/maintenance" ] && exit 0
  lock_acquire watchdog "axh.sh watchdog" || exit 0; trap 'lock_release watchdog' EXIT
  rotate
  [ -x "$BIN/axonhub" ] && [ -x "$BIN/cloudflared" ] || { log "binaries missing, reinstalling"; cmd_install >> "$LOGS/axh.log" 2>&1; }
  ensure
  local bad n c; bad=" $(check) "
  for n in axonhub tunnel; do
    case "$bad" in
      *" $n "*) c=$(( $(cat "$RUN/$n.bad" 2>/dev/null || echo 0) + 1 )); echo "$c" > "$RUN/$n.bad"; log "$n unhealthy ($c)"
                if [ "$c" -ge 3 ] && sup_alive "$n"; then log "$n wedged, restarting child"; kill_child "$n"; echo 0 > "$RUN/$n.bad"; fi;;
      *) echo 0 > "$RUN/$n.bad";;
    esac
  done
}
cmd_boot() {  # 开机自启 + 每分钟看门狗。ephemeral 机器这些会随重建消失 → 由外部巡检/平台开机钩子调用 restore
  if command -v crontab >/dev/null; then
    ( crontab -l 2>/dev/null | grep -vF -e "axh.sh watchdog" -e "axh.sh restore"
      echo "* * * * * AXH_HOME=$AXH_HOME bash $SELF watchdog >/dev/null 2>&1"
      echo "@reboot AXH_HOME=$AXH_HOME bash $SELF restore >> $LOGS/boot.log 2>&1" ) | crontab -
    pgrep -x cron >/dev/null || pgrep -x crond >/dev/null || { service cron start >/dev/null 2>&1 || cron 2>/dev/null || echo "WARN: cron daemon not running"; }
  else echo "WARN: no crontab; rely on external patrol calling '$SELF health' / 'restore'"; fi
  if [ -d /run/systemd/system ] && [ "$(id -u)" = 0 ]; then
    cat > /etc/systemd/system/axonhub-stack.service <<U
[Unit]
Description=AxonHub + Cloudflare Tunnel keepalive
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
Environment=HOME=$HOME
Environment=AXH_HOME=$AXH_HOME
ExecStart=/bin/bash $SELF restore
ExecStop=/bin/bash $SELF stop
[Install]
WantedBy=multi-user.target
U
    systemctl daemon-reload && systemctl enable axonhub-stack.service >/dev/null 2>&1 && echo "systemd unit enabled"
  fi
}
cmd_restore() { cmd_install || exit 1; cmd_boot; cmd_start; }   # 幂等，可反复执行
cmd_upgrade() { AXH_UPGRADE=1 cmd_install && { kill_child axonhub; kill_child tunnel; sleep 3; cmd_status; }; }

case "${1:-}" in
  install) cmd_install;; set-token) cmd_set_token;; start) cmd_start;; stop) cmd_stop;; status) cmd_status;;
  health) cmd_health;; watchdog) cmd_watchdog;; boot) cmd_boot;; restore) cmd_restore;; upgrade) cmd_upgrade;;
  supervise) supervise "${2:-}";;
  *) sed -n '2,4p' "$0"; exit 1;;
esac

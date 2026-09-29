#!/usr/bin/env bash
# Only cloudflared sees the hosts overlay. No global hosts edits or service install.
set -euo pipefail
command -v unshare >/dev/null || { echo 'missing unshare (util-linux)' >&2; exit 1; }
command -v mount >/dev/null || { echo 'missing mount' >&2; exit 1; }
if [ "${1:-}" != --inside ]; then
  exec unshare --mount --propagation private bash "$0" --inside "$@"
fi
shift
stack=${AXH_HOME:?AXH_HOME required}
hosts=$(mktemp "$stack/run/hosts.edge.XXXXXX")
trap 'rm -f "$hosts"' EXIT
python3 - "$hosts" <<'PY'
import sys
from pathlib import Path
regions = {"region1.v2.argotunnel.com", "region2.v2.argotunnel.com"}
lines = []
for line in Path('/etc/hosts').read_text().splitlines():
    fields = line.split('#', 1)[0].split()
    if len(fields) > 1 and any(x.rstrip('.').lower() in regions for x in fields[1:]):
        aliases = [x for x in fields[1:] if x.rstrip('.').lower() not in regions]
        if aliases:
            lines.append(fields[0] + ' ' + ' '.join(aliases))
    else:
        lines.append(line)
lines.append('127.0.0.1 ' + ' '.join(sorted(regions)))
Path(sys.argv[1]).write_text('\n'.join(lines) + '\n')
PY
mount --bind "$hosts" /etc/hosts
rm -f "$hosts"   # The namespace's bind mount keeps this inode alive.
if [ "${1:-}" = --check ]; then
  python3 -c 'import socket; assert all(socket.gethostbyname(n) == "127.0.0.1" for n in ("region1.v2.argotunnel.com", "region2.v2.argotunnel.com"))'
else
  exec "$@"   # Keep the supervisor's child PID through unshare, bash and exec.
fi

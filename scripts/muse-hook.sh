#!/usr/bin/env bash
# Invoked by the platform runtime, not by the sandbox's watchdog.
set -euo pipefail
source "${HATCH_HOOK_RUNTIME:?platform hook runtime required}"
[[ "${HATCH_HOOK_DRY_RUN:-0}" == 1 ]] && { silent 'dry-run'; exit 0; }
export AXH_HOME=${AXH_HOME:?persistent stack path required}
[[ -e "$AXH_HOME/run/maintenance" ]] && { silent 'maintenance'; exit 0; }
now=$(date +%s)
tick=$(stat -c %Y "$AXH_HOME/run/session.tick" 2>/dev/null || echo 0)
boot_id=$(cat /proc/sys/kernel/random/boot_id)
previous=$(cat "$AXH_HOME/run/session.boot" 2>/dev/null || true)
# A recent fresh exec covers periodic renewal. Service failure can wake sooner.
if [[ "$boot_id" == "$previous" && $((now-tick)) -lt 60 ]] && \
   timeout 30 bash "$AXH_HOME/axh.sh" health >/dev/null 2>&1; then
  silent 'recent session and healthy'; exit 0
fi
# Never capture this hook process's env as if it were fresh credentials.
# wake requests the platform Agent to run the definition's recovery prompt.
wake 'AxonHub needs a fresh exec for proxy handoff and recovery; run session-restore'

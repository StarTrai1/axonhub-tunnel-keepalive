#!/usr/bin/env python3
"""Register a dedicated Muse polling hook; preserve existing home-init hooks."""
import json
import os
from pathlib import Path
import shlex
import sys
import time


def main():
    stack = Path(os.environ['AXH_HOME']).resolve()
    hooks = Path(sys.argv[1]).resolve()
    if not (hooks / 'definitions').is_dir() or not (hooks / 'scripts').is_dir():
        raise SystemExit('Verify the actual Muse hooks/definitions and hooks/scripts directories first')
    name = 'axonhub-session-recovery'
    definition = hooks / 'definitions' / (name + '.json')
    launcher = hooks / 'scripts' / (name + '.sh')
    marker = '# managed by axonhub-tunnel-keepalive'
    if launcher.exists() and marker not in launcher.read_text():
        raise SystemExit('Existing unowned hook launcher; refusing overwrite')
    if definition.exists() and json.loads(definition.read_text()).get('id') != name:
        raise SystemExit('Existing unowned hook definition; refusing overwrite')
    command = f'AXH_HOME={shlex.quote(str(stack))} bash {shlex.quote(str(stack / "axh.sh"))} session-restore'
    prompt = ('Use a NEW platform exec with its current injected proxy environment. Do not source saved '
              'proxy.env/proxy.json or reuse an old shell. Run once: ' + command +
              '. This command captures current proxy credentials even when health is already OK. '
              'Exit 0: remain silent. Exit 1: report sanitized health/log evidence; no infinite retry. '
              'Exit 2: fresh proxy or environment unavailable; retry in the next scheduled round, '
              'report after three consecutive rounds. Preserve named Tunnel token/domain and owner data; '
              'never switch to Quick Tunnel. Respect maintenance. Verify public HTTPS /health after recovery. '
              'Do not print credentials, proxy URLs, tokens, cookies or passwords.')
    launcher.write_text('#!/usr/bin/env bash\n' + marker + '\nexport AXH_HOME=' + shlex.quote(str(stack)) +
                        '\nexec /bin/bash ' + shlex.quote(str(stack / 'muse-hook.sh')) + '\n')
    launcher.chmod(0o700)
    now = int(time.time() * 1000)
    definition.write_text(json.dumps({'version': 1, 'id': name, 'enabled': True,
        'created_at_ms': now, 'updated_at_ms': now, 'delivery': {'surface': 'main'},
        'poll_interval_secs': 60, 'script_path': str(launcher), 'script_timeout_secs': 45,
        'prompt': prompt}, indent=2) + '\n')
    definition.chmod(0o600)
    (stack / 'platform-task.txt').write_text(
        'Platform task: axonhub-session-recovery\nSchedule: every 1 minute\n'
        'Timeout: 240 seconds; no overlapping executions\n\n' + prompt + '\n')
    print('Hook files registered; platform-task.txt prepared. Not proof of platform execution.')
    print('Verify hook enabled status and actual platform runs; run the task from a fresh exec.')


if __name__ == '__main__':
    main()

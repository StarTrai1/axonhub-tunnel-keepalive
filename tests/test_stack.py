"""Offline integration tests; fake daemons bind only ephemeral loopback ports."""
import os
import json
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE = '''#!/usr/bin/env python3
import http.server, json, os, sys
from pathlib import Path
if '--version' in sys.argv or 'version' in sys.argv:
    print('fake-test-daemon'); sys.exit(0)
stack = Path(os.environ['AXH_HOME'])
tunnel = Path(sys.argv[0]).name == 'cloudflared'
port = int(os.environ['AXH_METRICS_PORT'] if tunnel else os.environ['AXH_PORT'])
if tunnel:
    (stack / 'run/tunnel.args').write_text(json.dumps(sys.argv[1:]))
else:
    (stack / 'run/axonhub.env').write_text(json.dumps({k: os.environ.get(k) for k in
        ('HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy', 'ALL_PROXY', 'NO_PROXY')}))
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        initialized = (stack / 'data/initialized').exists()
        code = 503 if tunnel and (stack / 'data/unready').exists() else 200
        self.send_response(code); self.end_headers()
        self.wfile.write(json.dumps({'isInitialized': initialized}).encode())
    def log_message(self, *args): pass
http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class StackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='axh-test-')
        self.stack = Path(self.tmp.name) / 'stack'
        self.stack.mkdir()
        for source in (ROOT / 'scripts').iterdir():
            if source.is_file():
                shutil.copy(source, self.stack / source.name)
        for directory in ('bin', 'data', 'run'):
            (self.stack / directory).mkdir()
        for name in ('axonhub', 'cloudflared'):
            path = self.stack / 'bin' / name
            path.write_text(FAKE)
            path.chmod(0o700)
        self.env = {**os.environ, 'AXH_HOME': str(self.stack), 'AXH_PORT': str(free_port()),
                    'AXH_METRICS_PORT': str(free_port()), 'AXH_MODE': 'quick',
                    'AXH_PROXY_LOCAL_PORT': str(free_port()),
                    'AXH_WATCHDOG_BACKEND': 'loop', 'AXH_CF_TRANSPORT': 'direct',
                    'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1',
                    'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '', 'no_proxy': ''}
        self.script = self.stack / 'axh.sh'

    def call(self, command, check=True, **kwargs):
        return subprocess.run(['bash', str(self.script), command], env=self.env,
                              capture_output=True, text=True, check=check, timeout=45, **kwargs)

    def pid(self, name, child=False):
        path = self.stack / 'run' / (f'{name}.child' if child else f'{name}.lock/pid')
        try:
            return int(path.read_text())
        except (OSError, ValueError):
            return 0

    def wait_for(self, predicate, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.1)
        self.fail('condition did not become true')

    def tearDown(self):
        self.call('stop', check=False)
        for name in ('watchdog-loop', 'axonhub', 'tunnel', 'shim', 'maa', 'axproxy'):
            pid = self.pid(name)
            try:
                if pid and str(self.stack).encode() in Path(f'/proc/{pid}/cmdline').read_bytes():
                    os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        time.sleep(0.2)
        self.tmp.cleanup()

    def test_initialization_gate_and_ready_contract(self):
        self.call('start')
        self.assertEqual(self.pid('tunnel'), 0)
        (self.stack / 'data/initialized').touch()
        self.call('start')
        self.wait_for(lambda: self.pid('tunnel', child=True))
        self.assertEqual(self.call('health').stdout.strip(), 'OK')
        (self.stack / 'data/unready').touch()
        result = self.call('health', check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('tunnel', result.stdout)

    def test_recovery_idempotence_and_maintenance(self):
        (self.stack / 'data/initialized').write_text('owner-preserved')
        self.call('restore')
        self.wait_for(lambda: self.pid('watchdog-loop') and self.pid('axonhub', child=True))
        original = self.pid('axonhub', child=True)
        loop = self.pid('watchdog-loop')
        self.call('restore')
        self.assertEqual(self.pid('axonhub', child=True), original)
        self.assertEqual(self.pid('watchdog-loop'), loop)
        os.kill(original, signal.SIGKILL)
        self.wait_for(lambda: self.pid('axonhub', child=True) not in (0, original))
        supervisor = self.pid('axonhub')
        os.kill(supervisor, signal.SIGKILL)
        self.call('watchdog')
        self.wait_for(lambda: self.pid('axonhub') not in (0, supervisor))
        self.wait_for(lambda: self.call('health', check=False).returncode == 0)
        self.call('stop')
        self.call('restore')
        self.call('watchdog')
        self.assertIn('MAINTENANCE', self.call('health').stdout)
        self.assertEqual((self.stack / 'data/initialized').read_text(), 'owner-preserved')
        for name in ('axonhub', 'tunnel', 'shim'):
            self.assertEqual(self.pid(name), 0)

    def test_proxy_persistence_refresh_and_permissions(self):
        self.env['HTTPS_PROXY'] = 'http://fake-user:fake-pass@127.0.0.1:3128'
        result = self.call('set-proxy')
        path = self.stack / 'proxy.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('fake-pass', result.stdout + result.stderr)
        self.env['HTTPS_PROXY'] = 'http://new-user:new-pass@127.0.0.1:3129'
        self.call('set-proxy')
        self.assertIn('3129', path.read_text())

    def test_namespace_failure_does_not_change_system_hosts(self):
        before = Path('/etc/hosts').read_bytes()
        result = subprocess.run(['bash', str(self.stack / 'cloudflared-ns.sh'), '--check'],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(Path('/etc/hosts').read_bytes(), before)
        self.assertFalse(list((self.stack / 'run').glob('hosts.edge.*')))
        if result.returncode:
            self.assertTrue('permitted' in result.stderr or 'permission' in result.stderr.lower())

    def test_proxy_supervision_exec_identity_and_token_file(self):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', 7844))
            except OSError:
                self.skipTest('local shim port 7844 already in use')
        # Default proxy mode must work even when the namespace helper cannot run.
        (self.stack / 'cloudflared-ns.sh').write_text('#!/bin/bash\nexit 91\n')
        (self.stack / 'data/initialized').touch()
        (self.stack / 'tunnel.token').write_text('fake-secret-token')
        self.env['AXH_CF_TRANSPORT'] = 'proxy'
        self.call('set-proxy')
        self.call('start')
        self.wait_for(lambda: self.pid('axproxy', child=True))
        environment = json.loads((self.stack / 'run/axonhub.env').read_text())
        for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy', 'ALL_PROXY'):
            self.assertEqual(environment[key], 'http://127.0.0.1:' + self.env['AXH_PROXY_LOCAL_PORT'])
        self.wait_for(lambda: self.pid('shim', child=True))
        args = json.loads((self.stack / 'run/tunnel.args').read_text())
        self.assertEqual(args[args.index('--protocol') + 1], 'http2')
        self.assertEqual(args[args.index('--edge') + 1], '127.0.0.1:7844')
        self.assertIn('--token-file', args)
        self.assertNotIn('fake-secret-token', ' '.join(args))
        tunnel_pid = self.pid('tunnel', child=True)
        self.assertIn(str(self.stack / 'bin/cloudflared').encode(), Path(f'/proc/{tunnel_pid}/cmdline').read_bytes())
        shim_pid = self.pid('shim', child=True)
        os.kill(shim_pid, signal.SIGKILL)
        self.wait_for(lambda: self.pid('shim', child=True) not in (0, shim_pid))
        supervisor = self.pid('shim')
        os.kill(supervisor, signal.SIGKILL)
        self.call('watchdog')
        self.wait_for(lambda: self.pid('shim') not in (0, supervisor))
        self.wait_for(lambda: self.call('health', check=False).returncode == 0)
        self.call('stop')
        self.assertFalse(Path(f'/proc/{tunnel_pid}/cmdline').exists())
        self.assertEqual(self.pid('axproxy'), 0)

    def test_cron_absent_daemon_falls_back_without_package_install(self):
        fixture = self.stack / 'cron-fixture.sh'
        fixture.write_text('''
crontab() { if [ "$1" = -l ]; then return 0; else cat >/dev/null; fi; }
pgrep() { return 1; }
timeout() { return 1; }
''')
        self.env['BASH_ENV'] = str(fixture)
        self.env['AXH_WATCHDOG_BACKEND'] = 'auto'
        (self.stack / 'run/maintenance').touch()
        self.call('boot')
        self.wait_for(lambda: self.pid('watchdog-loop'))
        self.assertEqual((self.stack / 'run/watchdog.backend').read_text().strip(), 'loop')
        original = self.pid('watchdog-loop')
        self.call('boot')
        self.assertEqual(self.pid('watchdog-loop'), original)

    def test_loop_loss_is_reported_to_external_patrol(self):
        (self.stack / 'data/initialized').touch()
        self.call('restore')
        self.wait_for(lambda: self.pid('watchdog-loop'))
        os.kill(self.pid('watchdog-loop'), signal.SIGTERM)
        self.wait_for(lambda: self.pid('watchdog-loop') == 0)
        result = self.call('health', check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('watchdog-loop', result.stdout)
        self.call('restore')
        self.wait_for(lambda: self.pid('watchdog-loop'))
        self.assertEqual(self.call('health').stdout.strip(), 'OK')

    def test_session_recovery_after_all_processes_lost_preserves_named_tunnel(self):
        (self.stack / 'data/initialized').write_text('retained-owner')
        (self.stack / 'tunnel.token').write_text('retained-token')
        self.env['AXH_HOSTNAME'] = 'example.invalid'
        self.env['AXH_PLATFORM_REQUIRED'] = '1'
        self.call('session-restore')
        old = {}
        # Reproduce VM process loss without rebooting the test host.
        for name in ('watchdog-loop', 'axonhub', 'tunnel'):
            for child in (False, True):
                pid = self.pid(name, child)
                if pid:
                    old[(name, child)] = pid
                    os.kill(pid, signal.SIGKILL)
        time.sleep(.2)
        self.assertEqual(self.call('health', check=False).returncode, 1)
        self.call('session-restore')
        self.assertEqual(self.call('health').stdout.strip(), 'OK')
        self.assertNotEqual(self.pid('watchdog-loop'), old[('watchdog-loop', False)])
        self.assertEqual((self.stack / 'data/initialized').read_text(), 'retained-owner')
        self.assertEqual((self.stack / 'tunnel.token').read_text(), 'retained-token')
        args = json.loads((self.stack / 'run/tunnel.args').read_text())
        self.assertIn('--token-file', args)
        self.assertNotIn('--url', args)
        self.call('stop')
        self.assertIn('MAINTENANCE', self.call('session-restore').stdout)
        self.assertEqual(self.pid('axonhub'), 0)

    def test_named_token_missing_never_becomes_quick(self):
        (self.stack / 'data/initialized').touch()
        self.env['AXH_HOSTNAME'] = 'example.invalid'
        self.call('start')
        result = self.call('health', check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('token-missing', result.stdout)
        self.assertFalse((self.stack / 'run/tunnel.args').exists())

    def test_set_proxy_requires_fresh_input_and_corrupt_state_is_repairable(self):
        self.call('set-proxy')
        original = (self.stack / 'proxy.json').read_bytes()
        for name in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'AXH_EDGE_PROXY'):
            self.env.pop(name, None)
        self.assertEqual(self.call('set-proxy', check=False).returncode, 2)
        self.assertEqual((self.stack / 'proxy.json').read_bytes(), original)
        (self.stack / 'proxy.json').write_text('invalid')
        self.env['HTTPS_PROXY'] = 'http://fresh:secret@localhost:3456'
        self.call('set-proxy')
        self.assertEqual(json.loads((self.stack / 'proxy.json').read_text())['url'], self.env['HTTPS_PROXY'])

    def test_platform_and_watchdog_staleness_are_not_healthy(self):
        (self.stack / 'data/initialized').touch()
        self.call('restore')
        self.wait_for(lambda: (self.stack / 'run/watchdog.tick').exists())
        self.env['AXH_PLATFORM_REQUIRED'] = '1'
        self.assertIn('platform-stale', self.call('health', check=False).stdout)
        self.call('session-restore')
        self.assertEqual(self.call('health').stdout.strip(), 'OK')
        old = time.time() - 240
        os.utime(self.stack / 'run/watchdog.tick', (old, old))
        self.assertIn('watchdog-stale', self.call('health', check=False).stdout)

    def test_maa_supervision_heartbeat_and_stop_marker(self):
        if not shutil.which('node'):
            self.skipTest('Node required')
        self.env.update(AXH_MAA_ENABLED='1', MUSE_VM_ID='test-vm', AXH_NODE_BIN=shutil.which('node'))
        app = self.stack / 'MuseAutoApprove'
        for folder in ('work', 'data', 'log'):
            (app / folder).mkdir(parents=True)
        (app / 'work/muse-rpc.cjs').write_text("module.exports={rpcCall:async()=>({pending:[]})};")
        (app / 'muse-daemon.cjs').write_text('''
const rpc=require('./work/muse-rpc.cjs');
setInterval(()=>rpc.rpcCall({}, 'egress.approvals', {}), 100);
''')
        (self.stack / 'data/initialized').touch()
        self.call('start')
        self.wait_for(lambda: (self.stack / 'run/maa.ok').exists())
        self.assertEqual(self.call('health').stdout.strip(), 'OK')
        old = self.pid('maa', child=True)
        os.kill(old, signal.SIGKILL)
        self.wait_for(lambda: self.pid('maa', child=True) not in (0, old))
        self.wait_for(lambda: (self.stack / 'run/maa.ok').exists())
        os.utime(self.stack / 'run/maa.ok', (0, 0))
        # Stop polling but leave the process alive: heartbeat freshness matters.
        os.kill(self.pid('maa', child=True), signal.SIGSTOP)
        os.utime(self.stack / 'run/maa.ok', (0, 0))
        self.assertIn('maa', self.call('health', check=False).stdout)
        os.kill(self.pid('maa', child=True), signal.SIGCONT)
        (app / 'data/muse-daemon.stop').touch()
        os.kill(self.pid('maa', child=True), signal.SIGTERM)
        self.wait_for(lambda: self.pid('maa') == 0)
        self.call('watchdog')
        self.assertEqual(self.pid('maa'), 0)

    def test_maa_rotation_preserves_healthy_daemon_and_resets_failed_supervisor(self):
        if not shutil.which('node'):
            self.skipTest('Node required')
        self.env.update(AXH_MAA_ENABLED='1', AXH_MAA_PROXY='edge', MUSE_VM_ID='test-vm', AXH_NODE_BIN=shutil.which('node'))
        app = self.stack / 'MuseAutoApprove'
        for folder in ('work', 'data', 'log'):
            (app / folder).mkdir(parents=True)
        (app / 'work/muse-rpc.cjs').write_text('module.exports={rpcCall:async()=>({pending:[]})};')
        (app / 'muse-daemon.cjs').write_text('''
const fs=require('fs');
if (!fs.existsSync(process.env.AXH_HOME+'/data/maa-can-connect')) process.exit(1);
fs.writeFileSync(process.env.AXH_HOME+'/run/maa.proxy',process.env.MUSE_PROXY);
const rpc=require('./work/muse-rpc.cjs');
setInterval(()=>rpc.rpcCall({},'egress.approvals',{}),100);
''')
        (self.stack / 'data/initialized').touch()
        self.call('start')
        self.wait_for(lambda: 'maa exited rc=1' in (self.stack / 'logs/axh.log').read_text())
        supervisor = self.pid('maa')
        (self.stack / 'data/maa-can-connect').touch()
        self.env['HTTPS_PROXY'] = 'http://fresh:secret@127.0.0.1:4321'
        self.call('set-proxy')
        self.wait_for(lambda: self.pid('maa') not in (0, supervisor) and (self.stack / 'run/maa.ok').exists(), timeout=5)
        self.assertEqual((self.stack / 'run/maa.proxy').read_text(), 'http://127.0.0.1:' + self.env['AXH_PROXY_LOCAL_PORT'])
        supervisor, child = self.pid('maa'), self.pid('maa', child=True)
        self.env['HTTPS_PROXY'] = 'http://newer:secret@127.0.0.1:4321'
        self.call('set-proxy')
        self.assertEqual(self.pid('maa'), supervisor)
        self.assertEqual(self.pid('maa', child=True), child)
        self.call('stop')
        self.env['HTTPS_PROXY'] = 'http://last:secret@127.0.0.1:4321'
        self.call('set-proxy')
        self.assertEqual(self.pid('maa'), 0)

    def test_public_status_uses_direct_probe_despite_expired_proxy(self):
        # Observe curl arguments without contacting any public service.
        fixture = self.stack / 'curl-fixture.sh'
        fixture.write_text('''
curl() {
  case "${*: -1}" in
    https://probe.invalid/health)
      [ "$1" = --noproxy ] && [ "$2" = '*' ] && return 0
      return 22;;
    */admin/system/status) echo '{"isInitialized":true}';;
  esac
}
''')
        self.env.update(BASH_ENV=str(fixture), AXH_HOSTNAME='probe.invalid')
        (self.stack / 'tunnel.token').write_text('fake')
        output = self.call('status').stdout
        self.assertIn('public    https     : ok', output)
        self.assertIn('; direct)', output)


if __name__ == '__main__':
    unittest.main()

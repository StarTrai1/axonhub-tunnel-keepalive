import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HookTests(unittest.TestCase):
    def test_registration_preserves_other_hooks_and_wakes_for_new_session(self):
        with tempfile.TemporaryDirectory(prefix='axh-hook-') as directory:
            root = Path(directory)
            stack, hooks = root / 'stack', root / 'hooks'
            (stack / 'run').mkdir(parents=True)
            (hooks / 'definitions').mkdir(parents=True)
            (hooks / 'scripts').mkdir()
            other = hooks / 'definitions/home-init.json'
            other.write_text('{"owned":"by-other-app"}')
            shutil.copy(ROOT / 'scripts/muse-hook.sh', stack)
            (stack / 'axh.sh').write_text('#!/bin/bash\nexit 0\n')
            runtime = root / 'runtime.sh'
            runtime.write_text('silent() { echo "SILENT:$*"; }; wake() { echo "WAKE:$*"; }\n')
            env = {**os.environ, 'AXH_HOME': str(stack), 'HATCH_HOOK_RUNTIME': str(runtime)}
            for _ in range(2):
                subprocess.run(['python3', str(ROOT / 'scripts/muse-hooks.py'), str(hooks)], env=env,
                               capture_output=True, check=True)
            self.assertEqual(other.read_text(), '{"owned":"by-other-app"}')
            definition = json.loads((hooks / 'definitions/axonhub-session-recovery.json').read_text())
            self.assertIn('NEW platform exec', definition['prompt'])
            self.assertEqual(definition['poll_interval_secs'], 60)
            def run(**extra):
                return subprocess.run(['bash', definition['script_path']], env={**env, **extra},
                                      capture_output=True, text=True, check=True).stdout
            self.assertIn('SILENT:dry-run', run(HATCH_HOOK_DRY_RUN='1'))
            self.assertIn('WAKE:', run())
            (stack / 'run/session.tick').touch()
            (stack / 'run/session.boot').write_text(Path('/proc/sys/kernel/random/boot_id').read_text())
            self.assertIn('SILENT:', run())
            (stack / 'run/session.boot').write_text('old-boot-id')
            self.assertIn('WAKE:', run())
            (stack / 'run/maintenance').touch()
            self.assertIn('SILENT:maintenance', run())

    def test_registration_rejects_unverified_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['python3', str(ROOT / 'scripts/muse-hooks.py'), directory],
                                    env={**os.environ, 'AXH_HOME': directory}, capture_output=True)
            self.assertNotEqual(result.returncode, 0)


class AutoApproveTests(unittest.TestCase):
    def test_unmodified_upstream_approves_host_schema_and_falls_back(self):
        """Fixture is byte-identical to muse-guardian 681759c; only IO is stubbed."""
        if not shutil.which('node'):
            self.skipTest('Node required')
        import hashlib
        # Source: https://github.com/bytehola/muse-guardian/blob/681759cf9633ea740af5e64037b667b52bb44a1b/MuseAutoApprove/muse-daemon.cjs
        source = ROOT / 'tests/fixtures/muse-daemon.cjs'
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         'feae37f72c922b764f694225d57a0db55e385e0fa5113ac734f49eb289d2a456')
        with tempfile.TemporaryDirectory(prefix='axh-maa-') as directory:
            stack = Path(directory)
            app = stack / 'MuseAutoApprove'
            for folder in ('work', 'log', 'data'):
                (app / folder).mkdir(parents=True)
            shutil.copy(source, app / 'muse-daemon.cjs')
            (app / 'work/paths.cjs').write_text('''
const path=require('path'); const root=path.resolve(__dirname,'..');
module.exports={LOG_DIR:root+'/log',DAEMON_LOG_PATH:root+'/log/daemon-log.ndjson',
AUTO_APPROVE_LOG_PATH:root+'/log/auto-approve-log.ndjson',DAEMON_STOP_PATH:root+'/data/muse-daemon.stop',
DAEMON_PID_PATH:root+'/data/muse-daemon.pid',COOKIE_PATH:root+'/data/cookies.json'};
''')
            (app / 'work/proxy.cjs').write_text('module.exports={installProxy:()=>{},PROXY:null};')
            (app / 'work/login-lib.cjs').write_text("module.exports={resolveCredentials:()=>({email:'test@example.test',source:'file'})};")
            (app / 'work/muse-rpc.cjs').write_text('''
const fs=require('fs');
module.exports={connect:async()=>({ws:{close:()=>{}}}), rpcCall:async(conn,method,params)=>{
 if(method==='egress.approvals') return {pending:[
  {approval_id:'host-only',host:'example.test',port:443,scheme:'tcp'},
  {approval_id:'command',host:'example.test',registered_command:'curl https://example.test'},
  {approval_id:'fallback',host:'fallback.test'},
  {approval_id:'opaque'}],pending_approvals:[{approval_id:'host-only'}]};
 if(method!=='egress.approval.decide') throw Error('unexpected RPC');
 fs.appendFileSync('data/calls.ndjson',JSON.stringify(params)+'\\n');
 if(params.approval_id==='fallback' && params.decision==='allow_always') throw Error('scope rejected');
 return {approval:{status:'approved',applied_rule_entries:[]}};
}};
''')
            result = subprocess.run(['node', str(app / 'muse-daemon.cjs'), '--once'], cwd=app,
                                    capture_output=True, text=True, check=True)
            calls = [json.loads(line) for line in (app / 'data/calls.ndjson').read_text().splitlines()]
            self.assertEqual([c['approval_id'] for c in calls],
                             ['host-only', 'command', 'fallback', 'fallback', 'opaque'])
            self.assertEqual(calls[3], {'approval_id': 'fallback', 'decision': 'allow_once'})
            for c in calls[:3] + calls[4:]:
                self.assertEqual(c['decision'], 'allow_always')
                self.assertEqual(c['always_scope'], 'destination_domain')
                self.assertEqual(c['allow_always_scope'], 'destination_domain')
            events = [json.loads(line) for line in (app / 'log/daemon-log.ndjson').read_text().splitlines()]
            self.assertEqual(sum(e['event'] == 'decided' for e in events), 3)
            self.assertEqual(sum(e['event'] == 'decided_fallback' for e in events), 1)
            self.assertIn('"decided":4', result.stdout)

    def test_readonly_status_distinguishes_poll_decision_and_current_launch(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('maa_status', ROOT / 'scripts/maa-status.py')
        status = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(status)
        with tempfile.TemporaryDirectory() as directory:
            stack = Path(directory)
            (stack / 'run').mkdir()
            log = stack / 'MuseAutoApprove/log/daemon-log.ndjson'
            log.parent.mkdir(parents=True)
            now = 1_000_000
            (stack / 'run/maa.started').write_text(str(now-1000))
            records = [
                {'ts':now-2000,'event':'decided','status':'approved'},  # old run
                {'ts':now-900,'event':'daemon_start','decision':'allow_always','scope':'destination_domain'},
                {'ts':now-800,'event':'heartbeat','pending':0},
                {'ts':now-700,'event':'list_error','error':'secret'},
                {'ts':now+1000,'event':'heartbeat','pending':99}]
            log.write_text('\n'.join(json.dumps(r) for r in records)+'\n{partial')
            original = log.read_bytes()
            state = status.snapshot(stack, now)
            self.assertTrue(status.healthy(state, now))
            self.assertEqual(state['decisions'], 0)
            self.assertEqual(state['pending'], 0)
            self.assertEqual(state['poll'], now-800)  # error doesn't refresh success
            self.assertEqual(log.read_bytes(), original)
            records += [{'ts':now-600,'event':'pending_found','count':2},
                        {'ts':now-500,'event':'decided','status':'pending'},
                        {'ts':now-400,'event':'decided_fallback','status':'approved'}]
            log.write_text('\n'.join(json.dumps(r) for r in records)+'\n')
            state = status.snapshot(stack, now)
            self.assertEqual((state['decisions'],state['approved'],state['fallback']), (2,1,1))
            self.assertEqual(state['pending'], 2)
            self.assertFalse(status.healthy(state, now+121000))
            (stack / 'run/maa.started').write_text(str(now))
            self.assertFalse(status.healthy(status.snapshot(stack, now), now))
            # Truncated/rotated log, malformed records, bounded tail are tolerated.
            (stack / 'run/maa.started').write_text(str(now-1000))
            log.write_text('x'*(status.WINDOW+1)+'\n'+json.dumps(records[2])+'\n{partial')
            self.assertTrue(status.healthy(status.snapshot(stack, now), now))
            log.unlink()
            self.assertFalse(status.healthy(status.snapshot(stack, now), now))


if __name__ == '__main__':
    unittest.main()

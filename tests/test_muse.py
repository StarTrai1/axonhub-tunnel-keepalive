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
    def test_adapter_limits_scope_redacts_and_reports_successful_rpc(self):
        if not shutil.which('node'):
            self.skipTest('Node required')
        with tempfile.TemporaryDirectory(prefix='axh-maa-') as directory:
            stack = Path(directory)
            app = stack / 'MuseAutoApprove'
            for folder in ('work', 'log'):
                (app / folder).mkdir(parents=True)
            (stack / 'run').mkdir()
            (app / 'work/muse-rpc.cjs').write_text('''
module.exports = {rpcCall: async (conn, method, params) => {
 if (method === 'egress.approvals') return {pending: [
   {approval_id:'net', type:'egress', destination_domain:'example.com'},
   {approval_id:'reader', type:'reader_grant', destination_domain:'example.com'},
   {approval_id:'unknown', description:'please allow example.com'}]};
 return {ok:true};
}};
''')
            test = stack / 'test.cjs'
            test.write_text('''
const assert = require('assert'); const fs = require('fs');
const adapter = require(process.env.ADAPTER); adapter.install();
const rpc = require(process.env.AXH_HOME + '/MuseAutoApprove/work/muse-rpc.cjs');
(async () => {
 const result = await rpc.rpcCall({}, 'egress.approvals', {});
 assert.deepEqual(result.pending.map(x=>x.approval_id), ['net']);
 await assert.rejects(rpc.rpcCall({}, 'egress.approval.decide', {approval_id:'reader'}));
 assert(fs.existsSync(process.env.AXH_HOME + '/run/maa.ok'));
 console.log('http://user:secret@proxy.test password=verysecret');
 fs.appendFileSync(process.env.AXH_HOME + '/MuseAutoApprove/log/daemon-log.ndjson',
   '{"proxy":"http://user:secret@proxy.test","password":"verysecret"}');
})().catch(e=>{console.error(e); process.exit(1)});
''')
            result = subprocess.run(['node', str(test)], env={**os.environ, 'AXH_HOME': directory,
                         'MUSE_VM_ID': 'test-vm', 'ADAPTER': str(ROOT / 'scripts/maa-adapter.cjs')},
                         capture_output=True, text=True, check=True)
            output = result.stdout + result.stderr + (app / 'log/daemon-log.ndjson').read_text()
            self.assertNotIn('secret', output)


if __name__ == '__main__':
    unittest.main()

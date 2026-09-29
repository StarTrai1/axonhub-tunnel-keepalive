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
 if (process.env.EMPTY_QUEUE && method === 'egress.approvals') return {pending: []};
 if (method === 'egress.approvals') return {pending: [
   {approval_id:'net', type:'egress', destination_domain:'example.com'},
   {approval_id:'reader', type:'reader_grant', destination_domain:'example.com'},
   {approval_id:'command', registered_command:'curl https://example.com', host:'example.com', port:443, scheme:'https'},
   {approval_id:'command-with-domain', registered_command:'curl', destination_domain:'example.com'},
   {approval_id:'host-only', host:'example.com', port:443, scheme:'https'},
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
 await assert.rejects(rpc.rpcCall({}, 'egress.approval.decide', {approval_id:'command'}));
 await assert.rejects(rpc.rpcCall({}, 'egress.approval.decide', {approval_id:'command-with-domain'}));
 const file=process.env.AXH_HOME + '/run/maa.coverage.json';
 const initial=JSON.parse(fs.readFileSync(file));
 assert.equal(initial.visible_pending, 6);
 assert.equal(initial.eligible_pending, 1);
 assert.equal(initial.skipped_command, 2);
 assert.equal(initial.decision_rpc_ok, 0);
 await rpc.rpcCall({}, 'egress.approval.decide', {approval_id:'net', decision:'allow_once'});
 assert.equal(JSON.parse(fs.readFileSync(file)).decision_rpc_ok, 1);
 assert(!fs.readFileSync(file, 'utf8').includes('registered_command'));
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
            # A new healthy poller with an empty queue must not report decision success.
            empty = stack / 'empty.cjs'
            empty.write_text('''
const fs=require('fs'); const assert=require('assert');
require(process.env.ADAPTER).install();
const rpc=require(process.env.AXH_HOME+'/MuseAutoApprove/work/muse-rpc.cjs');
(async()=>{
 await rpc.rpcCall({},'egress.approvals',{});
 const state=JSON.parse(fs.readFileSync(process.env.AXH_HOME+'/run/maa.coverage.json'));
 assert.equal(state.polls,1); assert.equal(state.visible_pending,0); assert.equal(state.decision_rpc_ok,0);
 assert.equal(state.command_approvals,'unsupported');
})().catch(e=>{console.error(e);process.exit(1)});
''')
            env = {**os.environ, 'AXH_HOME': directory, 'MUSE_VM_ID': 'test-vm',
                   'ADAPTER': str(ROOT / 'scripts/maa-adapter.cjs'), 'EMPTY_QUEUE': '1'}
            subprocess.run(['node', str(empty)], env=env, check=True, capture_output=True)
            status = subprocess.run(['node', str(ROOT / 'scripts/maa-adapter.cjs'), '--status'], env=env,
                                    check=True, capture_output=True, text=True).stdout
            self.assertIn('command approvals unsupported', status)
            self.assertIn('0 successful', status)


if __name__ == '__main__':
    unittest.main()

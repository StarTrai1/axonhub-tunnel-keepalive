'use strict';
// Preload for the reviewed upstream daemon: scoped decisions, redacted logs,
// and a heartbeat only after a successful approvals RPC. No account calls here.
const fs = require('fs');
const path = require('path');
const util = require('util');

function redact(value) {
  return String(value)
    .replace(/\b(?:https?|wss?):\/\/[^\s"'<>]+/gi, '[url withheld]')
    .replace(/((?:password|auth_token|notary_token|hatch_sess|token)["']?\s*[=:]\s*["']?)[^\s,;"'}]+/gi, '$1[redacted]');
}

function networkDestination(approval) {
  if (!approval || typeof approval !== 'object') return null;
  // A network-looking host on a command approval is not a network permission.
  if (Object.prototype.hasOwnProperty.call(approval, 'registered_command')) return null;
  // Unknown schema stays pending. Never infer permission from free-form text.
  const kind = approval.approval_type || approval.kind || approval.type;
  if (kind && !['egress', 'network', 'network_egress', 'egress_network'].includes(kind)) return null;
  const destination = approval.destination_domain || approval.destination_host ||
    (approval.destination && (approval.destination.hostname || approval.destination.host));
  return typeof destination === 'string' && /^[a-z0-9.-]+$/i.test(destination) ? destination.toLowerCase() : null;
}

function install() {
  const stack = process.env.AXH_HOME;
  if (!stack || !process.env.MUSE_VM_ID) throw new Error('AXH_HOME and current MUSE_VM_ID required');
  const app = path.join(stack, 'MuseAutoApprove');
  const append = fs.appendFileSync.bind(fs);
  fs.appendFileSync = function(file, data, ...args) {
    if (typeof file === 'string' && path.dirname(file) === path.join(app, 'log')) data = redact(data);
    return append(file, data, ...args);
  };
  for (const method of ['log', 'error', 'warn']) {
    const original = console[method].bind(console);
    console[method] = (...args) => original(redact(util.format(...args)));
  }
  // Upstream dependencies are loaded after logging protection is in place.
  const rpc = require(path.join(app, 'work/muse-rpc.cjs'));
  const call = rpc.rpcCall;
  const permitted = new Set();
  const coverage = {queue: 'egress.approvals', command_approvals: 'unsupported',
    polls: 0, visible_pending: 0, eligible_pending: 0, skipped_command: 0,
    skipped_other: 0, decision_rpc_ok: 0};
  const saveCoverage = () => {
    const file = path.join(stack, 'run/maa.coverage.json');
    fs.writeFileSync(file + '.tmp', JSON.stringify({...coverage, observed_at: Date.now()}), {mode: 0o600});
    fs.renameSync(file + '.tmp', file);
  };
  rpc.rpcCall = async (connection, method, params) => {
    if (method === 'egress.approval.decide' && !permitted.has(params.approval_id)) {
      throw new Error('approval outside verified network scope');
    }
    const result = await call(connection, method, params);
    if (method === 'egress.approval.decide') { coverage.decision_rpc_ok++; saveCoverage(); }
    if (method !== 'egress.approvals') return result;
    if (!result || (!Array.isArray(result.pending) && !Array.isArray(result.pending_approvals)) ||
        ['pending', 'pending_approvals'].some(key => result[key] != null && !Array.isArray(result[key]))) {
      throw new Error('unrecognized approvals schema; verify against current Muse API');
    }
    permitted.clear();
    coverage.polls++;
    coverage.visible_pending = 0; coverage.eligible_pending = 0;
    coverage.skipped_command = 0; coverage.skipped_other = 0;
    const seen = new Set();
    for (const item of [...(result.pending || []), ...(result.pending_approvals || [])]) {
      if (item && item.approval_id && seen.has(item.approval_id)) continue;
      if (item && item.approval_id) seen.add(item.approval_id);
      coverage.visible_pending++;
      if (item && Object.prototype.hasOwnProperty.call(item, 'registered_command')) coverage.skipped_command++;
      else if (item && item.approval_id && networkDestination(item)) coverage.eligible_pending++;
      else coverage.skipped_other++;
    }
    let skipped = 0;
    const filter = items => (items || []).filter(item => {
      if (networkDestination(item) && item.approval_id) { permitted.add(item.approval_id); return true; }
      skipped++; return false;
    });
    const filtered = {...result, pending: filter(result.pending), pending_approvals: filter(result.pending_approvals)};
    if (skipped) console.warn('MAA skipped %d approvals with unknown or non-network scope', skipped);
    saveCoverage();
    fs.writeFileSync(path.join(stack, 'run/maa.ok'), String(Date.now()), {mode: 0o600});
    return filtered;
  };
}

module.exports = {redact, networkDestination, install};
if (process.env.AXH_HOME && process.argv[1] && path.basename(process.argv[1]) === 'muse-daemon.cjs') install();
if (require.main === module && process.argv[2] === '--status') {
  console.log('MAA coverage       : sentinel egress.approvals only; command approvals unsupported');
  try {
    const data = JSON.parse(fs.readFileSync(path.join(process.env.AXH_HOME, 'run/maa.coverage.json'), 'utf8'));
    const count = key => Number.isSafeInteger(data[key]) && data[key] >= 0 ? data[key] : 0;
    console.log(`MAA queue snapshot : visible=${count('visible_pending')} eligible=${count('eligible_pending')} command_skipped=${count('skipped_command')} other_skipped=${count('skipped_other')}`);
    console.log(`MAA decision RPCs  : ${count('decision_rpc_ok')} successful in this daemon run; polling alone does not verify approval coverage`);
  } catch { console.log('MAA queue snapshot : not observed'); }
}

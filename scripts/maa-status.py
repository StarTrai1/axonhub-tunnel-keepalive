#!/usr/bin/env python3
"""Read upstream MAA events without injecting code or changing approval RPCs."""
import json
import os
from pathlib import Path
import sys
import time

WINDOW = 1024 * 1024


def snapshot(stack, now=None):
    now = int(time.time() * 1000) if now is None else now
    state = dict(poll=None, pending=None, decisions=0, approved=0, fallback=0,
                 errors=0, decision=None, scope=None)
    try:
        started = int((stack / 'run/maa.started').read_text())
        if not 0 < started <= now:
            return state
        with (stack / 'MuseAutoApprove/log/daemon-log.ndjson').open('rb') as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - WINDOW)
            stream.seek(offset)
            if offset:
                stream.readline()  # Skip a potentially partial first record.
            lines = stream.read(WINDOW).splitlines()
    except (OSError, ValueError):
        return state
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        ts = event.get('ts')
        if not isinstance(ts, (int, float)) or not started <= ts <= now:
            continue
        kind = event.get('event')
        if kind == 'daemon_start':
            state['decision'] = event.get('decision')
            state['scope'] = event.get('scope')
        if kind in ('heartbeat', 'pending_found'):
            state['poll'] = ts
            count = event.get('pending' if kind == 'heartbeat' else 'count')
            state['pending'] = count if type(count) is int and count >= 0 else None
        elif kind in ('decided', 'decided_fallback'):
            state['decisions'] += 1
            state['approved'] += event.get('status') == 'approved'
            state['fallback'] += kind == 'decided_fallback'
        elif kind in ('list_error', 'decide_error', 'fallback_error', 'sweep_error'):
            state['errors'] += 1
    return state


def healthy(state, now=None):
    now = int(time.time() * 1000) if now is None else now
    return state['poll'] is not None and 0 <= now - state['poll'] <= 120000


def main():
    state = snapshot(Path(os.environ['AXH_HOME']))
    if '--health' in sys.argv:
        return 0 if healthy(state) else 1
    # Only known policy values and counts; never print raw events/IDs/URLs/secrets.
    decision = state['decision'] if state['decision'] in ('allow_once', 'allow_always') else 'not observed'
    scope = 'destination_domain' if state['scope'] == 'destination_domain' else 'not observed'
    print(f'MAA runtime policy : {decision}; scope={scope} (upstream daemon)')
    print('MAA queue          : egress.approvals; upstream handles all returned IDs without local filtering')
    print(f"MAA last pending   : {state['pending'] if state['pending'] is not None else 'not observed'}")
    print(f"MAA log window     : decisions={state['decisions']} approved={state['approved']} fallback={state['fallback']} errors={state['errors']} (current launch, last 1 MiB)")
    print('MAA verification   : polling is not approval; correlate daemon IDs with history; permanent rules require separate verification')
    return 0


if __name__ == '__main__':
    sys.exit(main())

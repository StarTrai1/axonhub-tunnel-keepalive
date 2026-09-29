#!/usr/bin/env python3
"""Atomic proxy handoff from a fresh platform exec; never mint credentials."""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
from urllib.parse import urlsplit


def validate(raw):
    try:
        url = urlsplit(raw)
        if url.scheme not in ('http', 'https') or not url.hostname or not (url.port or 1):
            raise ValueError()
        if url.path not in ('', '/') or url.query or url.fragment or any(c in raw for c in '\r\n\0'):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('invalid HTTP(S) proxy configuration (value withheld)') from None
    return raw


def state_path():
    return Path(os.environ['AXH_HOME']) / 'proxy.json'


def read_proxy():
    path = state_path() if os.environ.get('AXH_HOME') else None
    if path and path.exists():
        # Do not fall back to an old process environment on corrupt state.
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError('invalid proxy state')
        return validate(data['url'])
    return validate(next((os.environ[k] for k in
                         ('AXH_EDGE_PROXY', 'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy')
                         if os.environ.get(k)), ''))


def save_input():
    # axh.sh captures this before sourcing persistent env. No saved-value fallback.
    raw = validate(os.environ.get('AXH_PROXY_INPUT', ''))
    path = state_path()
    previous = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text())
        except (ValueError, OSError):
            pass  # A fresh validated input can repair corrupt saved state.
    if not isinstance(previous, dict):
        previous = {}
    changed = previous.get('url') != raw
    now = int(time.time())
    data = {'url': raw, 'captured_at': now,
            'changed_at': now if changed else previous.get('changed_at', now)}
    fd, temporary = tempfile.mkstemp(prefix='.proxy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return changed


def main():
    try:
        command = sys.argv[1]
        if command == 'save':
            print('proxy updated' if save_input() else 'proxy unchanged')
        elif command == 'shell':
            print('export AXH_EDGE_PROXY=' + shlex.quote(read_proxy()))
        elif command == 'status':
            path = state_path()
            if not path.exists():
                print('proxy capture: absent (legacy env or unconfigured)')
                return 0
            data = json.loads(path.read_text())
            print(f"proxy captured age: {max(0, int(time.time()) - data['captured_at'])}s; expiry unknown")
        else:
            return 2
        return 0
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        print('proxy state unavailable or invalid; capture from a fresh platform exec', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

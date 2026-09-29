"""Opt-in local smoke: python3 tests/smoke_cloudflared.py /path/to/cloudflared.

Uses fake credentials and a local TLS edge; never registers a real tunnel.
Requires openssl and a cloudflared binary supporting --no-prechecks (tested 2026.9.3).
"""
import base64
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time


def main():
    binary = Path(sys.argv[1]).resolve()
    subprocess.run([str(binary), '--version'], check=True)
    with tempfile.TemporaryDirectory(prefix='axh-cloudflared-smoke-') as directory:
        root = Path(directory)
        cert, key = root / 'cert.pem', root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=h2.cftunnel.com', '-addext', 'subjectAltName=DNS:h2.cftunnel.com'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        evidence, done = {}, threading.Event()
        context.set_servername_callback(lambda sock, name, ctx: evidence.update(sni=name))

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(15)
                try:
                    data = b''
                    while not data.endswith(b'\r\n\r\n'):
                        part = self.request.recv(1)
                        if not part:
                            return
                        data += part
                    evidence['connect'] = data.split(b'\r\n')[0].decode()
                    self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                    with context.wrap_socket(self.request, server_side=True) as tls:
                        # cloudflared is the HTTP/2 server on this outbound TLS
                        # connection, so it sends SETTINGS, not a client preface.
                        header = b''
                        while len(header) < 9:
                            part = tls.recv(9 - len(header))
                            if not part:
                                break
                            header += part
                        evidence['http2_settings'] = len(header) == 9 and header[3] == 4
                        done.set()
                except OSError as error:
                    evidence['error'] = type(error).__name__

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

        with Server(('127.0.0.1', 0), Handler) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            env = {**os.environ, 'AXH_EDGE_PROXY': f'http://127.0.0.1:{server.server_address[1]}',
                   'AXH_EDGE_TARGETS': 'region1.v2.argotunnel.com,region2.v2.argotunnel.com'}
            token = root / 'fake.token'
            token.write_text(base64.b64encode(json.dumps({
                'a': 'a' * 32, 't': '00000000-0000-4000-8000-000000000001',
                's': base64.b64encode(b's' * 32).decode()}).encode()).decode())
            token.chmod(0o600)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                metrics = sock.getsockname()[1]
            processes = []
            try:
                with (root / 'shim.log').open('w') as slog, (root / 'cloudflared.log').open('w') as clog:
                    shim = Path(__file__).resolve().parents[1] / 'scripts/cloudflared-edge-shim.py'
                    processes.append(subprocess.Popen([sys.executable, str(shim)], env=env,
                                                      stdout=slog, stderr=subprocess.STDOUT, start_new_session=True))
                    time.sleep(0.3)
                    if processes[0].poll() is not None:
                        raise RuntimeError('shim failed to bind 127.0.0.1:7844')
                    processes.append(subprocess.Popen([
                        str(binary), 'tunnel', '--no-autoupdate', '--no-prechecks', '--metrics', f'127.0.0.1:{metrics}',
                        '--edge', '127.0.0.1:7844', '--protocol', 'http2', '--cacert', str(cert),
                        'run', '--token-file', str(token)], stdout=clog, stderr=subprocess.STDOUT, start_new_session=True))
                    success = done.wait(25)
                    print(json.dumps(evidence))
                    if not success or not evidence.get('http2_settings') or evidence.get('sni') != 'h2.cftunnel.com':
                        raise AssertionError((root / 'cloudflared.log').read_text()[-4000:])
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                server.shutdown()


if __name__ == '__main__':
    main()

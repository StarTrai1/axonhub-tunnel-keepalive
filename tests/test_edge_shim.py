import asyncio
import base64
import importlib.util
import contextlib
import io
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import sys
import json
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
SPEC = importlib.util.spec_from_file_location('shim', Path(__file__).resolve().parents[1] / 'scripts/cloudflared-edge-shim.py')
shim = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shim)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.headers = []
        self.failures = []
        self.env = patch.dict(os.environ, {'AXH_EDGE_PROXY': '', 'AXH_EDGE_TARGETS': 'region1.v2.argotunnel.com,region2.v2.argotunnel.com'})
        self.env.start()

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        self.env.stop()

    async def proxy(self, handler):
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        self.servers.append(server)
        port = server.sockets[0].getsockname()[1]
        os.environ['AXH_EDGE_PROXY'] = f'http://user:p%40ss@127.0.0.1:{port}'

    async def relay(self):
        server = await asyncio.start_server(shim.Relay(shim.targets()).handle, '127.0.0.1', 0)
        self.servers.append(server)
        return await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])

    async def echo_proxy(self, reader, writer):
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            self.headers.append(header)
            if self.failures:
                writer.write(self.failures.pop(0))
                await writer.drain()
                return
            # Fragmented headers plus coalesced payload must not lose bytes.
            writer.write(b'HTTP/1.1 200 Connection')
            await writer.drain()
            await asyncio.sleep(0.01)
            writer.write(b' established\r\nX-Test: yes\r\n\r\nGREETING')
            await writer.drain()
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
            writer.write(b'FINAL')
            await writer.drain()
        finally:
            writer.close()

    async def test_bytes_auth_and_half_close(self):
        await self.proxy(self.echo_proxy)
        reader, writer = await self.relay()
        payload = bytes(range(256)) * 1024
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        result = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        self.assertEqual(result, b'GREETING' + payload + b'FINAL')
        self.assertIn(b'CONNECT region1.v2.argotunnel.com:7844 HTTP/1.1', self.headers[0])
        self.assertIn(b'Proxy-Authorization: Basic ' + base64.b64encode(b'user:p@ss'), self.headers[0])

    async def test_failover_and_round_robin(self):
        await self.proxy(self.echo_proxy)
        self.failures = [b'HTTP/1.1 403 Forbidden\r\n\r\n']
        relay = shim.Relay(shim.targets())
        server = await asyncio.start_server(relay.handle, '127.0.0.1', 0)
        self.servers.append(server)
        for _ in range(2):
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            self.assertEqual(await asyncio.wait_for(reader.readexactly(8), 3), b'GREETING')
            writer.write_eof()
            await reader.read()
            writer.close()
        self.assertIn(b'region1.v2', self.headers[0])
        self.assertIn(b'region2.v2', self.headers[1])
        self.assertIn(b'region2.v2', self.headers[2])

    async def test_rejected_connect_sanitized(self):
        await self.proxy(self.echo_proxy)
        self.failures = [b'HTTP/1.1 407 SecretCredentials\r\nProxy: user:p@ss\r\n\r\n']
        with self.assertRaisesRegex(shim.TransportError, '^CONNECT rejected: 407$'):
            await shim.connect_edge('region1.v2.argotunnel.com')

    async def test_timeout_is_bounded(self):
        async def hang(reader, writer):
            await reader.read()
            writer.close()
        await self.proxy(hang)
        with patch.object(shim, 'TIMEOUT', 0.05):
            with self.assertRaises(TimeoutError):
                await shim.connect_edge('region1.v2.argotunnel.com')

    async def test_running_relay_reloads_rotated_credentials_without_dropping_connection(self):
        await self.proxy(self.echo_proxy)
        with tempfile.TemporaryDirectory(prefix='axh-proxy-') as directory:
            with patch.dict(os.environ, {'AXH_HOME': directory}):
                from axh_proxy import save_input
                os.environ['AXH_PROXY_INPUT'] = os.environ['AXH_EDGE_PROXY']
                save_input()
                reader, writer = await self.relay()
                self.assertEqual(await reader.readexactly(8), b'GREETING')
                os.environ['AXH_PROXY_INPUT'] = os.environ['AXH_EDGE_PROXY'].replace('user:p%40ss', 'new:new-secret')
                save_input()
                # Old connection remains usable; a new connection sees the new auth.
                writer.write(b'still-connected'); await writer.drain()
                self.assertEqual(await reader.readexactly(15), b'still-connected')
                second_reader, second_writer = await self.relay()
                self.assertEqual(await second_reader.readexactly(8), b'GREETING')
                self.assertIn(base64.b64encode(b'new:new-secret'), self.headers[-1])
                for stream, output in ((reader, writer), (second_reader, second_writer)):
                    output.write_eof(); await stream.read(); output.close()

    def test_corrupt_proxy_file_does_not_fall_back_to_stale_env(self):
        with tempfile.TemporaryDirectory(prefix='axh-proxy-') as directory:
            Path(directory, 'proxy.json').write_text('{invalid')
            with patch.dict(os.environ, {'AXH_HOME': directory, 'AXH_EDGE_PROXY': 'http://old:password@localhost:3128'}):
                with self.assertRaises(shim.TransportError):
                    shim.proxy_config()

    def test_invalid_proxy_does_not_expose_credentials(self):
        os.environ['AXH_EDGE_PROXY'] = 'socks5://secret:password@example.com:1080'
        with self.assertRaises(shim.TransportError) as caught:
            shim.proxy_config()
        self.assertNotIn('secret', str(caught.exception))

    def test_https_proxy_configuration(self):
        os.environ['AXH_EDGE_PROXY'] = 'https://example.com'
        url, port, _ = shim.proxy_config()
        self.assertEqual((url.scheme, port), ('https', 443))

    async def tls_probe(self, https=False, trusted=True):
        if not shutil.which('openssl'):
            self.skipTest('openssl needed for local TLS fixture')
        with tempfile.TemporaryDirectory(prefix='axh-tls-') as directory:
            key, cert = Path(directory) / 'key.pem', Path(directory) / 'cert.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(key), '-out', str(cert), '-days', '1',
                            '-subj', '/CN=h2.cftunnel.com', '-addext',
                            'subjectAltName=DNS:h2.cftunnel.com,IP:127.0.0.1'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert, key)
            client_context = ssl.create_default_context(cafile=str(cert) if trusted else None)

            async def tls_proxy(reader, writer):
                try:
                    await reader.readuntil(b'\r\n\r\n')
                    writer.write(b'HTTP/1.1 200 OK\r\n\r\n')
                    await writer.drain()
                    await writer.start_tls(server_context)
                    await reader.read()
                except (OSError, ConnectionError):
                    pass
                finally:
                    writer.close()

            server = await asyncio.start_server(tls_proxy, '127.0.0.1', 0,
                                                ssl=server_context if https else None)
            self.servers.append(server)
            port = server.sockets[0].getsockname()[1]
            os.environ['AXH_EDGE_PROXY'] = f'{"https" if https else "http"}://127.0.0.1:{port}'
            os.environ['AXH_EDGE_TARGETS'] = 'region1.v2.argotunnel.com'
            with patch.object(shim.ssl, 'create_default_context', return_value=client_context):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    result = await shim.probe()
            return result, output.getvalue()

    async def test_probe_verifies_edge_tls_without_requiring_alpn(self):
        result, output = await self.tls_probe()
        self.assertEqual(result, 0, output)
        self.assertIn('verified TLS OK', output)

    async def test_probe_through_https_proxy_nested_tls(self):
        result, output = await self.tls_probe(https=True)
        self.assertEqual(result, 0, output)

    async def test_probe_rejects_untrusted_certificate(self):
        result, output = await self.tls_probe(trusted=False)
        self.assertEqual(result, 1)
        self.assertIn('SSLCertVerificationError', output)


if __name__ == '__main__':
    unittest.main()

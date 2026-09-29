import asyncio
import base64
import contextlib
import importlib.util
import io
import json
import os
import shutil
import ssl
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
SPEC = importlib.util.spec_from_file_location('forward', Path(__file__).resolve().parents[1] / 'scripts/axh-forward-proxy.py')
forward = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(forward)


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='axh-forward-')
        self.env = patch.dict(os.environ, {'AXH_HOME': self.tmp.name})
        self.env.start()
        self.connections, self.requests, self.bodies = [], [], []
        self.rotate_on_reject = False
        self.reject_always = False
        self.http_origin = False
        self.upstream = await asyncio.start_server(self.upstream_handler, '127.0.0.1', 0)
        self.port = self.upstream.sockets[0].getsockname()[1]
        self.proxy = await asyncio.start_server(forward.handle, '127.0.0.1', 0)
        self.local_port = self.proxy.sockets[0].getsockname()[1]
        self.save('old')

    async def asyncTearDown(self):
        self.proxy.close(); self.upstream.close()
        await self.proxy.wait_closed(); await self.upstream.wait_closed()
        self.env.stop(); self.tmp.cleanup()

    def save(self, password):
        path = Path(self.tmp.name, 'proxy.json')
        path.with_suffix('.tmp').write_text(json.dumps({'url': f'http://user:{password}@127.0.0.1:{self.port}'}))
        path.with_suffix('.tmp').replace(path)

    async def upstream_handler(self, reader, writer):
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            self.connections.append(header)
            if self.reject_always or (self.rotate_on_reject and len(self.connections) == 1):
                if self.rotate_on_reject:
                    self.save('fresh')
                writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
                self.bodies.append(await reader.read())
                return
            writer.write(b'HTTP/1.1 200 OK\r\n\r\n')
            await writer.drain()
            if self.http_origin:
                request = await reader.readuntil(b'\r\n\r\n')
                self.requests.append(request)
                body = await reader.readexactly(4)
                self.bodies.append(body)
                writer.write(b'HTTP/1.1 401 Unauthorized\r\nContent-Length: 2\r\n\r\n{}')
                await writer.drain()
            else:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def client(self):
        return await asyncio.open_connection('127.0.0.1', self.local_port)

    async def tunnel(self):
        reader, writer = await self.client()
        writer.write(b'CONNECT api.example.test:443 HTTP/1.1\r\nHost: api.example.test:443\r\n\r\n')
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
        self.assertIn(b'200', response)
        return reader, writer

    async def test_rotation_keeps_stream_and_next_connection_uses_new_auth(self):
        reader, writer = await self.tunnel()
        payload = bytes(range(256)) * 1024
        writer.write(payload); await writer.drain()
        self.assertEqual(await reader.readexactly(len(payload)), payload)
        self.save('fresh')
        r2, w2 = await self.tunnel()
        self.assertIn(base64.b64encode(b'user:old'), self.connections[0])
        self.assertIn(base64.b64encode(b'user:fresh'), self.connections[1])
        writer.write(b'data: after-rotation\n\n'); await writer.drain()
        self.assertEqual(await reader.readexactly(22), b'data: after-rotation\n\n')
        for stream, output in ((reader, writer), (r2, w2)):
            output.write_eof(); await stream.read(); output.close()

    async def test_407_retries_only_connect_before_post_body(self):
        self.rotate_on_reject = True
        self.http_origin = True
        reader, writer = await self.client()
        writer.write(b'POST http://api.example.test/v1/responses?x=1 HTTP/1.1\r\n'
                     b'Host: api.example.test\r\nContent-Length: 4\r\n'
                     b'Authorization: Bearer business-key\r\nProxy-Authorization: Basic client-secret\r\n\r\nDATA')
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        self.assertIn(b'401 Unauthorized', response)
        self.assertEqual(len(self.connections), 2)
        self.assertEqual(self.bodies, [b'', b'DATA'])
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0].startswith(b'POST /v1/responses?x=1 HTTP/1.1'))
        self.assertIn(b'Authorization: Bearer business-key', self.requests[0])
        self.assertNotIn(b'Proxy-Authorization', self.requests[0])
        self.assertNotIn(b'business-key', b''.join(self.connections))

    async def test_same_expired_credentials_do_not_retry_or_leak(self):
        self.reject_always = True
        reader, writer = await self.client()
        with contextlib.redirect_stdout(io.StringIO()) as logs:
            writer.write(b'CONNECT api.example.test:443 HTTP/1.1\r\n\r\n')
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        self.assertIn(b'502', response)
        self.assertEqual(len(self.connections), 1)
        self.assertIn('407', logs.getvalue())
        self.assertNotIn('user:old', logs.getvalue())
        self.assertNotIn('api.example.test', logs.getvalue())

    async def test_closed_business_connection_is_not_replayed(self):
        reader, writer = await self.tunnel()
        writer.write(b'POST-like-non-idempotent-data'); await writer.drain()
        await reader.readexactly(29)
        writer.write_eof(); await reader.read(); writer.close()
        self.assertEqual(len(self.connections), 1)

    async def test_reject_ambiguous_http_request_before_connect(self):
        reader, writer = await self.client()
        writer.write(b'POST http://api.example.test/ HTTP/1.1\r\nContent-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\nDATA')
        await writer.drain()
        response = await reader.read(); writer.close()
        self.assertIn(b'400', response)
        self.assertFalse(self.connections)

    async def test_https_upstream_is_verified_and_destination_tls_is_untouched(self):
        if not shutil.which('openssl'):
            self.skipTest('openssl required for local TLS fixture')
        cert, key = Path(self.tmp.name, 'cert.pem'), Path(self.tmp.name, 'key.pem')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=api.example.test',
                        '-addext', 'subjectAltName=DNS:api.example.test,IP:127.0.0.1'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        client_context = ssl.create_default_context(cafile=str(cert))
        untrusted_context = ssl.create_default_context()
        async def secure_origin(reader, writer):
            try:
                self.connections.append(await reader.readuntil(b'\r\n\r\n'))
                writer.write(b'HTTP/1.1 200 OK\r\n\r\n'); await writer.drain()
                await writer.start_tls(server_context)
                self.requests.append(await reader.readuntil(b'\r\n\r\n'))
                self.bodies.append(await reader.readexactly(4))
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\ndata: done\n\n')
                await writer.drain()
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
        server = await asyncio.start_server(secure_origin, '127.0.0.1', 0, ssl=server_context)
        try:
            Path(self.tmp.name, 'proxy.json').write_text(json.dumps({'url': f'https://user:pw@127.0.0.1:{server.sockets[0].getsockname()[1]}'}))
            with patch.object(forward.ssl, 'create_default_context', return_value=untrusted_context):
                with contextlib.redirect_stdout(io.StringIO()):
                    reader, writer = await self.client()
                    writer.write(b'CONNECT api.example.test:443 HTTP/1.1\r\n\r\n'); await writer.drain()
                    self.assertIn(b'502', await reader.read()); writer.close()
            self.assertFalse(self.connections)
            with patch.object(forward.ssl, 'create_default_context', return_value=client_context):
                reader, writer = await self.tunnel()
                await writer.start_tls(client_context, server_hostname='api.example.test')
                writer.write(b'POST /v1/responses HTTP/1.1\r\nHost: api.example.test\r\nContent-Length: 4\r\n\r\nDATA')
                await writer.drain()
                self.assertIn(b'200 OK', await reader.readuntil(b'\r\n\r\n'))
                self.assertEqual(await reader.readexactly(12), b'data: done\n\n')
                writer.close()
                await writer.wait_closed()
            self.assertEqual(self.bodies, [b'DATA'])
        finally:
            server.close(); await server.wait_closed()


if __name__ == '__main__':
    unittest.main()

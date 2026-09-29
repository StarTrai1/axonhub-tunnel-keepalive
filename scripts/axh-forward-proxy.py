#!/usr/bin/env python3
"""Loopback HTTP proxy for long-lived clients; Python 3.11+, no TLS interception."""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import os
import re
import ssl
import sys
from urllib.parse import unquote, urlsplit

from axh_proxy import read_proxy

HEADER_LIMIT = 65536
CONNECT_TIMEOUT = 10


class ProxyError(Exception):
    pass


def authority(host, port):
    return f'[{host}]:{port}' if ':' in host else f'{host}:{port}'


async def upstream_connect(host, port):
    # Retry only a rejected CONNECT, only after a credential change. No client
    # request/header/body has reached the origin yet, including non-idempotent POST.
    previous = None
    for attempt in range(2):
        raw = read_proxy()
        if attempt and raw == previous:
            raise ProxyError('upstream CONNECT 407; fresh credentials required')
        previous = raw
        proxy = urlsplit(raw)
        auth = ''
        if proxy.username is not None:
            secret = f'{unquote(proxy.username)}:{unquote(proxy.password or "")}'
            auth = 'Proxy-Authorization: Basic ' + base64.b64encode(secret.encode()).decode() + '\r\n'
        writer = None
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                reader, writer = await asyncio.open_connection(
                    proxy.hostname, proxy.port or (443 if proxy.scheme == 'https' else 80),
                    ssl=ssl.create_default_context() if proxy.scheme == 'https' else None,
                    limit=HEADER_LIMIT)
                target = authority(host, port)
                writer.write((f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n' + auth + '\r\n').encode('ascii'))
                await writer.drain()
                header = await reader.readuntil(b'\r\n\r\n')
                fields = header.split(b'\r\n', 1)[0].split()
                if len(fields) < 2 or fields[0] not in (b'HTTP/1.0', b'HTTP/1.1') or not fields[1].isdigit():
                    raise ProxyError('invalid upstream CONNECT response')
                code = int(fields[1])
                if 200 <= code < 300:
                    return reader, writer
                writer.close()
                if code == 407 and attempt == 0:
                    continue
                raise ProxyError(f'upstream CONNECT {code}')
        except BaseException:
            if writer:
                writer.close()
            raise


def parse_request(header):
    lines = header[:-4].decode('iso-8859-1').split('\r\n')
    method, target, version = lines[0].split(' ')
    if not re.fullmatch(r'[A-Z]+', method) or version not in ('HTTP/1.0', 'HTTP/1.1'):
        raise ValueError('request line')
    url = urlsplit('//' + target if method == 'CONNECT' else target)
    if (not url.hostname or url.username is not None or url.fragment or
            any(ord(c) < 33 or ord(c) > 126 for c in target)):
        raise ValueError('target')
    if method == 'CONNECT':
        if url.path or url.query or url.port is None:
            raise ValueError('CONNECT authority')
        port = url.port
    else:
        if url.scheme != 'http':
            raise ValueError('use CONNECT for HTTPS')
        port = url.port or 80
    if not 1 <= port <= 65535:
        raise ValueError('port')
    headers = []
    for line in lines[1:]:
        name, value = line.split(':', 1)
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or any(ord(c) < 32 and c != '\t' for c in value):
            raise ValueError('header')
        headers.append((name, value.strip()))
    if method == 'CONNECT':
        return url.hostname, port, None
    # Absolute-form HTTP is carried over a CONNECT to target:80, then converted
    # to origin-form. This keeps all auth retries before any business bytes.
    hop = {'proxy-authorization', 'proxy-connection', 'connection', 'host', 'keep-alive'}
    for name, value in headers:
        if name.lower() == 'connection':
            hop.update(part.strip().lower() for part in value.split(','))
    lengths = [value for name, value in headers if name.lower() == 'content-length']
    encodings = [value.lower() for name, value in headers if name.lower() == 'transfer-encoding']
    if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or encodings)) or (encodings and encodings != ['chunked']):
        raise ValueError('ambiguous request framing')
    if hop.intersection({'content-length', 'transfer-encoding', 'authorization'}):
        raise ValueError('invalid connection header')
    path = (url.path or '/') + ('?' + url.query if url.query else '')
    output = [f'{method} {path} {version}', f'Host: {authority(url.hostname, port)}', 'Connection: close']
    output.extend(f'{name}: {value}' for name, value in headers if name.lower() not in hop)
    return url.hostname, port, ('\r\n'.join(output) + '\r\n\r\n').encode('iso-8859-1')


async def pump(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


async def relay(reader, writer, remote, upstream):
    tasks = [asyncio.create_task(pump(reader, upstream)), asyncio.create_task(pump(remote, writer))]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), 30)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def handle(reader, writer):
    upstream = None
    started = False
    code = 400
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            header = await reader.readuntil(b'\r\n\r\n')
        host, port, request = parse_request(header)
        code = 502
        remote, upstream = await upstream_connect(host, port)
        started = True
        if request is None:
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await writer.drain()
        else:
            upstream.write(request)
            await upstream.drain()
        await relay(reader, writer, remote, upstream)
    except (OSError, ValueError, KeyError, ProxyError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
        if not started:
            writer.write(f'HTTP/1.1 {code} Proxy Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'.encode())
            try:
                await writer.drain()
            except OSError:
                pass
        # No destination URLs, proxy URLs, auth headers or upstream bodies in logs.
        if not isinstance(error, asyncio.IncompleteReadError):
            label = str(error) if isinstance(error, ProxyError) else type(error).__name__
            print(f'{datetime.now(timezone.utc).isoformat()} axproxy {label}', flush=True)
    finally:
        writer.close()
        if upstream:
            upstream.close()


async def main(check=False):
    port = int(os.environ.get('AXH_PROXY_LOCAL_PORT', '18080'))
    if check:
        _, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 2)
        writer.close()
        await writer.wait_closed()
        return
    server = await asyncio.start_server(handle, '127.0.0.1', port, limit=HEADER_LIMIT)
    print(f'axproxy listening on 127.0.0.1:{port}', flush=True)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='local TCP liveness only; not a business probe')
    try:
        asyncio.run(main(parser.parse_args().check))
    except (OSError, ValueError):
        sys.exit(1)
    except KeyboardInterrupt:
        pass

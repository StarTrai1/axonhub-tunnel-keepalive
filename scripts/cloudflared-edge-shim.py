#!/usr/bin/env python3
"""Cloudflare TCP edge transport over an HTTP(S) CONNECT proxy (Python 3.11+)."""
import argparse
import asyncio
import base64
import itertools
import os
import ssl
import sys
from urllib.parse import unquote, urlsplit

REGIONS = ("region1.v2.argotunnel.com", "region2.v2.argotunnel.com")
TIMEOUT = 10


class TransportError(Exception):
    pass


def proxy_config():
    raw = next((os.environ[k] for k in
                ("AXH_EDGE_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
                if os.environ.get(k)), "")
    try:
        url = urlsplit(raw)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError()
        port = url.port or (443 if url.scheme == "https" else 80)
        if url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError()
    except ValueError:
        raise TransportError("set an http:// or https:// proxy URL (value withheld)") from None
    auth = ""
    if url.username is not None:
        credentials = f"{unquote(url.username)}:{unquote(url.password or '')}"
        auth = "Proxy-Authorization: Basic " + base64.b64encode(credentials.encode()).decode() + "\r\n"
    return url, port, auth


def targets():
    # Domains are resolved by the proxy, outside the private /etc/hosts namespace.
    values = os.environ.get("AXH_EDGE_TARGETS", ",".join(REGIONS)).split(",")
    values = [v.strip() for v in values if v.strip()]
    if not values or any(any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:[]" for c in v) for v in values):
        raise TransportError("AXH_EDGE_TARGETS must contain comma-separated hostnames or IPs, without ports")
    return values


async def connect_edge(target):
    url, port, auth = proxy_config()
    writer = None
    try:
        async with asyncio.timeout(TIMEOUT):
            reader, writer = await asyncio.open_connection(
                url.hostname, port, ssl=ssl.create_default_context() if url.scheme == "https" else None)
            host = f"[{target.strip('[]')}]" if ":" in target else target
            authority = f"{host}:7844"
            writer.write((f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n" + auth + "\r\n").encode())
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            parts = header.split(b"\r\n", 1)[0].split()
            if len(parts) < 2 or parts[0] not in (b"HTTP/1.0", b"HTTP/1.1") or parts[1] != b"200":
                code = parts[1].decode() if len(parts) > 1 and parts[1].isdigit() else "invalid-response"
                raise TransportError(f"CONNECT rejected: {code}")
            return reader, writer
    except BaseException:
        if writer:
            writer.close()
        raise


def error_label(error):
    # Proxy URLs, auth headers and proxy response bodies never enter logs.
    return str(error) if isinstance(error, TransportError) else type(error).__name__


async def probe():
    ok = False
    for target in targets():
        writer = None
        stage = "CONNECT"
        try:
            _, writer = await connect_edge(target)
            stage = "edge TLS"
            context = ssl.create_default_context()
            # Match cloudflared HTTP2 TLSSettings: h2.cftunnel.com SNI, no ALPN.
            # The edge drives HTTP/2 after TLS; requiring ALPN would reject a
            # valid edge route even though cloudflared can use it.
            async with asyncio.timeout(TIMEOUT):
                await writer.start_tls(context, server_hostname="h2.cftunnel.com")
            print(f"{target}:7844 CONNECT + verified TLS OK (SNI h2.cftunnel.com)", flush=True)
            ok = True
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TransportError) as error:
            print(f"{target}:7844 {stage} FAIL ({error_label(error)})", flush=True)
        finally:
            if writer:
                writer.close()
    return 0 if ok else 1


async def copy_stream(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


class Relay:
    def __init__(self, edges):
        self.edges = edges
        self.turn = itertools.count()

    async def handle(self, reader, writer):
        upstream = None
        tasks = []
        try:
            start = next(self.turn) % len(self.edges)
            for offset in range(len(self.edges)):
                target = self.edges[(start + offset) % len(self.edges)]
                try:
                    remote, upstream = await connect_edge(target)
                    break
                except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TransportError) as error:
                    print(f"{target}:7844 {error_label(error)}", flush=True)
            if upstream is None:
                return
            tasks = [asyncio.create_task(copy_stream(reader, upstream)),
                     asyncio.create_task(copy_stream(remote, writer))]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            # Allow a half-closed peer to receive its final response, bounded in time.
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), timeout=30)
        except (OSError, ValueError):
            pass  # Normal connection teardown; /ready monitors the real tunnel.
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            if upstream:
                upstream.close()


async def serve():
    proxy_config()
    relay = Relay(targets())
    server = await asyncio.start_server(relay.handle, "127.0.0.1", 7844)
    print("edge shim listening on 127.0.0.1:7844", flush=True)
    async with server:
        await server.serve_forever()


def main():
    if sys.version_info < (3, 11):
        print("edge shim requires Python 3.11+", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="verify CONNECT and edge TLS; does not register a tunnel")
    parser.add_argument("--validate", action="store_true", help="validate configuration without networking")
    args = parser.parse_args()
    try:
        proxy_config()
        targets()
        if args.validate:
            return 0
        return asyncio.run(probe() if args.probe else serve()) or 0
    except (OSError, ValueError, TransportError) as error:
        print(f"edge shim: {error_label(error)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

"""Local HTTP/HTTPS forward proxy for accelerating terminal tools (pip, git, curl).

Routes requests through the multi-adapter bonding engine or tunnels HTTPS.
Enforces loopback restriction, timing-safe authentication, and CONNECT port filtering.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import socket
from typing import Optional, Set, Tuple
from rich.console import Console
from ..core.adapter import NetworkAdapter, bind_socket_to_interface, get_bonded_pool

ALLOWED_CONNECT_PORTS: Set[int] = {80, 443}

# Hop-by-hop / proxy-only headers that must never be forwarded to the origin.
_STRIPPED_REQUEST_HEADERS = (b"proxy-authorization", b"proxy-connection")


class DestinationBlocked(Exception):
    """Raised when a proxied destination resolves to a non-public address."""


def is_blocked_address(ip: "ipaddress._BaseAddress") -> bool:
    """Return True for any address that is not a publicly routable unicast address.

    Covers loopback, RFC1918/ULA private ranges, link-local (incl. the cloud
    metadata address 169.254.169.254), CGNAT, multicast, reserved and unspecified.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (not ip.is_global) or ip.is_multicast


async def open_bound_connection(
    target_ip: str, port: int, adapter: Optional[NetworkAdapter]
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a TCP connection pinned to ``adapter`` (device binding, else source IP)."""
    if adapter is None:
        return await asyncio.open_connection(target_ip, port)

    family = socket.AF_INET6 if ":" in target_ip else socket.AF_INET
    adapter_is_v6 = ":" in adapter.ip_address
    if (family == socket.AF_INET6) != adapter_is_v6:
        # Address-family mismatch: the adapter's IPv4 address cannot reach an IPv6 target.
        return await asyncio.open_connection(target_ip, port)

    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        bind_socket_to_interface(sock, adapter.name, adapter.ip_address)
        await asyncio.get_running_loop().sock_connect(sock, (target_ip, port))
    except BaseException:
        sock.close()
        raise
    return await asyncio.open_connection(sock=sock)


async def resolve_destination(
    host: str,
    port: int,
    allow_private: bool = False,
    prefer_family: Optional[int] = None,
) -> str:
    """Resolve ``host`` and return one validated IP literal to connect to.

    Every resolved address is checked, and callers must connect to the returned
    literal (not the hostname) so a second DNS lookup cannot rebind to an
    internal address after validation.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    candidates = []
    for family, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0].split("%")[0])
        if not allow_private and is_blocked_address(ip):
            raise DestinationBlocked(f"{host} resolves to non-public address {ip}")
        candidates.append((family, str(ip)))
    if not candidates:
        raise DestinationBlocked(f"{host} did not resolve")
    if prefer_family is not None:
        for family, ip_str in candidates:
            if family == prefer_family:
                return ip_str
    return candidates[0][1]


def parse_connect_target(target: str) -> Tuple[str, int]:
    """Parse a CONNECT authority (``host:port``, ``[v6]:port`` or bare host)."""
    if target.startswith("["):
        end = target.find("]")
        host = target[1:end] if end != -1 else target[1:]
        rest = target[end + 1:] if end != -1 else ""
        port_str = rest[1:] if rest.startswith(":") else "443"
    elif target.count(":") == 1:
        host, port_str = target.split(":")
    else:
        host, port_str = target, "443"
    try:
        port = int(port_str)
    except ValueError:
        port = 443
    return host, port


def is_loopback_host(host: str) -> bool:
    """Check if the given host resolves strictly to a loopback interface."""
    if host.lower() in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return ip.is_loopback
    except ValueError:
        return False


def validate_proxy_auth(auth_header: Optional[str], expected_credentials: str) -> bool:
    """
    Validate Proxy-Authorization Basic credentials using constant-time comparison.
    expected_credentials must be formatted as 'username:password'.
    """
    if not auth_header or not expected_credentials:
        return False

    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return False

    try:
        decoded = base64.b64decode(parts[1]).decode("utf-8")
        return hmac.compare_digest(decoded, expected_credentials)
    except Exception:
        return False


class LocalBondedProxy:
    """Asynchronous local HTTP/HTTPS accelerator proxy with security guardrails."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8899,
        auth: Optional[str] = None,
        allow_private: bool = False,
        console: Optional[Console] = None,
    ):
        if not is_loopback_host(host) and not auth:
            raise ValueError(
                f"Refusing to bind to non-loopback host '{host}' without authentication.\n"
                "Provide --auth user:pass to expose the proxy beyond this machine."
            )

        self.host = host
        self.port = port
        self.auth = auth
        self.allow_private = allow_private
        self.console = console or Console()
        self.server: Optional[asyncio.Server] = None
        self._adapters = []
        self._rr_counter = 0

    async def start(self) -> None:
        """Starts the local proxy server."""
        self._adapters = get_bonded_pool()
        self.server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self.console.print(
            f"[bold green]Plexo Proxy running on {self.host}:{self.port}[/bold green]"
        )
        if self.auth:
            self.console.print("[yellow]Proxy Authentication: ENABLED (Basic)[/yellow]")
        self.console.print(
            f"[cyan]Bonding across {len(self._adapters)} network interfaces:[/cyan]"
        )
        for a in self._adapters:
            self.console.print(f"  * {a.name} ({a.ip_address})")

        auth_str = f"{self.auth}@" if self.auth else ""
        self.console.print(
            "\n[dim]To use with pip:[/dim]\n"
            f"  pip install <package> --proxy http://{auth_str}{self.host}:{self.port}\n\n"
            "[dim]To use globally in terminal:[/dim]\n"
            f"  $env:HTTPS_PROXY = \"http://{auth_str}{self.host}:{self.port}\"\n"
        )

        async with self.server:
            await self.server.serve_forever()

    async def _handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        """Handles client connection with auth verification and port filtering."""
        try:
            request_line = await client_reader.readline()
            if not request_line:
                client_writer.close()
                return

            words = request_line.decode("latin1").split()
            if len(words) < 2:
                client_writer.close()
                return

            method, url = words[0], words[1]

            # Read headers
            raw_headers = bytearray()
            headers_dict = {}
            while True:
                line = await client_reader.readline()
                if not line or line == b"\r\n":
                    break
                if line.split(b":", 1)[0].strip().lower() not in _STRIPPED_REQUEST_HEADERS:
                    raw_headers.extend(line)
                decoded_line = line.decode("latin1").strip()
                if ":" in decoded_line:
                    k, v = decoded_line.split(":", 1)
                    headers_dict[k.strip().lower()] = v.strip()

            # 1. Enforce Authentication Check
            if self.auth:
                auth_val = headers_dict.get("proxy-authorization")
                if not validate_proxy_auth(auth_val, self.auth):
                    client_writer.write(
                        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                        b'Proxy-Authenticate: Basic realm="Plexo Proxy"\r\n'
                        b"Connection: close\r\n"
                        b"Content-Length: 0\r\n\r\n"
                    )
                    await client_writer.drain()
                    client_writer.close()
                    return

            # 2. Enforce CONNECT Port Restriction
            if method.upper() == "CONNECT":
                host, port = parse_connect_target(url)

                if port not in ALLOWED_CONNECT_PORTS:
                    msg = (
                        "HTTP/1.1 403 Forbidden\r\n"
                        "Content-Type: text/plain\r\n"
                        "Connection: close\r\n\r\n"
                        f"CONNECT to port {port} is restricted by Plexo.\r\n"
                    ).encode("utf-8")
                    client_writer.write(msg)
                    await client_writer.drain()
                    client_writer.close()
                    return

                # HTTPS Tunneling
                await self._handle_connect(host, port, client_reader, client_writer)
            else:
                # Plain HTTP proxy
                await self._handle_http(method, url, request_line + raw_headers + b"\r\n", client_reader, client_writer)

        except Exception:
            try:
                client_writer.close()
            except Exception:
                pass

    def _next_adapter(self) -> Optional[NetworkAdapter]:
        """Pick the next adapter round-robin (one adapter per connection).

        This balances *connections* across links; a single TCP flow is never
        split across adapters.
        """
        if not self._adapters:
            return None
        adapter = self._adapters[self._rr_counter % len(self._adapters)]
        self._rr_counter += 1
        return adapter

    async def _handle_connect(
        self, host: str, port: int, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        """Tunnels HTTPS connections directly."""
        try:
            adapter = self._next_adapter()

            try:
                family = socket.AF_INET if adapter and ":" not in adapter.ip_address else None
                target_ip = await resolve_destination(
                    host, port, self.allow_private, prefer_family=family
                )
            except DestinationBlocked:
                await self._reject(client_writer, "destination is not a public address")
                return

            remote_reader, remote_writer = await open_bound_connection(target_ip, port, adapter)

            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()

            await asyncio.gather(
                self._pipe(client_reader, remote_writer),
                self._pipe(remote_reader, client_writer),
                return_exceptions=True,
            )
        except Exception:
            try:
                client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await client_writer.drain()
                client_writer.close()
            except Exception:
                pass

    async def _handle_http(
        self,
        method: str,
        url: str,
        full_request: bytes,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """Forwards standard HTTP requests."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 80

        try:
            if port not in ALLOWED_CONNECT_PORTS:
                await self._reject(client_writer, f"port {port} is restricted by Plexo")
                return
            try:
                target_ip = await resolve_destination(host, port, self.allow_private)
            except DestinationBlocked:
                await self._reject(client_writer, "destination is not a public address")
                return

            adapter = self._next_adapter()
            remote_reader, remote_writer = await open_bound_connection(target_ip, port, adapter)
            remote_writer.write(full_request)
            await remote_writer.drain()

            await asyncio.gather(
                self._pipe(client_reader, remote_writer),
                self._pipe(remote_reader, client_writer),
                return_exceptions=True,
            )
        except Exception:
            try:
                client_writer.close()
            except Exception:
                pass

    async def _reject(self, client_writer: asyncio.StreamWriter, reason: str) -> None:
        """Send a 403 and close the client connection."""
        body = f"Blocked by Plexo: {reason}.\r\n".encode("utf-8")
        client_writer.write(
            b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
            b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        await client_writer.drain()
        client_writer.close()

    async def _pipe(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Pipes bytes between reader and writer until EOF."""
        try:
            while not reader.at_eof():
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


def run_proxy(
    host: str = "127.0.0.1",
    port: int = 8899,
    auth: Optional[str] = None,
    allow_private: bool = False,
) -> None:
    """Synchronous launcher for the proxy server."""
    proxy = LocalBondedProxy(
        host=host, port=port, auth=auth, allow_private=allow_private
    )
    asyncio.run(proxy.start())

import base64
import pytest
from mushy.proxy.server import (
    is_loopback_host,
    validate_proxy_auth,
    ALLOWED_CONNECT_PORTS,
    LocalBondedProxy,
)


def test_is_loopback_host():
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("127.0.0.2") is True

    # Remote / unsafe addresses
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("192.168.1.100") is False
    assert is_loopback_host("10.193.133.147") is False
    assert is_loopback_host("example.com") is False


def test_validate_proxy_auth_success():
    expected = "user:secret"
    encoded = base64.b64encode(expected.encode()).decode()
    header = f"Basic {encoded}"
    assert validate_proxy_auth(header, expected) is True


def test_validate_proxy_auth_failures():
    expected = "user:secret"

    # Missing header or credentials
    assert validate_proxy_auth(None, expected) is False
    assert validate_proxy_auth("", expected) is False

    # Wrong credentials
    wrong_encoded = base64.b64encode(b"user:wrong").decode()
    assert validate_proxy_auth(f"Basic {wrong_encoded}", expected) is False

    # Malformed header
    assert validate_proxy_auth("Bearer token123", expected) is False
    assert validate_proxy_auth("Basic !!!not-base64", expected) is False


def test_connect_port_restrictions():
    assert 80 in ALLOWED_CONNECT_PORTS
    assert 443 in ALLOWED_CONNECT_PORTS
    assert 22 not in ALLOWED_CONNECT_PORTS
    assert 25 not in ALLOWED_CONNECT_PORTS
    assert 8080 not in ALLOWED_CONNECT_PORTS


def test_proxy_init_security_guards():
    # Loopback without auth is permitted
    p1 = LocalBondedProxy(host="127.0.0.1")
    assert p1.host == "127.0.0.1"

    # Non-loopback without auth raises ValueError
    with pytest.raises(ValueError):
        LocalBondedProxy(host="0.0.0.0")

    with pytest.raises(ValueError):
        LocalBondedProxy(host="192.168.1.50")

    # Non-loopback WITH auth is permitted
    p2 = LocalBondedProxy(host="0.0.0.0", auth="user:pass")
    assert p2.auth == "user:pass"

    # There is no unauthenticated remote override any more
    with pytest.raises(ValueError):
        LocalBondedProxy(host="0.0.0.0")


# --- SSRF / destination validation (v0.2.2) -------------------------------

import asyncio  # noqa: E402
import ipaddress  # noqa: E402

import mushy.proxy.server as server_mod  # noqa: E402
from mushy.proxy.server import (  # noqa: E402
    DestinationBlocked,
    is_blocked_address,
    parse_connect_target,
    resolve_destination,
)


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1", "::1", "10.0.0.5", "172.16.3.4", "192.168.1.1",
        "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
        "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.1.2.3",
    ],
)
def test_blocked_addresses(addr):
    assert is_blocked_address(ipaddress.ip_address(addr)) is True


@pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_allowed(addr):
    assert is_blocked_address(ipaddress.ip_address(addr)) is False


def test_parse_connect_target():
    assert parse_connect_target("example.com:443") == ("example.com", 443)
    assert parse_connect_target("example.com") == ("example.com", 443)
    assert parse_connect_target("example.com:abc") == ("example.com", 443)
    assert parse_connect_target("[2001:db8::1]:8443") == ("2001:db8::1", 8443)
    assert parse_connect_target("[2001:db8::1]") == ("2001:db8::1", 443)


@pytest.mark.asyncio
async def test_resolve_destination_blocks_private_by_default():
    with pytest.raises(DestinationBlocked):
        await resolve_destination("127.0.0.1", 443)
    with pytest.raises(DestinationBlocked):
        await resolve_destination("169.254.169.254", 80)
    assert await resolve_destination("127.0.0.1", 443, allow_private=True) == "127.0.0.1"
    assert await resolve_destination("8.8.8.8", 443) == "8.8.8.8"


async def _raw_request(proxy, payload: bytes) -> bytes:
    server = await asyncio.start_server(proxy._handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(payload)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(65536), timeout=5)
        writer.close()
        return data
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_connect_to_private_destination_is_rejected():
    proxy = LocalBondedProxy()
    reply = await _raw_request(proxy, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 403")
    assert b"not a public address" in reply


@pytest.mark.asyncio
async def test_http_to_metadata_address_is_rejected():
    proxy = LocalBondedProxy()
    reply = await _raw_request(
        proxy, b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: 169.254.169.254\r\n\r\n"
    )
    assert reply.startswith(b"HTTP/1.1 403")


@pytest.mark.asyncio
async def test_http_to_restricted_port_is_rejected():
    proxy = LocalBondedProxy()
    reply = await _raw_request(
        proxy, b"GET http://8.8.8.8:8080/ HTTP/1.1\r\nHost: 8.8.8.8\r\n\r\n"
    )
    assert reply.startswith(b"HTTP/1.1 403")
    assert b"port 8080" in reply


@pytest.mark.asyncio
async def test_proxy_authorization_not_forwarded_to_origin(monkeypatch):
    received = bytearray()

    async def origin(reader, writer):
        while True:
            line = await reader.readline()
            received.extend(line)
            if line in (b"\r\n", b""):
                break
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    origin_server = await asyncio.start_server(origin, "127.0.0.1", 0)
    origin_port = origin_server.sockets[0].getsockname()[1]
    monkeypatch.setattr(server_mod, "ALLOWED_CONNECT_PORTS", {origin_port})
    try:
        creds = base64.b64encode(b"user:pw").decode()
        proxy = LocalBondedProxy(auth="user:pw", allow_private=True)
        reply = await _raw_request(
            proxy,
            (
                f"GET http://127.0.0.1:{origin_port}/ HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{origin_port}\r\n"
                f"Proxy-Authorization: Basic {creds}\r\n"
                "Proxy-Connection: keep-alive\r\n\r\n"
            ).encode(),
        )
        assert b"200 OK" in reply
        assert b"proxy-authorization" not in bytes(received).lower()
        assert b"proxy-connection" not in bytes(received).lower()
    finally:
        origin_server.close()
        await origin_server.wait_closed()


def test_round_robin_adapter_selection():
    proxy = LocalBondedProxy()
    assert proxy._next_adapter() is None
    proxy._adapters = ["a", "b", "c"]
    assert [proxy._next_adapter() for _ in range(5)] == ["a", "b", "c", "a", "b"]

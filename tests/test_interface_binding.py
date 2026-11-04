"""Interface pinning: device binding with graceful fallback (no real NICs needed)."""

import socket
from unittest.mock import MagicMock, patch

import pytest

import mushy.core.adapter as adapter_mod
from mushy.core.adapter import (
    bind_socket_to_interface,
    interface_socket_options,
)
from mushy.core.client import BoundRangeClient


@pytest.fixture(autouse=True)
def _reset_state():
    interface_socket_options.cache_clear()
    adapter_mod._warned_ifaces.clear()
    yield
    interface_socket_options.cache_clear()
    adapter_mod._warned_ifaces.clear()


def _sock(family=socket.AF_INET):
    s = MagicMock()
    s.family = family
    return s


def test_linux_device_binding_used_when_permitted():
    s = _sock()
    with patch("platform.system", return_value="Linux"):
        assert bind_socket_to_interface(s, "wlan0", "192.168.1.5") == "device"
    level, opt, value = s.setsockopt.call_args[0]
    assert (level, opt) == (socket.SOL_SOCKET, adapter_mod.SO_BINDTODEVICE)
    assert value == b"wlan0\0"
    s.bind.assert_not_called()


def test_linux_falls_back_to_source_ip_with_warning(caplog):
    s = _sock()
    s.setsockopt.side_effect = PermissionError
    with patch("platform.system", return_value="Linux"), caplog.at_level("WARNING"):
        assert bind_socket_to_interface(s, "wlan0", "192.168.1.5") == "source-ip"
    s.bind.assert_called_once_with(("192.168.1.5", 0))
    assert "policy routing" in caplog.text


def test_fallback_warning_is_emitted_once(caplog):
    with patch("platform.system", return_value="Linux"), caplog.at_level("WARNING"):
        for _ in range(3):
            s = _sock()
            s.setsockopt.side_effect = PermissionError
            bind_socket_to_interface(s, "eth1", "10.0.0.2")
    assert caplog.text.count("Could not pin sockets") == 1


def test_no_binding_possible_returns_none_and_warns(caplog):
    s = _sock()
    s.setsockopt.side_effect = PermissionError
    with patch("platform.system", return_value="Linux"), caplog.at_level("WARNING"):
        assert bind_socket_to_interface(s, "eth2", None) == "none"
    assert "No interface binding" in caplog.text


def test_windows_uses_source_ip_only():
    s = _sock()
    with patch("platform.system", return_value="Windows"):
        assert bind_socket_to_interface(s, "Wi-Fi", "192.168.0.9") == "source-ip"
    s.setsockopt.assert_not_called()


def test_macos_uses_bound_if():
    s = _sock()
    with patch("platform.system", return_value="Darwin"), patch("socket.if_nametoindex", return_value=7):
        assert bind_socket_to_interface(s, "en0", "10.0.0.2") == "device"
    level, opt, _ = s.setsockopt.call_args[0]
    assert (level, opt) == (socket.IPPROTO_IP, adapter_mod.IP_BOUND_IF)


def test_socket_options_empty_when_unprivileged():
    with patch("platform.system", return_value="Linux"), patch("socket.socket") as sock_cls:
        sock_cls.return_value.setsockopt.side_effect = PermissionError
        assert interface_socket_options("wlan0") == ()


def test_socket_options_present_when_permitted():
    with patch("platform.system", return_value="Linux"), patch("socket.socket"):
        opts = interface_socket_options("wlan0")
    assert opts == ((socket.SOL_SOCKET, adapter_mod.SO_BINDTODEVICE, b"wlan0\0"),)


def test_bound_range_client_applies_device_option():
    with patch("mushy.core.client.interface_socket_options", return_value=((1, 25, b"wlan0\0"),)):
        client = BoundRangeClient(local_ip="127.0.0.1", iface_name="wlan0")
    opts = client._pool.connection_pool_kw["socket_options"]
    assert (1, 25, b"wlan0\0") in opts
    # default TCP_NODELAY is preserved
    assert (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) in opts


def test_bound_range_client_without_iface_keeps_defaults():
    client = BoundRangeClient(local_ip="127.0.0.1")
    assert (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) in client._pool.connection_pool_kw["socket_options"]

"""Tests for adapter discovery and classification."""

from mushy.core.adapter import classify_interface, is_usable_ip, NetworkAdapter


def test_classify_interface():
    assert classify_interface("Wi-Fi") == "wifi"
    assert classify_interface("WLAN 2") == "wifi"
    assert classify_interface("Ethernet") == "ethernet"
    assert classify_interface("Apple Mobile Device Ethernet") == "usb"
    assert classify_interface("Remote NDIS Compatible Device") == "usb"
    assert classify_interface("Loopback Pseudo-Interface 1") == "other"


def test_is_usable_ip():
    assert is_usable_ip("192.168.1.15") is True
    assert is_usable_ip("10.0.0.5") is True
    assert is_usable_ip("172.16.0.100") is True

    # Invalid / Unusable IPs
    assert is_usable_ip("127.0.0.1") is False  # Loopback
    assert is_usable_ip("169.254.1.2") is False  # Link-local APIPA
    assert is_usable_ip("0.0.0.0") is False  # Unspecified
    assert is_usable_ip("invalid_ip") is False


def test_adapter_display_name():
    adapter = NetworkAdapter(
        name="Wi-Fi",
        ip_address="192.168.1.45",
        netmask="255.255.255.0",
        kind="wifi",
        is_up=True,
    )
    assert "Wi-Fi" in adapter.display_name
    assert "WIFI" in adapter.display_name
    assert "192.168.1.45" in adapter.display_name


# --- connectivity check retries (v0.3.1) -----------------------------------

def test_connectivity_retries_after_a_failed_round():
    from unittest.mock import MagicMock, patch

    import mushy.core.adapter as adapter_mod

    sock = MagicMock()
    # round 1: both hosts fail, round 2: first host succeeds
    sock.connect.side_effect = [OSError("timeout"), OSError("timeout"), None]
    with patch.object(adapter_mod.socket, "socket", return_value=sock), patch.object(
        adapter_mod.time, "sleep"
    ):
        assert adapter_mod.test_adapter_connectivity("10.0.0.2") is True
    assert sock.connect.call_count == 3


def test_connectivity_gives_up_after_all_retries():
    from unittest.mock import MagicMock, patch

    import mushy.core.adapter as adapter_mod

    sock = MagicMock()
    sock.connect.side_effect = OSError("unreachable")
    with patch.object(adapter_mod.socket, "socket", return_value=sock), patch.object(
        adapter_mod.time, "sleep"
    ):
        assert adapter_mod.test_adapter_connectivity("10.0.0.2", retries=2) is False
    assert sock.connect.call_count == 4  # 2 hosts x 2 rounds

"""Per-link summary shown after a download."""

from rich.console import Console

from mushy.core.adapter import NetworkAdapter
from mushy.core.engine import ProgressUpdate
from mushy.ui.dashboard import TerminalDashboard

MB = 1024 * 1024
USB = NetworkAdapter("usb0", "10.0.0.2", "255.255.255.0", "usb", True)
WIFI = NetworkAdapter("wlan0", "10.0.0.3", "255.255.255.0", "wifi", True)


def _update(total, usb_bytes, wifi_bytes):
    return ProgressUpdate(
        total_bytes=total,
        downloaded_bytes=usb_bytes + wifi_bytes,
        speed_bytes_sec=1.0,
        adapter_speeds={USB.ip_address: 1.0, WIFI.ip_address: 1.0},
        adapter_bytes={USB.ip_address: usb_bytes, WIFI.ip_address: wifi_bytes},
    )


def _dashboard():
    console = Console(record=True, width=140)
    return TerminalDashboard(console=console), console


def test_both_links_listed_with_share():
    d, console = _dashboard()
    d.setup("big.bin", 20 * MB, [USB, WIFI])
    d.update(_update(20 * MB, 12 * MB, 8 * MB))
    d.finish("big.bin", 20 * MB, 4.0, "abc")
    text = console.export_text()
    assert "Per-Link Data" in text
    assert "usb0: 12.0 MB (60%)" in text
    assert "wlan0: 8.0 MB (40%)" in text
    assert "carried no data" not in text


def test_small_file_explains_single_link():
    d, console = _dashboard()
    d.setup("small.bin", 3 * MB, [USB, WIFI])
    d.update(_update(3 * MB, 3 * MB, 0))
    d.finish("small.bin", 3 * MB, 1.0, "abc")
    text = console.export_text()
    assert "wlan0: 0.0 MB (0%)" in text
    assert "smaller than one 8 MB chunk" in text


def test_large_file_idle_link_is_flagged():
    d, console = _dashboard()
    d.setup("big.bin", 50 * MB, [USB, WIFI])
    d.update(_update(50 * MB, 50 * MB, 0))
    d.finish("big.bin", 50 * MB, 5.0, "abc")
    text = console.export_text()
    assert "wlan0 carried no data" in text
    assert "mushy doctor" in text


def test_bytes_accumulate_across_stages():
    d, console = _dashboard()
    d.setup("video", 10 * MB, [USB, WIFI])
    d.update(_update(10 * MB, 6 * MB, 4 * MB))
    d.setup("audio", 10 * MB, [USB, WIFI])  # second stage resets the bars
    d.update(_update(10 * MB, 2 * MB, 8 * MB))
    d.finish("out.mp4", 20 * MB, 3.0, "abc")
    text = console.export_text()
    assert "usb0: 8.0 MB (40%)" in text
    assert "wlan0: 12.0 MB (60%)" in text


def test_single_link_shows_no_per_link_rows():
    d, console = _dashboard()
    d.setup("f.bin", 5 * MB, [USB])
    d.update(
        ProgressUpdate(
            total_bytes=5 * MB,
            downloaded_bytes=5 * MB,
            speed_bytes_sec=1.0,
            adapter_speeds={USB.ip_address: 1.0},
            adapter_bytes={USB.ip_address: 5 * MB},
        )
    )
    d.finish("f.bin", 5 * MB, 1.0, "abc")
    assert "Per-Link Data" not in console.export_text()

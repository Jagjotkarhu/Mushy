"""`mushy doctor`: pinned test traffic per link, cross-checked against NIC counters."""

import http.server
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

import mushy.core.doctor as doctor
from mushy.core.adapter import NetworkAdapter

PAYLOAD = b"x" * (2 * 1024 * 1024)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/missing"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        try:
            self.wfile.write(PAYLOAD)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the probe stops early on purpose

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def out(monkeypatch):
    console = Console(record=True, width=200)
    monkeypatch.setattr(doctor, "console", console)
    return console


def _adapter(name="testnic0"):
    return NetworkAdapter(name, "127.0.0.1", "255.0.0.0", "other", True)


def _counters(**recv):
    return {nic: SimpleNamespace(bytes_recv=value) for nic, value in recv.items()}


def _run(adapters, url, before, after, **kwargs):
    with patch.object(doctor.psutil, "net_io_counters", side_effect=[before, after]):
        return doctor.run_doctor_diagnostic(
            adapters, duration_secs=5, probe_url=url, probe_bytes=1024 * 1024, **kwargs
        )


def test_ok_when_counter_confirms_traffic(server, out):
    ok = _run([_adapter()], server + "/", _counters(testnic0=0), _counters(testnic0=5_000_000))
    assert ok is True
    assert "OK" in out.export_text()


def test_leak_detected_when_nic_counter_stays_flat(server, out):
    ok = _run([_adapter()], server + "/", _counters(testnic0=0), _counters(testnic0=0))
    assert ok is False
    text = out.export_text()
    assert "LEAK" in text
    assert "policy routing" in text


def test_failed_link_is_reported(out):
    ok = _run([_adapter()], "http://127.0.0.1:1/", _counters(testnic0=0), _counters(testnic0=0))
    assert ok is False
    assert "FAILED" in out.export_text()


def test_http_error_is_reported(server, out):
    ok = _run([_adapter()], server + "/missing", _counters(testnic0=0), _counters(testnic0=0))
    assert ok is False
    assert "HTTP 404" in out.export_text()


def test_two_links_both_ok(server, out):
    adapters = [_adapter("nicA"), _adapter("nicB")]
    ok = _run(
        adapters,
        server + "/",
        _counters(nicA=0, nicB=0),
        _counters(nicA=4_000_000, nicB=4_000_000),
    )
    assert ok is True
    assert "Bonding is ready" in out.export_text()


def test_one_of_two_links_leaking_fails(server, out):
    adapters = [_adapter("nicA"), _adapter("nicB")]
    ok = _run(adapters, server + "/", _counters(nicA=0, nicB=0), _counters(nicA=4_000_000, nicB=0))
    assert ok is False
    assert "LEAK" in out.export_text()


def test_no_adapters(out):
    assert doctor.run_doctor_diagnostic([]) is False
    assert "No active network interfaces" in out.export_text()

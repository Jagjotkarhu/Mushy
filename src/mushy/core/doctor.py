"""Diagnostics: verify that every network link really carries its own traffic."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import psutil
import urllib3
import urllib3.connection
from rich.console import Console
from rich.table import Table

from .adapter import NetworkAdapter, interface_socket_options

console = Console()

DEFAULT_PROBE_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
DEFAULT_PROBE_BYTES = 8 * 1024 * 1024
# The NIC's own receive counter must account for at least this share of the bytes
# the pinned probe received, otherwise the traffic left through another interface.
MIN_COUNTER_SHARE = 0.5


@dataclass
class ProbeResult:
    """Outcome of one pinned test download over a single adapter."""

    name: str
    ip_address: str
    binding: str = "source-ip"
    bytes_received: int = 0
    seconds: float = 0.0
    error: Optional[str] = None

    @property
    def speed_mb_s(self) -> float:
        return (self.bytes_received / (1024 * 1024)) / self.seconds if self.seconds > 0 else 0.0


def _probe_adapter(
    adapter: NetworkAdapter,
    url: str,
    max_seconds: float,
    max_bytes: int,
    result: ProbeResult,
) -> None:
    """Download from ``url`` over ``adapter`` only (device-pinned when permitted)."""
    pinned = interface_socket_options(adapter.name)
    result.binding = "device" if pinned else "source-ip"
    options = list(urllib3.connection.HTTPConnection.default_socket_options) + list(pinned)
    pool = urllib3.PoolManager(
        source_address=(adapter.ip_address, 0),
        socket_options=options,
        timeout=urllib3.Timeout(connect=5.0, read=5.0),
        retries=False,
    )
    start = time.monotonic()
    try:
        resp = pool.request(
            "GET", url, preload_content=False, headers={"User-Agent": "mushy-doctor"}
        )
        try:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            for chunk in resp.stream(64 * 1024):
                result.bytes_received += len(chunk)
                if result.bytes_received >= max_bytes or time.monotonic() - start >= max_seconds:
                    break
        finally:
            resp.close()
    except Exception as exc:  # report any failure to the user
        result.error = f"{type(exc).__name__}: {exc}"[:70]
    finally:
        result.seconds = time.monotonic() - start
        pool.clear()


def _recv_counters() -> Dict[str, int]:
    return {nic: stat.bytes_recv for nic, stat in psutil.net_io_counters(pernic=True).items()}


def run_doctor_diagnostic(
    adapters: List[NetworkAdapter],
    duration_secs: float = 5.0,
    probe_url: str = DEFAULT_PROBE_URL,
    probe_bytes: int = DEFAULT_PROBE_BYTES,
) -> bool:
    """Run a short pinned download over every adapter at once and cross-check NIC counters.

    Returns True only if every link received data and its own interface counter
    confirms the traffic really used that interface.
    """
    console.print("\n[bold cyan]🔍 Running Mushy Doctor Diagnostic...[/bold cyan]")
    if not adapters:
        console.print("[bold red]No active network interfaces found.[/bold red]\n")
        return False

    console.print(
        f"Testing {len(adapters)} link(s) at the same time "
        f"(up to {duration_secs:.0f}s / {probe_bytes // (1024 * 1024)} MB each)...\n"
    )

    results = [ProbeResult(name=a.name, ip_address=a.ip_address) for a in adapters]
    before = _recv_counters()
    threads = [
        threading.Thread(
            target=_probe_adapter,
            args=(a, probe_url, duration_secs, probe_bytes, r),
            daemon=True,
        )
        for a, r in zip(adapters, results)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration_secs + 15)
    after = _recv_counters()

    table = Table(title="Link Test (each link downloads through its own interface)")
    table.add_column("Interface", style="cyan")
    table.add_column("Binding", style="blue")
    table.add_column("Received", justify="right", style="green")
    table.add_column("Speed", justify="right", style="yellow")
    table.add_column("NIC Counter", justify="right", style="green")
    table.add_column("Status", style="magenta")

    failed = 0
    leaked = 0
    for r in results:
        nic_delta = max(0, after.get(r.name, 0) - before.get(r.name, 0))
        if r.error:
            status = f"❌ FAILED ({r.error})"
            failed += 1
        elif r.bytes_received == 0:
            status = "❌ NO DATA"
            failed += 1
        elif nic_delta < r.bytes_received * MIN_COUNTER_SHARE:
            status = "⚠️ LEAK: traffic used another interface"
            leaked += 1
        else:
            status = "✅ OK"
        table.add_row(
            r.name,
            r.binding,
            f"{r.bytes_received / 1024:.0f} KB",
            f"{r.speed_mb_s:.2f} MB/s",
            f"{nic_delta / 1024:.0f} KB",
            status,
        )
    console.print(table)

    if leaked:
        console.print(
            "\n[bold yellow]⚠️ Some traffic left through a different interface than the one it was bound to.[/bold yellow]\n"
            "[white]On Linux the kernel may still route via the default gateway.[/white]\n"
            "[white]Fix: grant CAP_NET_RAW or run with 'sudo' on Linux < 5.7 (for SO_BINDTODEVICE), "
            "or configure policy routing tables.[/white]"
        )
    if failed:
        console.print(
            "\n[bold red]One or more links failed the test.[/bold red] "
            "[white]Check that the link is online, or try another test file with --url.[/white]"
        )
    if any(r.binding == "source-ip" for r in results) and not failed and not leaked:
        console.print(
            "\n[dim]Note: some links use source-IP binding only (no kernel interface pinning).[/dim]"
        )

    ok = not failed and not leaked
    if ok and len(results) == 1:
        console.print("\n[bold green]✅ Link OK.[/bold green] [white]Connect a second link to test bonding.[/white]\n")
    elif ok:
        console.print("\n[bold green]✅ Every link carried its own traffic. Bonding is ready.[/bold green]\n")
    else:
        console.print()
    return ok

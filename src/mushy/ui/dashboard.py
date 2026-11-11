"""Rich live dashboard for bonded multi-adapter downloads."""

from __future__ import annotations

from typing import Dict, List, Optional
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from ..core.adapter import NetworkAdapter
from ..core.engine import DEFAULT_CHUNK_SIZE, ProgressUpdate


class TerminalDashboard:
    """Manages live terminal display for multi-interface bonded transfers."""

    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console(legacy_windows=False)
        self.progress = Progress(
            SpinnerColumn(spinner_name="line"),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=40),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "|",
            DownloadColumn(),
            "|",
            TransferSpeedColumn(),
            "|",
            TimeRemainingColumn(),
            console=self.console,
        )
        self.overall_task: Optional[TaskID] = None
        self.adapter_tasks: Dict[str, TaskID] = {}
        self._adapter_names: Dict[str, str] = {}  # ip -> interface name
        self._stage_bytes: Dict[str, int] = {}  # ip -> bytes in the current stage
        self._carried_bytes: Dict[str, int] = {}  # ip -> bytes from finished stages

    def reset(self) -> None:
        """Clears all active tasks to allow smooth stage transitions."""
        for ip, count in self._stage_bytes.items():
            self._carried_bytes[ip] = self._carried_bytes.get(ip, 0) + count
        self._stage_bytes = {}
        for task_id in list(self.progress.task_ids):
            self.progress.remove_task(task_id)
        self.overall_task = None
        self.adapter_tasks.clear()

    def setup(
        self,
        filename: str,
        total_bytes: int,
        adapters: List[NetworkAdapter],
        mirrors: Optional[List[str]] = None,
    ) -> None:
        """Initializes the multi-bar display."""
        if self.overall_task is not None:
            self.reset()

        mirror_info = f"\nActive Mirrors: [bold green]{len(mirrors)}[/bold green]" if mirrors and len(mirrors) > 1 else ""
        self.console.print(
            Panel.fit(
                f"[bold cyan]Mushy Multi-Network Bonding Engine[/bold cyan]\n"
                f"File: [bold yellow]{filename}[/bold yellow] ({total_bytes / (1024*1024):.1f} MB)\n"
                f"Active Bonded Links: [bold green]{len(adapters)}[/bold green]{mirror_info}",
                border_style="cyan",
            )
        )

        self.overall_task = self.progress.add_task(
            "[bold green]Bonded Total", total=total_bytes
        )

        for a in adapters:
            self._adapter_names[a.ip_address] = a.name
        for a in adapters:
            kind_color = "magenta" if a.kind == "usb" else "cyan" if a.kind == "wifi" else "blue"
            task_id = self.progress.add_task(
                f"  [{kind_color}]{a.name}[/{kind_color}] ({a.ip_address})",
                total=total_bytes,
                visible=True,
            )
            self.adapter_tasks[a.ip_address] = task_id

    def update(self, update: ProgressUpdate) -> None:
        """Updates live numbers on the console."""
        if self.overall_task is not None:
            self.progress.update(
                self.overall_task,
                completed=update.downloaded_bytes,
                total=update.total_bytes,
            )

        adapter_bytes = getattr(update, "adapter_bytes", {})
        if adapter_bytes:
            self._stage_bytes.update(adapter_bytes)
        for ip, speed in update.adapter_speeds.items():
            task_id = self.adapter_tasks.get(ip)
            if task_id is not None:
                transferred = adapter_bytes.get(ip)
                if transferred is not None:
                    self.progress.update(task_id, completed=transferred)
                else:
                    self.progress.update(task_id)

    def _link_totals(self) -> Dict[str, int]:
        """Bytes carried per interface name across all stages of this run."""
        totals: Dict[str, int] = {name: 0 for name in self._adapter_names.values()}
        for ip, name in self._adapter_names.items():
            totals[name] += self._carried_bytes.get(ip, 0) + self._stage_bytes.get(ip, 0)
        return totals

    def _add_link_rows(self, table: Table, total_bytes: int) -> None:
        """Show how much each link carried, and explain when a link was unused."""
        totals = self._link_totals()
        if len(totals) < 2:
            return
        carried = sum(totals.values()) or 1
        lines = [
            f"{name}: {count / (1024 * 1024):.1f} MB ({count * 100 / carried:.0f}%)"
            for name, count in totals.items()
        ]
        table.add_row("Per-Link Data", "\n".join(lines))
        idle = [name for name, count in totals.items() if count == 0]
        if idle and total_bytes <= DEFAULT_CHUNK_SIZE:
            table.add_row(
                "Note",
                f"[dim]File is smaller than one {DEFAULT_CHUNK_SIZE // (1024 * 1024)} MB chunk, "
                "so only one link was needed.[/dim]",
            )
        elif idle:
            table.add_row(
                "Note",
                f"[yellow]{', '.join(idle)} carried no data. Run 'mushy doctor' to check it.[/yellow]",
            )

    def finish(
        self,
        filename: str,
        total_bytes: int,
        elapsed_seconds: float,
        checksum: str,
        mirrors: Optional[List[str]] = None,
    ) -> None:
        """Displays completion summary table."""
        avg_speed_mb = (total_bytes / (1024 * 1024)) / max(0.1, elapsed_seconds)

        table = Table(title="[bold green]Download Complete & Verified[/bold green]")
        table.add_column("Property", style="bold cyan")
        table.add_column("Value", style="yellow")

        table.add_row("File", filename)
        table.add_row("Total Size", f"{total_bytes / (1024*1024):.2f} MB ({total_bytes:,} bytes)")
        table.add_row("Time Taken", f"{elapsed_seconds:.2f} s")
        table.add_row("Aggregated Speed", f"[bold green]{avg_speed_mb:.2f} MB/s[/bold green]")
        self._add_link_rows(table, total_bytes)
        if mirrors and len(mirrors) > 1:
            table.add_row("Mirrors Utilized", f"[bold green]{len(mirrors)}[/bold green]")
        table.add_row("SHA-256", f"[dim]{checksum}[/dim]")

        self.console.print()
        self.console.print(table)

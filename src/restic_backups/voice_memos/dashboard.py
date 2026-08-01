"""Textual dashboard for live parallel diarize runs.

Reads per-worker log files written by `voice-memos diarize-parallel` and the
summaries/ tree to show progress + per-worker tail.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.containers import Grid
from textual.widgets import Footer, Header, RichLog, Static

SUMMARIES_DIR = Path(
    os.environ.get(
        "SUMMARIES_DIR", Path(__file__).resolve().parent.parent / "summaries"
    )
)
TAIL_LINES = 40
REFRESH_S = 2.0
HEADER_RE = re.compile(r"^\[(\d+)/(\d+)\] ([0-9A-F-]+) :: (.+)$")


def find_latest_chunkdir() -> str | None:
    cands: list[str] = []
    for base in {tempfile.gettempdir(), "/tmp", os.environ.get("TMPDIR", "")}:
        if not base:
            continue
        cands.extend(glob.glob(os.path.join(base, "diarize-chunks*")))
    cands = sorted(set(cands), key=os.path.getmtime)
    return cands[-1] if cands else None


def scan_summaries() -> tuple[int, int]:
    """Return (eligible, diarized)."""
    eligible = diarized = 0
    if not SUMMARIES_DIR.exists():
        return 0, 0
    for f in SUMMARIES_DIR.rglob("*.json"):
        if f.name == "index.json":
            continue
        try:
            d = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if (d.get("transcript") or {}).get("segments"):
            eligible += 1
            if d.get("diarization"):
                diarized += 1
    return eligible, diarized


class WorkerPanel(RichLog):
    def __init__(self, log_path: str, worker_id: int):
        super().__init__(highlight=False, markup=False, wrap=False)
        self.border_title = f"worker {worker_id}  {Path(log_path).name}"
        self.log_path = log_path
        self.last_size = 0
        self.current_memo = "(waiting...)"

    def tail(self) -> str:
        """Read new bytes, update display. Return latest [N/M] header if seen."""
        try:
            with open(self.log_path) as f:
                f.seek(self.last_size)
                new = f.read()
                self.last_size = f.tell()
        except FileNotFoundError:
            return self.current_memo
        if not new:
            return self.current_memo
        for raw in new.rstrip().split("\n"):
            line = raw.rstrip()
            self.write(line)
            m = HEADER_RE.match(line)
            if m:
                self.current_memo = f"[{m.group(1)}/{m.group(2)}] {m.group(4)}"
        self.border_title = f"worker {self.worker_id}  ▶ {self.current_memo}"
        return self.current_memo

    @property
    def worker_id(self) -> int:
        m = re.search(r"worker-(\d+)", self.log_path)
        return int(m.group(1)) if m else 0


class DiarizeDashboard(App):
    CSS = """
    Screen { layout: vertical; }
    #stats { height: 3; padding: 1; background: $boost; }
    Grid { grid-size: 1; grid-gutter: 1 1; padding: 1; }
    WorkerPanel { border: round $accent; min-height: 12; }
    """
    BINDINGS: ClassVar = [("q", "quit", "quit"), ("r", "force_refresh", "refresh")]

    def __init__(self, log_dir: str):
        super().__init__()
        self.log_dir = log_dir
        logs = sorted(glob.glob(str(Path(log_dir) / "worker-*.log")))
        self.panels = [WorkerPanel(p, i + 1) for i, p in enumerate(logs)]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("loading...", id="stats")
        grid = Grid()
        if len(self.panels) >= 2:
            grid.styles.grid_size_columns = 2
        if len(self.panels) >= 4:
            grid.styles.grid_size_rows = 2
        with grid:
            yield from self.panels
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_all()
        self.set_interval(REFRESH_S, self.refresh_all)

    def action_force_refresh(self) -> None:
        self.refresh_all()

    def refresh_all(self) -> None:
        for p in self.panels:
            p.tail()
        eligible, diarized = scan_summaries()
        pending = eligible - diarized
        pct = (100.0 * diarized / eligible) if eligible else 0.0
        bar = "█" * int(pct / 2) + "░" * (50 - int(pct / 2))
        self.query_one("#stats", Static).update(
            f"{bar}  {diarized}/{eligible} ({pct:.1f}%)  "
            f"pending: {pending}  workers: {len(self.panels)}  "
            f"log dir: {self.log_dir}"
        )


def main(log_dir: str | None = None) -> None:
    log_dir = log_dir or find_latest_chunkdir()
    if not log_dir:
        sys.exit(
            "No diarize-chunks-* directory found. "
            "Start `restic-backups voice-memos diarize-parallel` first."
        )
    if not glob.glob(str(Path(log_dir) / "worker-*.log")):
        sys.exit(f"No worker-*.log in {log_dir}. Wait for workers to start.")
    DiarizeDashboard(log_dir).run()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)

"""Parallel Voice Memos diarization process management."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ..errors import BackupError
from . import pipeline


def start(
    workers: int,
    order: str,
    min_duration: float,
    min_speakers: int,
    max_speakers: int,
    wait: bool,
) -> Path:
    """Split eligible memos across worker processes and return their log directory."""
    if workers < 1:
        raise BackupError("workers must be at least 1")
    if not (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")):
        raise BackupError("HF_TOKEN is required for diarization")

    uuids = pipeline.eligible_diarization_uuids(order, min_duration)
    if not uuids:
        raise BackupError("no memos are eligible for diarization")
    chunk_size = math.ceil(len(uuids) / workers)
    log_dir = Path(tempfile.mkdtemp(prefix="diarize-chunks-"))
    processes: list[subprocess.Popen[bytes]] = []

    for number, offset in enumerate(range(0, len(uuids), chunk_size), 1):
        chunk = uuids[offset : offset + chunk_size]
        log_path = log_dir / f"worker-{number}.log"
        command = [
            sys.executable,
            "-m",
            "restic_backups.cli",
            "voice-memos",
            "diarize",
            "--uuids",
            ",".join(chunk),
            "--order",
            "natural",
            "--min-speakers",
            str(min_speakers),
            "--max-speakers",
            str(max_speakers),
        ]
        log = log_path.open("wb")
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=not wait,
        )
        log.close()
        processes.append(process)
        (log_dir / f"worker-{number}.pid").write_text(str(process.pid))

    (log_dir / "all.txt").write_text("\n".join(uuids) + "\n")
    Path(tempfile.gettempdir(), "diarize-latest").write_text(str(log_dir))
    if wait:
        codes = [process.wait() for process in processes]
        if any(codes):
            raise BackupError(f"diarization workers failed: {codes}")
    return log_dir

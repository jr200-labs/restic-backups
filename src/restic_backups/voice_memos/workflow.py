"""Voice Memos backup and local-file workflows."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .. import config
from ..errors import BackupError
from ..generic import restic
from .pipeline import DEFAULT_RECORDINGS_DIR, SUMMARIES_DIR

JOB_ENV = "RESTIC_BACKUPS_JOB"


def job_id(jobs: dict[str, dict[str, object]]) -> str:
    requested = os.environ.get(JOB_ENV)
    if requested is not None:
        if requested not in jobs or jobs[requested]["type"] != "voice-memos":
            raise BackupError(f"voice-memos job '{requested}' is not configured")
        return requested
    matches = [name for name, job in jobs.items() if job["type"] == "voice-memos"]
    if len(matches) != 1:
        raise BackupError(
            "configure exactly one voice-memos job or set RESTIC_BACKUPS_JOB"
        )
    return matches[0]


def run_restic(
    args: list[str], *, tagged: bool = False, repository_id: str | None = None
) -> int:
    """Run restic against the configured Voice Memos repository."""
    _, storage, repositories, jobs = config.load_validated()
    selected_job = job_id(jobs)
    if tagged and args:
        args = [
            args[0],
            "--tag",
            str(jobs[selected_job].get("tag", selected_job)),
            *args[1:],
        ]
    return restic.command(
        selected_job,
        args,
        storage,
        repositories,
        jobs,
        repository_id=repository_id,
    )


def find_audio(
    query: str,
    restore: bool = False,
    target: Path | None = None,
    repository_id: str | None = None,
) -> Path:
    """Resolve a summary UUID to its recording, optionally restoring it first."""
    matches = sorted(SUMMARIES_DIR.rglob(f"{query.removesuffix('.json')}.json"))
    matches = [path for path in matches if path.name != "index.json"]
    if not matches:
        raise BackupError(f"no summary found for '{query}'")
    try:
        source = json.loads(matches[0].read_text()).get("source_path")
    except (json.JSONDecodeError, OSError) as exc:
        raise BackupError(f"could not read {matches[0]}: {exc}") from exc
    if not source:
        raise BackupError(f"summary has no source_path: {matches[0]}")

    audio = DEFAULT_RECORDINGS_DIR / source
    if audio.exists():
        return audio
    if not restore:
        raise BackupError(
            f"audio is not available locally: {audio}; pass --restore to retrieve it"
        )

    destination = (target or Path("/tmp/voice-memos-restored")).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    code = run_restic(
        [
            "restore",
            "latest",
            "--target",
            str(destination),
            "--include",
            str(DEFAULT_RECORDINGS_DIR / source),
        ],
        repository_id=repository_id,
    )
    if code:
        raise BackupError(f"restic restore failed with exit code {code}")
    restored = destination / str(DEFAULT_RECORDINGS_DIR).lstrip(os.sep) / source
    if not restored.exists():
        raise BackupError(f"restore did not produce {restored}")
    return restored


def reveal(path: Path) -> None:
    """Reveal a recording in Finder."""
    try:
        subprocess.run(["open", "-R", path], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise BackupError(f"could not reveal {path}: {exc}") from exc

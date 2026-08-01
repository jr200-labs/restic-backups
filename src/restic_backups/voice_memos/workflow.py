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

BACKUP_ID = "voice-memos"
HOST = "mac-icloud"


def run_restic(args: list[str], *, tagged: bool = False) -> int:
    """Run restic against the configured Voice Memos repository."""
    _, credentials, stores, backups = config.load_validated()
    if tagged and args:
        args = [
            args[0],
            "--tag",
            str(backups[BACKUP_ID].get("tag", BACKUP_ID)),
            *args[1:],
        ]
    return restic.command(BACKUP_ID, args, credentials, stores, backups)


def backup(recordings_dir: Path = DEFAULT_RECORDINGS_DIR) -> int:
    """Back up recordings and generated summaries in one snapshot."""
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    return run_restic(
        [
            "backup",
            str(recordings_dir.expanduser()),
            str(SUMMARIES_DIR),
            "--tag",
            "summaries",
            "--host",
            HOST,
        ]
    )


def find_audio(query: str, restore: bool = False, target: Path | None = None) -> Path:
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
        ]
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

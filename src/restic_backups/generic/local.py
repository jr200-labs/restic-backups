"""Local restic repository operations."""

from __future__ import annotations

import shutil
from typing import Any

from ..errors import BackupError
from . import repository


def is_initialized(restic_repository: dict[str, Any], storage: dict[str, Any]) -> bool:
    """Return whether the local repository has a Restic config file."""
    return (repository.local_path(restic_repository, storage) / "config").is_file()


def delete_repository(
    restic_repository: dict[str, Any], storage: dict[str, Any]
) -> int:
    """Permanently delete a local restic repository directory."""
    path = repository.local_path(restic_repository, storage)
    if path.is_symlink() or not path.is_dir():
        raise BackupError(f"local restic repository does not exist: {path}")
    entries = sum(1 for _ in path.rglob("*"))
    shutil.rmtree(path)
    return entries

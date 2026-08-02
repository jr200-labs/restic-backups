"""Resolve configured repositories and managed paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from ..config import backup_repository_ids, config_path
from ..errors import BackupError


def fail(message: str) -> NoReturn:
    raise BackupError(message)


def resolve(
    backup_id: str,
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
    repository_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    backup = backups.get(backup_id)
    if backup is None:
        fail(f"backup job '{backup_id}' not found in {config_path()}")
    repository_ids = backup_repository_ids(backup, backup_id)
    if repository_id is None:
        if len(repository_ids) != 1:
            fail(f"backup job '{backup_id}' requires a repository selection")
        repository_id = repository_ids[0]
    if repository_id not in repository_ids:
        fail(
            f"repository '{repository_id}' is not configured for backup job '{backup_id}'"
        )
    restic_repository = repositories[repository_id]
    if not restic_repository["enabled"]:
        fail(f"restic repository '{restic_repository['id']}' is disabled")
    return restic_repository, storage[restic_repository["storage-id"]]


def relative_path(restic_repository: dict[str, Any], storage: dict[str, Any]) -> Path:
    if storage["type"] == "s3":
        bucket = restic_repository["bucket"]
        key_prefix = restic_repository["key_prefix"].strip("/")
        if (
            bucket in {".", ".."}
            or "/" in bucket
            or any(part in {"", ".", ".."} for part in key_prefix.split("/"))
        ):
            fail("unsafe managed S3 repository path")
        return Path(bucket) / key_prefix
    return safe_local_repository_path(restic_repository)


def safe_local_repository_path(restic_repository: dict[str, Any]) -> Path:
    path = Path(restic_repository["path"])
    if path.is_absolute() or path == Path(".") or ".." in path.parts:
        fail("local restic repository path must be safe and relative")
    return path


def data_dir(
    backup_id: str,
    restic_repository: dict[str, Any],
    storage: dict[str, Any],
) -> Path:
    components = [storage["id"], backup_id]
    if any(not value or value in {".", ".."} or "/" in value for value in components):
        fail("unsafe managed data path component")
    root = config_path().resolve().parent
    return (
        root
        / "data"
        / storage["id"]
        / relative_path(restic_repository, storage)
        / backup_id
    )


def cache_dir(restic_repository: dict[str, Any]) -> Path:
    path = Path(restic_repository["cache-dir"]).expanduser()
    return path if path.is_absolute() else config_path().resolve().parent / path


def local_path(restic_repository: dict[str, Any], storage: dict[str, Any]) -> Path:
    root = Path(storage["path"]).expanduser().resolve()
    if not root.is_dir():
        fail(f"local storage is not mounted or is not a directory: {root}")
    path = (root / safe_local_repository_path(restic_repository)).resolve()
    if not path.is_relative_to(root):
        fail("local restic repository path resolves outside its storage root")
    return path


def location(restic_repository: dict[str, Any], storage: dict[str, Any]) -> str:
    if storage["type"] == "local":
        return str(
            Path(storage["path"]).expanduser()
            / safe_local_repository_path(restic_repository)
        )
    endpoint = storage["endpoint"].rstrip("/")
    key_prefix = restic_repository["key_prefix"].strip("/")
    return f"{endpoint}/{restic_repository['bucket']}/{key_prefix}"

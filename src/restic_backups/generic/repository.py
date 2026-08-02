"""Resolve configured repositories and managed paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from ..config import config_path
from ..errors import BackupError


def fail(message: str) -> NoReturn:
    raise BackupError(message)


def resolve(
    backup_id: str,
    credentials: dict[str, dict[str, Any]],
    stores: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    backup = backups.get(backup_id)
    if backup is None:
        fail(f"backup job '{backup_id}' not found in {config_path()}")
    store = stores[backup["restic-store-id"]]
    if not store["enabled"]:
        fail(f"restic store '{store['id']}' is disabled")
    return store, credentials[store["credentials-id"]]


def data_dir(backup_id: str, store: dict[str, Any]) -> Path:
    key_prefix = store["key_prefix"].strip("/")
    components = [store["id"], store["bucket"], backup_id]
    if any(not value or value in {".", ".."} or "/" in value for value in components):
        fail("unsafe managed data path component")
    parts = key_prefix.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        fail("unsafe key prefix for managed data")
    root = config_path().resolve().parent
    return root / "data" / components[0] / components[1] / Path(*parts) / backup_id


def cache_dir(store: dict[str, Any]) -> Path:
    path = Path(store["cache-dir"]).expanduser()
    return path if path.is_absolute() else config_path().resolve().parent / path

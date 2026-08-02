"""Load and validate backup configuration."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, NoReturn

import yaml

from .errors import BackupError
from .generic import sops

CONFIG_ENV = "RESTIC_BACKUPS_CONFIG"
PLACEHOLDER = "CHANGE_ME"
logger = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


def fail(message: str) -> NoReturn:
    raise BackupError(message)


def config_path() -> Path:
    value = os.environ.get(CONFIG_ENV)
    if not value:
        fail(f"set --config or {CONFIG_ENV}")
    return Path(value).expanduser()


def load_config(path: Path, use_sops: bool) -> dict[str, Any]:
    if use_sops:
        return sops.decrypt(path)
    try:
        loaded = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        fail(f"config file not found: {path}")
    except OSError as exc:
        fail(f"could not read {path}: {exc}")
    except yaml.YAMLError as exc:
        fail(f"config is not valid YAML: {exc}")
    if not isinstance(loaded, dict):
        fail(f"config in {path} must be a mapping")
    return loaded


def required_text(item: dict[str, Any], field: str, owner: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{owner}.{field} must be a non-empty string")
    return value


def indexed(
    config: dict[str, Any], section: str, id_field: str = "id"
) -> dict[str, dict[str, Any]]:
    items = config.get(section)
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{section} must be a non-empty list")
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ConfigError(f"{section} entries must be mappings")
        item_id = required_text(item, id_field, section)
        if item_id in result:
            raise ConfigError(f"duplicate {section} {id_field} '{item_id}'")
        result[item_id] = item
    return result


def validate(
    config: dict[str, Any],
    *,
    check_placeholders: bool = True,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    storage = indexed(config, "storage")
    repositories = indexed(config, "restic-repositories")
    backups = indexed(config, "backups", "job-id")

    for storage_id, item in storage.items():
        storage_type = required_text(item, "type", storage_id)
        if storage_type == "s3":
            required_text(item, "endpoint", storage_id)
            required_text(item, "region", storage_id)
            credentials = item.get("credentials")
            if not isinstance(credentials, dict):
                raise ConfigError(f"{storage_id}.credentials must be a mapping")
            for field in ("access-key-id", "secret-access-key"):
                required_text(credentials, field, f"{storage_id}.credentials")
        elif storage_type == "local":
            path = Path(required_text(item, "path", storage_id)).expanduser()
            if not path.is_absolute():
                raise ConfigError(f"{storage_id}.path must be absolute")
        else:
            raise ConfigError(f"{storage_id}.type must be 's3' or 'local'")

    for repository_id, item in repositories.items():
        storage_id = required_text(item, "storage-id", repository_id)
        backend = storage.get(storage_id)
        if backend is None:
            raise ConfigError(
                f"{repository_id} references unknown storage '{storage_id}'"
            )
        if not isinstance(item.get("enabled"), bool):
            raise ConfigError(f"{repository_id}.enabled must be true or false")
        required_text(item, "password", repository_id)
        if "cache-dir" in item:
            required_text(item, "cache-dir", repository_id)
        if backend["type"] == "s3":
            for field in ("bucket", "key_prefix"):
                required_text(item, field, repository_id)
            for field in ("endpoint", "region"):
                required_text(backend, field, storage_id)
            for field in ("access-key-id", "secret-access-key"):
                required_text(backend["credentials"], field, storage_id)
        else:
            repository_path = Path(required_text(item, "path", repository_id))
            if (
                repository_path.is_absolute()
                or repository_path == Path(".")
                or ".." in repository_path.parts
            ):
                raise ConfigError(f"{repository_id}.path must be a safe relative path")
        if check_placeholders and item["enabled"]:
            ensure_repository_ready(item, backend)

        archive = item.get("archive")
        if archive is None:
            continue
        if backend["type"] != "s3":
            raise ConfigError(f"{repository_id}.archive requires S3 storage")
        if not isinstance(archive, dict):
            raise ConfigError(f"{repository_id}.archive must be a mapping")
        storage_class = required_text(
            archive, "storage-class", f"{repository_id}.archive"
        )
        restore = archive.get("restore")
        if storage_class == "GLACIER_IR":
            if restore is not None:
                raise ConfigError(
                    f"{repository_id}: GLACIER_IR forbids a restore policy"
                )
            continue
        tiers = {
            "GLACIER": {"Standard", "Bulk", "Expedited"},
            "DEEP_ARCHIVE": {"Standard", "Bulk"},
        }.get(storage_class)
        if tiers is None or not isinstance(restore, dict):
            raise ConfigError(f"{repository_id} has an invalid cold-storage policy")
        if restore.get("tier") not in tiers:
            raise ConfigError(f"{repository_id} has an invalid retrieval tier")
        if not isinstance(restore.get("days"), int) or restore["days"] <= 0:
            raise ConfigError(f"{repository_id}.archive.restore.days must be positive")
        required_text(restore, "timeout", f"{repository_id}.archive.restore")

    for backup_id, backup in backups.items():
        repository_id = required_text(backup, "restic-repository-id", backup_id)
        if "tag" in backup:
            required_text(backup, "tag", backup_id)
        paths = backup.get("paths")
        if paths is not None and (
            not isinstance(paths, list)
            or not paths
            or any(not isinstance(path, str) or not path for path in paths)
        ):
            raise ConfigError(f"{backup_id}.paths must be a non-empty list of paths")
        if repository_id not in repositories:
            raise ConfigError(
                f"{backup_id} references unknown restic repository '{repository_id}'"
            )

    return storage, repositories, backups


def ensure_repository_ready(
    restic_repository: dict[str, Any], storage: dict[str, Any]
) -> None:
    """Reject placeholders only when this repository is about to be used."""
    values = [str(restic_repository["password"])]
    if storage["type"] == "s3":
        values = [
            str(restic_repository[field])
            for field in ("password", "bucket", "key_prefix")
        ]
        values.extend(str(storage[field]) for field in ("endpoint", "region"))
        values.extend(
            str(storage["credentials"][field])
            for field in ("access-key-id", "secret-access-key")
        )
    else:
        values.append(str(storage["path"]))
    if any(PLACEHOLDER in value for value in values):
        raise ConfigError(
            f"{restic_repository['id']} is enabled but contains placeholders"
        )


def load_validated(
    *, check_placeholders: bool = False
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    path = config_path()
    use_sops = os.environ.get(sops.SOPS_ENV) == "1"
    logger.info("Loading configuration: %s%s", path, " (SOPS)" if use_sops else "")
    loaded = load_config(path, use_sops)
    try:
        storage, repositories, backups = validate(
            loaded, check_placeholders=check_placeholders
        )
    except ConfigError as exc:
        fail(f"invalid config in {path}: {exc}")
    logger.debug(
        "Configuration loaded: storage=%d repositories=%d backups=%d",
        len(storage),
        len(repositories),
        len(backups),
    )
    return loaded, storage, repositories, backups

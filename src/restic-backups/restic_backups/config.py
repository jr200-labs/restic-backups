"""Load and validate backup configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, NoReturn

import yaml

from .errors import BackupError
from .generic import sops

CONFIG_ENV = "RESTIC_BACKUPS_CONFIG"
PLACEHOLDER = "CHANGE_ME"


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


def indexed(config: dict[str, Any], section: str) -> dict[str, dict[str, Any]]:
    items = config.get(section)
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{section} must be a non-empty list")
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ConfigError(f"{section} entries must be mappings")
        item_id = required_text(item, "id", section)
        if item_id in result:
            raise ConfigError(f"duplicate {section} id '{item_id}'")
        result[item_id] = item
    return result


def validate(
    config: dict[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    credentials = indexed(config, "credentials")
    stores = indexed(config, "restic-stores")
    backups = indexed(config, "backups")

    for credential_id, credential in credentials.items():
        for field in ("access-key-id", "secret-access-key"):
            required_text(credential, field, credential_id)

    for store_id, store in stores.items():
        credential_id = required_text(store, "credentials-id", store_id)
        store_credential = credentials.get(credential_id)
        if store_credential is None:
            raise ConfigError(
                f"{store_id} references unknown credentials '{credential_id}'"
            )
        if not isinstance(store.get("enabled"), bool):
            raise ConfigError(f"{store_id}.enabled must be true or false")
        required = [
            required_text(store, field, store_id)
            for field in ("endpoint", "region", "bucket", "key_prefix", "password")
        ]
        if store["enabled"] and PLACEHOLDER in required:
            raise ConfigError(f"{store_id} is enabled but contains placeholders")
        if store["enabled"] and any(
            store_credential[field] == PLACEHOLDER
            for field in ("access-key-id", "secret-access-key")
        ):
            raise ConfigError(f"{store_id} uses placeholder credentials")

        archive = store.get("archive")
        if archive is None:
            continue
        if not isinstance(archive, dict):
            raise ConfigError(f"{store_id}.archive must be a mapping")
        storage_class = required_text(archive, "storage-class", f"{store_id}.archive")
        restore = archive.get("restore")
        if storage_class == "GLACIER_IR":
            if restore is not None:
                raise ConfigError(f"{store_id}: GLACIER_IR forbids a restore policy")
            continue
        tiers = {
            "GLACIER": {"Standard", "Bulk", "Expedited"},
            "DEEP_ARCHIVE": {"Standard", "Bulk"},
        }.get(storage_class)
        if tiers is None or not isinstance(restore, dict):
            raise ConfigError(f"{store_id} has an invalid cold-storage policy")
        if restore.get("tier") not in tiers:
            raise ConfigError(f"{store_id} has an invalid retrieval tier")
        if not isinstance(restore.get("days"), int) or restore["days"] <= 0:
            raise ConfigError(f"{store_id}.archive.restore.days must be positive")
        required_text(restore, "timeout", f"{store_id}.archive.restore")

    for backup_id, backup in backups.items():
        store_id = required_text(backup, "restic-store-id", backup_id)
        if store_id not in stores:
            raise ConfigError(
                f"{backup_id} references unknown restic store '{store_id}'"
            )

    return credentials, stores, backups


def load_validated() -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    path = config_path()
    loaded = load_config(path, os.environ.get(sops.SOPS_ENV) == "1")
    try:
        credentials, stores, backups = validate(loaded)
    except ConfigError as exc:
        fail(f"invalid config in {path}: {exc}")
    return loaded, credentials, stores, backups

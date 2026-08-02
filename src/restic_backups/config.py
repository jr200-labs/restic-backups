"""Load and validate backup configuration."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

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


def job_repository_ids(job: dict[str, Any], job_id: str) -> list[str]:
    values = job.get("restic-repository-ids")
    legacy = job.get("restic-repository-id")
    if values is not None and legacy is not None:
        raise ConfigError(
            f"{job_id} cannot define both restic-repository-id and restic-repository-ids"
        )
    if values is None:
        values = [required_text(job, "restic-repository-id", job_id)]
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or not value for value in values)
        or len(values) != len(set(values))
    ):
        raise ConfigError(
            f"{job_id}.restic-repository-ids must be a non-empty list of unique IDs"
        )
    return values


backup_repository_ids = job_repository_ids


def credential_source(value: Any, owner: str) -> None:
    if not isinstance(value, dict) or set(value) not in ({"env"}, {"file"}):
        raise ConfigError(f"{owner} must contain exactly one of env or file")
    required_text(value, next(iter(value)), owner)


def github_repository_name(url: str, owner: str) -> tuple[str, str, str]:
    """Validate a github.com clone URL and return owner, repository, transport."""
    transport = "ssh"
    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
    else:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigError(f"{owner}.repository-url must be a safe github.com URL")
        path = parsed.path.lstrip("/")
        transport = "https"
    parts = path.removesuffix(".git").split("/")
    if len(parts) != 2 or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."}
        for part in parts
    ):
        raise ConfigError(f"{owner}.repository-url must identify OWNER/REPOSITORY")
    return parts[0], parts[1], transport


def validate_github(github: dict[str, Any], job_id: str) -> None:
    _, _, transport = github_repository_name(
        required_text(github, "repository-url", f"{job_id}.source"),
        f"{job_id}.source",
    )
    components = github.get("components")
    fields = {"git", "lfs", "wiki", "metadata", "release-assets"}
    if not isinstance(components, dict) or set(components) != fields:
        raise ConfigError(
            f"{job_id}.source.components must define git, lfs, wiki, metadata, and release-assets"
        )
    if any(not isinstance(value, bool) for value in components.values()):
        raise ConfigError(f"{job_id}.source.components values must be true or false")
    if not any(components.values()):
        raise ConfigError(
            f"{job_id}.source.components must enable at least one component"
        )
    if components["lfs"] and not components["git"]:
        raise ConfigError(f"{job_id}.source.components.lfs requires git")
    timeout = github.get("migration-timeout-seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(
            f"{job_id}.source.migration-timeout-seconds must be a positive integer"
        )

    authentication = github.get("authentication", {})
    if not isinstance(authentication, dict):
        raise ConfigError(f"{job_id}.source.authentication must be a mapping")
    unknown = set(authentication) - {"git", "api"}
    if unknown:
        raise ConfigError(f"{job_id}.source.authentication has unknown fields")
    git_auth = authentication.get("git", {})
    if not isinstance(git_auth, dict) or set(git_auth) - {"ssh", "https"}:
        raise ConfigError(f"{job_id}.source.authentication.git is invalid")
    if transport == "ssh" and "https" in git_auth:
        raise ConfigError(f"{job_id}: HTTPS authentication requires an HTTPS URL")
    if transport == "https" and "ssh" in git_auth:
        raise ConfigError(f"{job_id}: SSH authentication requires an SSH URL")
    if "ssh" in git_auth:
        ssh = git_auth["ssh"]
        if not isinstance(ssh, dict) or set(ssh) != {"private-key", "known-hosts"}:
            raise ConfigError(
                f"{job_id}.source.authentication.git.ssh must define private-key and known-hosts"
            )
        credential_source(ssh["private-key"], f"{job_id}.source SSH private-key")
        credential_source(ssh["known-hosts"], f"{job_id}.source SSH known-hosts")
    if "https" in git_auth:
        https = git_auth["https"]
        if not isinstance(https, dict) or set(https) != {"token"}:
            raise ConfigError(
                f"{job_id}.source.authentication.git.https must define token"
            )
        credential_source(https["token"], f"{job_id}.source HTTPS token")
    api = authentication.get("api")
    if api is not None:
        if not isinstance(api, dict) or set(api) != {"token"}:
            raise ConfigError(f"{job_id}.source.authentication.api must define token")
        credential_source(api["token"], f"{job_id}.source API token")
    if (components["metadata"] or components["release-assets"]) and api is None:
        raise ConfigError(f"{job_id}.source.authentication.api.token is required")


def indexed_jobs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if "jobs" in config and "backups" in config:
        raise ConfigError("configuration cannot define both jobs and backups")
    legacy = "jobs" not in config
    section = "backups" if legacy else "jobs"
    result = indexed(config, section, "job-id")
    normalized: dict[str, dict[str, Any]] = {}
    for job_id, original in result.items():
        job = dict(original)
        if legacy:
            if "github" in job:
                job_type, source = "github-repository", job["github"]
            elif "paths" in job:
                job_type, source = "files", {"paths": job["paths"]}
            elif job_id == "voice-memos":
                job_type, source = "voice-memos", {}
            else:
                job_type, source = "files", {}
        else:
            job_type = required_text(job, "type", job_id)
            source = job.get("source")
            if not isinstance(source, dict):
                raise ConfigError(f"{job_id}.source must be a mapping")
        if job_type not in {"files", "github-repository", "voice-memos"}:
            raise ConfigError(f"{job_id}.type is not a supported job type")
        job["type"] = job_type
        job["source"] = source
        job["_legacy"] = legacy
        normalized[job_id] = job
    return normalized


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
    jobs = indexed_jobs(config)

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

    for job_id, job in jobs.items():
        if job_id in {".", ".."} or "/" in job_id or "\\" in job_id:
            raise ConfigError(f"{job_id}.job-id must be a safe path component")
        repository_ids = job_repository_ids(job, job_id)
        if "tag" in job:
            required_text(job, "tag", job_id)
        source = job["source"]
        paths = source.get("paths") if job["type"] == "files" else None
        if (
            job["type"] == "files"
            and (paths is not None or not job["_legacy"])
            and (
                not isinstance(paths, list)
                or not paths
                or any(not isinstance(path, str) or not path for path in paths)
            )
        ):
            raise ConfigError(f"{job_id}.source.paths must be a non-empty list")
        if job["type"] == "github-repository":
            validate_github(source, job_id)
        if job["type"] == "voice-memos":
            for field in ("recordings-dir", "summaries-dir"):
                if field in source:
                    required_text(source, field, f"{job_id}.source")
        for repository_id in repository_ids:
            if repository_id not in repositories:
                raise ConfigError(
                    f"{job_id} references unknown restic repository '{repository_id}'"
                )

    return storage, repositories, jobs


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
        storage, repositories, jobs = validate(
            loaded, check_placeholders=check_placeholders
        )
    except ConfigError as exc:
        fail(f"invalid config in {path}: {exc}")
    logger.debug(
        "Configuration loaded: storage=%d repositories=%d jobs=%d",
        len(storage),
        len(repositories),
        len(jobs),
    )
    return loaded, storage, repositories, jobs

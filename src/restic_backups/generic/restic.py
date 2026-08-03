"""Execute restic for a configured repository."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, NoReturn

from .. import audit, config
from ..errors import BackupError
from . import repository

logger = logging.getLogger(__name__)

MUTATING_COMMANDS = {
    "backup",
    "copy",
    "forget",
    "init",
    "migrate",
    "prune",
    "rebuild-index",
    "recover",
    "repair",
    "rewrite",
    "tag",
    "unlock",
}
MUTATING_KEY_COMMANDS = {"add", "passwd", "remove"}


def fail(message: str) -> NoReturn:
    raise BackupError(message)


def log_output(repository_id: str, stream: str, output: str, level: int) -> None:
    """Log non-empty Restic output lines."""
    for line in output.splitlines():
        if line.strip():
            logger.log(level, "%s: restic %s: %s", repository_id, stream, line)


def mutates_repository(args: list[str]) -> bool:
    """Return whether Restic arguments can change repository data."""
    if not args or "--dry-run" in args:
        return False
    return args[0] in MUTATING_COMMANDS or (
        args[0] == "key" and len(args) > 1 and args[1] in MUTATING_KEY_COMMANDS
    )


def available_commands() -> list[tuple[str, str]]:
    """Read command names and descriptions from the installed restic."""
    try:
        result = subprocess.run(
            ["restic", "help"], check=True, capture_output=True, text=True
        )
    except FileNotFoundError:
        fail("restic is not installed")
    except subprocess.CalledProcessError as exc:
        fail(exc.stderr.strip() or "could not read restic help")
    commands: list[tuple[str, str]] = []
    reading = False
    for line in result.stdout.splitlines():
        if line == "Available Commands:":
            reading = True
            continue
        if not reading:
            continue
        if not line.strip():
            if commands:
                break
            continue
        parts = line.split(maxsplit=1)
        if not line.startswith("  ") or len(parts) != 2:
            break
        commands.append((parts[0], parts[1]))
    if not commands:
        fail("could not parse restic help")
    return commands


def command_help(command: str) -> str:
    """Read a command's help from the installed restic."""
    try:
        result = subprocess.run(
            ["restic", command, "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        fail("restic is not installed")
    except subprocess.CalledProcessError as exc:
        fail(exc.stderr.strip() or f"could not read restic {command} help")
    return result.stdout


def command_usage(command: str) -> str:
    """Read a command's usage line from the installed restic."""
    lines = command_help(command).splitlines()
    try:
        start = lines.index("Usage:")
        return next(line.strip() for line in lines[start + 1 :] if line.strip())
    except (ValueError, StopIteration):
        fail(f"could not parse restic {command} help")


def supports_dry_run(command: str) -> bool:
    """Return whether the installed restic command has a dry-run flag."""
    return "--dry-run" in command_help(command)


def command(
    backup_id: str,
    args: list[str],
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
    *,
    quiet: bool = False,
    repository_id: str | None = None,
) -> int:
    restic_repository, backend = repository.resolve(
        backup_id, storage, repositories, backups, repository_id
    )
    if args and args[0] == "backup":
        tag = str(backups[backup_id].get("tag", backup_id))
        args = ["backup", "--tag", tag, *args[1:]]
    return repository_command(restic_repository, backend, args, quiet=quiet)


def repository_command(
    restic_repository: dict[str, Any],
    storage: dict[str, Any],
    args: list[str],
    *,
    quiet: bool = False,
) -> int:
    code, _ = repository_run(restic_repository, storage, args, quiet=quiet)
    return code


def command_output(
    backup_id: str,
    args: list[str],
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
    repository_id: str | None = None,
) -> str:
    restic_repository, backend = repository.resolve(
        backup_id, storage, repositories, backups, repository_id
    )
    code, output = repository_run(
        restic_repository, backend, args, quiet=True, capture=True
    )
    if code:
        fail(f"restic {args[0]} failed with exit code {code}")
    return output


def archive_options(
    restic_repository: dict[str, Any],
    args: list[str],
    env: dict[str, str],
    *,
    destination: bool,
) -> list[str]:
    archive = restic_repository.get("archive")
    if archive is None:
        return []
    storage_class = archive["storage-class"]
    options = ["-o", f"s3.storage-class={storage_class}"] if destination else []
    if storage_class == "GLACIER_IR" or (destination and args[0] in {"init", "backup"}):
        return options
    if args[0] not in {
        "cat",
        "check",
        "copy",
        "forget",
        "prune",
        "restore",
        "snapshots",
    }:
        fail(f"restic command '{args[0]}' is not supported for cold S3 storage")
    if os.environ.get("ALLOW_ARCHIVE_RETRIEVAL") != "1":
        fail(f"set ALLOW_ARCHIVE_RETRIEVAL=1 to permit {storage_class} retrieval")
    features = {value for value in env.get("RESTIC_FEATURES", "").split(",") if value}
    features.add("s3-restore")
    env["RESTIC_FEATURES"] = ",".join(sorted(features))
    restore = archive["restore"]
    options.extend(
        (
            "-o",
            "s3.enable-restore=true",
            "-o",
            f"s3.restore-tier={restore['tier']}",
            "-o",
            f"s3.restore-days={restore['days']}",
            "-o",
            f"s3.restore-timeout={restore['timeout']}",
        )
    )
    return options


def source_context(
    source: dict[str, Any],
    source_storage: dict[str, Any],
    destination: dict[str, Any],
    destination_storage: dict[str, Any],
) -> tuple[str, dict[str, str], list[str]]:
    try:
        config.ensure_repository_ready(source, source_storage)
    except config.ConfigError as exc:
        fail(str(exc))
    if source["id"] == destination["id"]:
        fail("source and destination repositories must be different")

    source_env = {"RESTIC_FROM_PASSWORD": source["password"]}
    source_options: list[str] = []
    if source_storage["type"] == "local":
        source_location = str(repository.local_path(source, source_storage))
    else:
        source_location = repository_url(source, source_storage)
        source_credentials = source_storage["credentials"]
        source_env.update(
            AWS_ACCESS_KEY_ID=source_credentials["access-key-id"],
            AWS_SECRET_ACCESS_KEY=source_credentials["secret-access-key"],
            AWS_DEFAULT_REGION=source_storage["region"],
        )
        if destination_storage["type"] == "s3":
            if (
                any(
                    source_credentials[field]
                    != destination_storage["credentials"][field]
                    for field in ("access-key-id", "secret-access-key")
                )
                or source_storage["region"] != destination_storage["region"]
            ):
                fail(
                    "restic cannot copy directly between S3 repositories with "
                    "different credentials or regions; use an rclone backend"
                )
        else:
            source_options = ["-o", f"s3.region={source_storage['region']}"]
        source_options.extend(
            archive_options(source, ["copy"], source_env, destination=False)
        )
    return source_location, source_env, source_options


def initialize_from_source(
    destination: dict[str, Any],
    destination_storage: dict[str, Any],
    source_location: str,
    source_env: dict[str, str],
    source_options: list[str],
    *,
    dry_run: bool,
) -> int:
    """Initialize a missing destination with the source chunker parameters."""

    code, _ = repository_run(
        destination,
        destination_storage,
        ["cat", "config"],
        quiet=True,
    )
    if code == 0:
        logger.info("%s: already initialized; skipping", destination["id"])
        return 0
    if code == 10:
        logger.info(
            "%s: destination is not initialized; %s",
            destination["id"],
            (
                "would initialize with source chunker parameters"
                if dry_run
                else "initializing with source chunker parameters"
            ),
        )
        if dry_run:
            return 0
        code, _ = repository_run(
            destination,
            destination_storage,
            [
                "init",
                "--from-repo",
                source_location,
                "--copy-chunker-params",
            ],
            environment=source_env,
            extra_options=source_options,
        )
    return code


def initialize_repository_from_source(
    source: dict[str, Any],
    source_storage: dict[str, Any],
    destination: dict[str, Any],
    destination_storage: dict[str, Any],
    *,
    dry_run: bool = False,
) -> int:
    """Initialize one configured repository from another."""
    source_location, source_env, source_options = source_context(
        source, source_storage, destination, destination_storage
    )
    return initialize_from_source(
        destination,
        destination_storage,
        source_location,
        source_env,
        source_options,
        dry_run=dry_run,
    )


def copy_repository(
    source: dict[str, Any],
    source_storage: dict[str, Any],
    destination: dict[str, Any],
    destination_storage: dict[str, Any],
    snapshot_ids: list[str],
    *,
    dry_run: bool = False,
) -> int:
    """Copy snapshots between two configured repositories."""
    source_location, source_env, source_options = source_context(
        source, source_storage, destination, destination_storage
    )
    code = initialize_from_source(
        destination,
        destination_storage,
        source_location,
        source_env,
        source_options,
        dry_run=dry_run,
    )
    if code:
        return code
    if dry_run:
        logger.info(
            "%s: would copy snapshots to %s",
            source["id"],
            destination["id"],
        )
        return 0

    code, _ = repository_run(
        destination,
        destination_storage,
        ["copy", "--verbose", "--from-repo", source_location, *snapshot_ids],
        environment=source_env,
        extra_options=source_options,
        live=True,
    )
    return code


def repository_url(restic_repository: dict[str, Any], storage: dict[str, Any]) -> str:
    endpoint = storage["endpoint"].rstrip("/")
    key_prefix = restic_repository["key_prefix"].strip("/")
    return f"s3:{endpoint}/{restic_repository['bucket']}/{key_prefix}"


def repository_run(
    restic_repository: dict[str, Any],
    storage: dict[str, Any],
    args: list[str],
    *,
    quiet: bool = False,
    capture: bool = False,
    environment: dict[str, str] | None = None,
    extra_options: list[str] | None = None,
    live: bool = False,
) -> tuple[int, str]:
    if not args:
        fail("restic command required")
    if live and capture:
        fail("live restic output cannot also be captured")
    try:
        config.ensure_repository_ready(restic_repository, storage)
    except config.ConfigError as exc:
        fail(str(exc))
    logger.debug("%s: running restic %s", restic_repository["id"], args[0])

    env = os.environ.copy()
    env.update(environment or {})
    if live:
        env.setdefault("RESTIC_PROGRESS_FPS", "1")
    env["RESTIC_PASSWORD"] = restic_repository["password"]
    options = list(extra_options or [])
    if storage["type"] == "local":
        env["RESTIC_REPOSITORY"] = str(
            repository.local_path(restic_repository, storage)
        )
    else:
        credentials = storage["credentials"]
        env.update(
            AWS_ACCESS_KEY_ID=credentials["access-key-id"],
            AWS_SECRET_ACCESS_KEY=credentials["secret-access-key"],
            AWS_DEFAULT_REGION=storage["region"],
            RESTIC_REPOSITORY=repository_url(restic_repository, storage),
        )
        options.extend(("-o", f"s3.region={storage['region']}"))
    if "cache-dir" in restic_repository:
        env["RESTIC_CACHE_DIR"] = str(repository.cache_dir(restic_repository))
    options.extend(archive_options(restic_repository, args, env, destination=True))

    try:
        raw_command = ["restic", *options, *args]
        event_id = (
            audit.record_repository_write(raw_command[0], raw_command[1:])
            if mutates_repository(args)
            else None
        )
        if live:
            result = subprocess.run(
                raw_command,
                env=env,
                check=False,
                text=True,
            )
            output_text = error_text = ""
        else:
            result = subprocess.run(
                raw_command,
                env=env,
                capture_output=True,
                check=False,
                text=True,
            )
            output_text = result.stdout or ""
            error_text = result.stderr or ""
    except FileNotFoundError:
        fail("restic is not installed")
    finally:
        audit.finish(
            locals().get("event_id"),
            "result" in locals() and result.returncode == 0,
        )
    if not quiet:
        log_output(restic_repository["id"], "stdout", output_text, logging.INFO)
        log_output(
            restic_repository["id"],
            "stderr",
            error_text,
            logging.INFO if result.returncode == 0 else logging.ERROR,
        )
    elif result.returncode not in {0, 10}:
        log_output(restic_repository["id"], "stderr", error_text, logging.ERROR)
    if "operation not permitted" in error_text.lower():
        logger.error(
            "restic was blocked by macOS. Grant the terminal Full Disk Access, "
            "quit it fully, reopen it, and retry."
        )
    return result.returncode, output_text if capture else ""

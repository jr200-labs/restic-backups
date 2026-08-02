"""Execute restic for a configured repository."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from typing import Any, NoReturn

from .. import audit, config
from ..errors import BackupError
from . import repository

logger = logging.getLogger(__name__)


def fail(message: str) -> NoReturn:
    raise BackupError(message)


def available_commands() -> list[tuple[str, str]]:
    """Read command names and descriptions from the installed restic."""
    event_id = audit.record("restic", ["help"])
    try:
        result = subprocess.run(
            ["restic", "help"], check=True, capture_output=True, text=True
        )
    except FileNotFoundError:
        fail("restic is not installed")
    except subprocess.CalledProcessError as exc:
        fail(exc.stderr.strip() or "could not read restic help")
    finally:
        audit.finish(event_id, "result" in locals() and result.returncode == 0)

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
    event_id = audit.record("restic", [command, "--help"])
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
    finally:
        audit.finish(event_id, "result" in locals() and result.returncode == 0)
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
) -> int:
    restic_repository, backend = repository.resolve(
        backup_id, storage, repositories, backups
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
) -> str:
    restic_repository, backend = repository.resolve(
        backup_id, storage, repositories, backups
    )
    code, output = repository_run(
        restic_repository, backend, args, quiet=True, capture=True
    )
    if code:
        fail(f"restic {args[0]} failed with exit code {code}")
    return output


def repository_run(
    restic_repository: dict[str, Any],
    storage: dict[str, Any],
    args: list[str],
    *,
    quiet: bool = False,
    capture: bool = False,
) -> tuple[int, str]:
    if not args:
        fail("restic command required")
    try:
        config.ensure_repository_ready(restic_repository, storage)
    except config.ConfigError as exc:
        fail(str(exc))
    logger.debug("%s: running restic %s", restic_repository["id"], args[0])

    env = os.environ.copy()
    env["RESTIC_PASSWORD"] = restic_repository["password"]
    options: list[str] = []
    if storage["type"] == "local":
        env["RESTIC_REPOSITORY"] = str(
            repository.local_path(restic_repository, storage)
        )
    else:
        credentials = storage["credentials"]
        endpoint = storage["endpoint"].rstrip("/")
        key_prefix = restic_repository["key_prefix"].strip("/")
        env.update(
            AWS_ACCESS_KEY_ID=credentials["access-key-id"],
            AWS_SECRET_ACCESS_KEY=credentials["secret-access-key"],
            AWS_DEFAULT_REGION=storage["region"],
            RESTIC_REPOSITORY=(
                f"s3:{endpoint}/{restic_repository['bucket']}/{key_prefix}"
            ),
        )
        options = ["-o", f"s3.region={storage['region']}"]
    if "cache-dir" in restic_repository:
        env["RESTIC_CACHE_DIR"] = str(repository.cache_dir(restic_repository))
    archive = restic_repository.get("archive")
    if archive is not None:
        storage_class = archive["storage-class"]
        options.extend(("-o", f"s3.storage-class={storage_class}"))
        if storage_class != "GLACIER_IR":
            if args[0] in {"init", "backup"}:
                pass
            elif args[0] in {
                "check",
                "copy",
                "forget",
                "prune",
                "restore",
                "snapshots",
            }:
                if os.environ.get("ALLOW_ARCHIVE_RETRIEVAL") != "1":
                    fail(
                        f"set ALLOW_ARCHIVE_RETRIEVAL=1 to permit {storage_class} retrieval"
                    )
                features = {
                    value
                    for value in env.get("RESTIC_FEATURES", "").split(",")
                    if value
                }
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
            else:
                fail(f"restic command '{args[0]}' is not supported for cold S3 storage")

    try:
        output_target = subprocess.PIPE if capture else None
        if quiet and not capture:
            output_target = subprocess.DEVNULL
        raw_command = ["restic", *options, *args]
        event_id = audit.record(raw_command[0], raw_command[1:])
        with tempfile.TemporaryFile(mode="w+") as errors:
            result = subprocess.run(
                raw_command,
                env=env,
                stdout=output_target,
                stderr=errors,
                check=False,
                text=True,
            )
            errors.seek(0)
            error_text = errors.read()
    except FileNotFoundError:
        fail("restic is not installed")
    finally:
        audit.finish(
            locals().get("event_id"),
            "result" in locals() and result.returncode == 0,
        )
    if not quiet or result.returncode not in {0, 10}:
        print(error_text, end="", file=sys.stderr)
    if "operation not permitted" in error_text.lower():
        print(
            "\nrestic was blocked by macOS. Grant the terminal Full Disk Access, "
            "quit it fully, reopen it, and retry.",
            file=sys.stderr,
        )
    return result.returncode, result.stdout or ""

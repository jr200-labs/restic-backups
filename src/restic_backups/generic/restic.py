"""Execute restic for a configured repository."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from typing import Any, NoReturn

from ..errors import BackupError
from . import repository

logger = logging.getLogger(__name__)


def fail(message: str) -> NoReturn:
    raise BackupError(message)


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


def command_usage(command: str) -> str:
    """Read a command's usage line from the installed restic."""
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
    lines = result.stdout.splitlines()
    try:
        start = lines.index("Usage:")
        return next(line.strip() for line in lines[start + 1 :] if line.strip())
    except (ValueError, StopIteration):
        fail(f"could not parse restic {command} help")


def command(
    backup_id: str,
    args: list[str],
    credentials: dict[str, dict[str, Any]],
    stores: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
    *,
    quiet: bool = False,
) -> int:
    store, credential = repository.resolve(backup_id, credentials, stores, backups)
    if args and args[0] == "backup":
        tag = str(backups[backup_id].get("tag", backup_id))
        args = ["backup", "--tag", tag, *args[1:]]
    return store_command(store, credential, args, quiet=quiet)


def store_command(
    store: dict[str, Any],
    credential: dict[str, Any],
    args: list[str],
    *,
    quiet: bool = False,
) -> int:
    code, _ = store_run(store, credential, args, quiet=quiet)
    return code


def command_output(
    backup_id: str,
    args: list[str],
    credentials: dict[str, dict[str, Any]],
    stores: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
) -> str:
    store, credential = repository.resolve(backup_id, credentials, stores, backups)
    code, output = store_run(store, credential, args, quiet=True, capture=True)
    if code:
        fail(f"restic {args[0]} failed with exit code {code}")
    return output


def store_run(
    store: dict[str, Any],
    credential: dict[str, Any],
    args: list[str],
    *,
    quiet: bool = False,
    capture: bool = False,
) -> tuple[int, str]:
    if not args:
        fail("restic command required")
    logger.debug("%s: running restic %s", store["id"], args[0])

    env = os.environ.copy()
    endpoint = store["endpoint"].rstrip("/")
    key_prefix = store["key_prefix"].strip("/")
    env.update(
        AWS_ACCESS_KEY_ID=credential["access-key-id"],
        AWS_SECRET_ACCESS_KEY=credential["secret-access-key"],
        AWS_DEFAULT_REGION=store["region"],
        RESTIC_PASSWORD=store["password"],
        RESTIC_REPOSITORY=(f"s3:{endpoint}/{store['bucket']}/{key_prefix}"),
    )
    if "cache-dir" in store:
        env["RESTIC_CACHE_DIR"] = str(repository.cache_dir(store))
    options: list[str] = ["-o", f"s3.region={store['region']}"]
    archive = store.get("archive")
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
        with tempfile.TemporaryFile(mode="w+") as errors:
            result = subprocess.run(
                ["restic", *options, *args],
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
    if not quiet or result.returncode not in {0, 10}:
        print(error_text, end="", file=sys.stderr)
    if "operation not permitted" in error_text.lower():
        print(
            "\nrestic was blocked by macOS. Grant the terminal Full Disk Access, "
            "quit it fully, reopen it, and retry.",
            file=sys.stderr,
        )
    return result.returncode, result.stdout or ""

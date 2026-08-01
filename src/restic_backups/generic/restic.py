"""Execute restic for a configured repository."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import Any, NoReturn

from ..errors import BackupError
from . import repository


def fail(message: str) -> NoReturn:
    raise BackupError(message)


def command(
    backup_id: str,
    args: list[str],
    credentials: dict[str, dict[str, Any]],
    stores: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
) -> int:
    if not args:
        fail("restic command required")
    store, credential = repository.resolve(backup_id, credentials, stores, backups)

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
    options: list[str] = ["-o", f"s3.region={store['region']}"]
    archive = store.get("archive")
    if archive is not None:
        storage_class = archive["storage-class"]
        options.extend(("-o", f"s3.storage-class={storage_class}"))
        if storage_class != "GLACIER_IR":
            if args[0] in {"init", "backup"}:
                pass
            elif args[0] in {"check", "copy", "prune", "restore"}:
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
        with tempfile.TemporaryFile(mode="w+") as errors:
            result = subprocess.run(
                ["restic", *options, *args], env=env, stderr=errors, check=False
            )
            errors.seek(0)
            error_text = errors.read()
    except FileNotFoundError:
        fail("restic is not installed")
    print(error_text, end="", file=sys.stderr)
    if "operation not permitted" in error_text.lower():
        print(
            "\nrestic was blocked by macOS. Grant the terminal Full Disk Access, "
            "quit it fully, reopen it, and retry.",
            file=sys.stderr,
        )
    return result.returncode

"""Append-only Restic repository-write audit logging."""

from __future__ import annotations

import json
import os
import re
import socket
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .errors import BackupError

AUDIT_ENV = "RESTIC_BACKUPS_AUDIT"
AUDIT_LOG = Path("audit-log.json")
REDACTED = "[REDACTED]"
FALSE_VALUES = {"0", "false", "no", "off"}
SENSITIVE = re.compile(
    r"(?:password|secret|token|api[-_]?key|access[-_]?key|credential)", re.IGNORECASE
)
URL_PASSWORD = re.compile(r"(https?://[^:/\s]+:)[^@\s]+(@)", re.IGNORECASE)


def enabled() -> bool:
    return os.environ.get(AUDIT_ENV, "1").strip().lower() not in FALSE_VALUES


def redact_args(args: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            redacted.append(REDACTED)
            redact_next = False
            continue

        key, separator, _ = arg.partition("=")
        sensitive_key = key == "-p" or (
            key != "--insecure-no-password"
            and (key.startswith("-") or bool(separator))
            and SENSITIVE.search(key) is not None
        )
        if sensitive_key:
            if separator:
                redacted.append(f"{key}={REDACTED}")
            else:
                redacted.append(key)
                redact_next = True
            continue

        redacted.append(URL_PASSWORD.sub(rf"\1{REDACTED}\2", arg))
    return redacted


_pending: set[str] = set()


def _append(event: Mapping[str, object]) -> None:
    encoded = (json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n").encode()
    try:
        descriptor = os.open(AUDIT_LOG, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("short write")
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BackupError(f"could not append {AUDIT_LOG}: {exc}") from exc


def record_repository_write(command: str, args: list[str]) -> str | None:
    """Record a command that can change a Restic repository."""
    if not enabled():
        return None
    event_id = uuid4().hex
    event = {
        "event": "started",
        "id": event_id,
        "start-time": datetime.now(UTC).isoformat(),
        "hostname": socket.gethostname(),
        "command": command,
        "args": redact_args(args),
    }
    _append(event)
    _pending.add(event_id)
    return event_id


def finish(
    event_id: str | None,
    successful: bool,
    details: Mapping[str, object] | None = None,
) -> None:
    if event_id is None or event_id not in _pending:
        return
    event: dict[str, object] = {
        "event": "finished",
        "started-id": event_id,
        "end-time": datetime.now(UTC).isoformat(),
        "successful": successful,
    }
    if details:
        event["details"] = details
    _append(event)
    _pending.remove(event_id)


def finish_all(successful: bool) -> None:
    for event_id in tuple(_pending):
        finish(event_id, successful)

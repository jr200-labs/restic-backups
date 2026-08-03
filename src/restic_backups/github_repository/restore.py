"""Restore GitHub repositories from restic snapshots."""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn

import questionary
import typer
from rich.console import Console
from rich.text import Text

from .. import audit
from ..errors import BackupError
from ..generic.cli import choose_repository, load_snapshots
from ..generic.tui import menu_choice, select
from . import workflow

console = Console(stderr=True)


class RestoreMode(str, Enum):
    bare = "bare"
    clone = "clone"


def fail(message: str) -> NoReturn:
    console.print(Text(f"restic-backups: {message}", style="bold red"))
    raise typer.Exit(1)


def _selected_snapshot(
    job_id: str, requested: str | None, snapshots: list[dict[str, Any]]
) -> dict[str, Any]:
    if requested is None:
        if not sys.stdin.isatty():
            fail("--snapshot is required when stdin is not interactive")
        ordered = sorted(
            snapshots, key=lambda item: str(item.get("time", "")), reverse=True
        )
        choices = []
        for index, snapshot in enumerate(ordered):
            snapshot_id = snapshot["id"]
            timestamp = str(snapshot.get("time", "unknown")).replace("T", " ")[:19]
            short_id = str(snapshot.get("short_id", snapshot_id[:8]))
            hostname = str(snapshot.get("hostname", "unknown host"))
            count = len(workflow.snapshot_repositories(job_id, snapshot))
            choices.append(
                questionary.Choice(
                    f"{'Latest' if index == 0 else '':<8}{timestamp}  {short_id}  "
                    f"{hostname}  {count} "
                    f"{'repository' if count == 1 else 'repositories'}",
                    snapshot_id,
                )
            )
        choices.append(questionary.Separator(" "))
        selected = select("Snapshot to restore:", choices=choices).unsafe_ask()
        if selected is None:
            raise typer.Abort()
        requested = str(selected)
    matches = [item for item in snapshots if item["id"].startswith(requested)]
    if len(matches) != 1:
        fail(f"snapshot '{requested}' was not found or is ambiguous")
    return matches[0]


def _selected_github_repository(
    requested: str | None, available: dict[str, str]
) -> tuple[str, str]:
    if requested is not None:
        if requested not in available:
            fail(f"GitHub repository '{requested}' is not present in the snapshot")
        return requested, available[requested]
    if not sys.stdin.isatty():
        fail("--github-repository is required when stdin is not interactive")
    selected = select(
        "GitHub repository:",
        choices=[questionary.Choice(label, label) for label in available]
        + [questionary.Separator(" ")],
    ).unsafe_ask()
    if selected is None:
        raise typer.Abort()
    label = str(selected)
    return label, available[label]


def run(
    job_id: str,
    repository_id: str | None,
    snapshot_id: str | None,
    github_repository: str | None,
    mode: RestoreMode | None,
    target: Path | None,
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
) -> None:
    repository_id = choose_repository(job_id, repository_id, repositories, jobs)
    _, snapshots = load_snapshots(job_id, repository_id, storage, repositories, jobs)
    if not snapshots:
        fail(f"no snapshots are available for '{job_id}'")
    snapshot = _selected_snapshot(job_id, snapshot_id, snapshots)
    label, snapshot_path = _selected_github_repository(
        github_repository, workflow.snapshot_repositories(job_id, snapshot)
    )
    if mode is None:
        if not sys.stdin.isatty():
            fail("--mode is required when stdin is not interactive")
        selected = select(
            "Restore format:",
            choices=[
                menu_choice("Bare repository", "Preserve the Git mirror", "bare", 18),
                menu_choice("Clone", "Create a normal working checkout", "clone", 18),
                questionary.Separator(" "),
            ],
        ).unsafe_ask()
        if selected is None:
            raise typer.Abort()
        mode = RestoreMode(str(selected))
    if target is None:
        if not sys.stdin.isatty():
            fail("--target is required when stdin is not interactive")
        selected_target = questionary.path("Restore target:").unsafe_ask()
        if not selected_target:
            raise typer.Abort()
        target = Path(str(selected_target))

    event_id = audit.record(
        "restic-backups",
        [
            "job",
            "restore",
            job_id,
            "--repository",
            repository_id,
            "--snapshot",
            snapshot["id"],
            "--github-repository",
            label,
            "--mode",
            mode.value,
            "--target",
            str(target),
        ],
    )
    try:
        workflow.restore_repository(
            job_id,
            snapshot["id"],
            snapshot_path,
            target,
            mode.value,
            repository_id,
            storage,
            repositories,
            jobs,
        )
    except (BackupError, OSError) as exc:
        audit.finish(event_id, False)
        fail(str(exc))
    audit.finish(event_id, True)
    console.print(
        Text(f"Restored {label} as {mode.value} to {target}", style="bold green")
    )

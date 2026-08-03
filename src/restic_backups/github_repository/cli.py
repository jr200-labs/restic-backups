"""CLI and TUI for GitHub repository backups."""

from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, NoReturn

import questionary
import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .. import audit, config
from ..errors import BackupError
from ..generic import restic
from ..generic.cli import (
    choose_dry_run,
    choose_repository,
    load_snapshots,
    print_typer_help,
)
from ..generic.tui import menu_choice as tui_menu_choice
from ..generic.tui import select
from . import workflow

app = typer.Typer(
    help="Incrementally back up GitHub repositories and related GitHub data.",
    invoke_without_command=True,
    no_args_is_help=False,
)
console = Console()
error_console = Console(stderr=True)


class RestoreMode(str, Enum):
    bare = "bare"
    clone = "clone"


def menu_choice(label: str, description: str, value: str) -> questionary.Choice:
    return tui_menu_choice(label, description, value, 18)


def fail(message: str) -> NoReturn:
    error_console.print(Text(f"restic-backups: {message}", style="bold red"))
    raise typer.Exit(1)


def validated() -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    try:
        return config.load_validated()
    except BackupError as exc:
        fail(str(exc))


def github_jobs(backups: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        job_id: item
        for job_id, item in backups.items()
        if item["type"] in {"github-owner", "github-repository"}
    }


def choose_job(job_id: str | None, backups: dict[str, dict[str, Any]]) -> str:
    jobs = github_jobs(backups)
    if job_id is not None:
        if job_id not in jobs:
            fail(f"GitHub repository backup job '{job_id}' not found")
        return job_id
    if not jobs:
        fail("no GitHub repository backup jobs are configured")
    if not sys.stdin.isatty():
        fail("job ID is required when stdin is not interactive")
    selected = select(
        "GitHub repository job:",
        choices=[
            questionary.Choice(
                f"{item_id}  ({item['source'].get('owner-url', 'configured URLs')})",
                item_id,
            )
            for item_id, item in jobs.items()
        ]
        + [questionary.Separator(" ")],
    ).unsafe_ask()
    if selected is None:
        raise typer.Abort()
    return str(selected)


@app.callback()
def menu(context: typer.Context) -> None:
    """Choose a GitHub repository operation when run interactively."""
    if context.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty():
        typer.echo(context.get_help())
        return
    interactive_menu()


def interactive_menu() -> None:
    while True:
        selected = select(
            "GitHub repository command:",
            choices=[
                menu_choice(
                    "List jobs", "Show configured GitHub repository jobs", "list"
                ),
                menu_choice(
                    "Run backup", "Update local data and create snapshots", "backup"
                ),
                menu_choice(
                    "Show status",
                    "Show latest snapshots and component results",
                    "status",
                ),
                menu_choice(
                    "Show data path", "Print the managed local workspace", "data-dir"
                ),
                menu_choice(
                    "Restore repository",
                    "Recover one Git repository from a snapshot",
                    "restore",
                ),
                menu_choice("Help", "Show commands and flags", "help"),
                menu_choice("Back", "Return to the top-level workflows", "back"),
                questionary.Separator(" "),
            ],
        ).unsafe_ask()
        if selected in {None, "back"}:
            return
        try:
            if selected == "list":
                list_command()
                return
            if selected == "backup":
                backup_command(None, choose_dry_run(), None)
                return
            if selected == "status":
                status_command(None)
                return
            if selected == "data-dir":
                data_dir_command(None)
                return
            if selected == "restore":
                restore_command(None, None, None, None, None, None)
                return
            print_typer_help(app, "restic-backups github-repository")
        except typer.Abort:
            continue


@app.command("list")
def list_command() -> None:
    """List configured GitHub repository backup jobs."""
    _, _, _, backups = validated()
    table = Table(title="GitHub repository backups", box=box.ROUNDED)
    table.add_column("Job ID", style="cyan")
    table.add_column("Repositories")
    table.add_column("Destinations")
    table.add_column("Components")
    for job_id, item in github_jobs(backups).items():
        components = [
            name for name, enabled in item["source"]["components"].items() if enabled
        ]
        table.add_row(
            job_id,
            str(item["source"]["owner-url"])
            if "owner-url" in item["source"]
            else "\n".join(item["source"]["repository-urls"]),
            "\n".join(config.backup_repository_ids(item, job_id)),
            ", ".join(components),
        )
    console.print(table)


@app.command("backup")
def backup_command(
    job: Annotated[
        str | None,
        typer.Argument(help="Job ID; prompts when omitted.", metavar="JOB_ID"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate and show the plan without writing."),
    ] = False,
    repository_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--repository",
            "-r",
            help="Repository ID; repeat to back up to multiple repositories.",
        ),
    ] = None,
) -> None:
    """Compatibility alias for job run."""
    from ..jobs.cli import run_command

    run_command(job, dry_run, repository_ids)


@app.command("status")
def status_command(
    job: str | None = typer.Argument(
        None,
        help="Job ID for component details; omit to show all jobs.",
        metavar="JOB_ID",
    ),
) -> None:
    """Show latest snapshots for all GitHub jobs and optional component details."""
    _, storage, repositories, backups = validated()
    jobs = github_jobs(backups)
    if not jobs:
        fail("no GitHub repository backup jobs are configured")
    if job is not None:
        job_id = choose_job(job, backups)
        jobs = {job_id: jobs[job_id]}

    table = Table(title="GitHub repository status", box=box.ROUNDED)
    table.add_column("Job ID", style="cyan")
    table.add_column("Destination")
    table.add_column("Latest snapshot")
    table.add_column("Snapshot time")
    table.add_column("Local update")
    table.add_column("Components")
    failed = False
    manifests: dict[str, dict[str, Any] | None] = {}
    snapshots_by_repository: dict[str, list[dict[str, Any]] | None] = {}
    for job_id, item in jobs.items():
        try:
            manifest = workflow.read_manifest(job_id)
        except (BackupError, OSError):
            manifest = None
            failed = True
        manifests[job_id] = manifest
        component_status = (
            ", ".join(
                f"{name}: {result['status']}"
                for name, result in workflow.manifest_components(manifest)
            )
            if manifest
            else "not run"
        )
        for repository_id in config.backup_repository_ids(item, job_id):
            snapshot_id = snapshot_time = "—"
            if not config.repository_is_enabled(repositories[repository_id]):
                snapshot_id = "disabled"
            else:
                try:
                    if repository_id not in snapshots_by_repository:
                        output = restic.command_output(
                            job_id,
                            ["snapshots", "--json"],
                            storage,
                            repositories,
                            backups,
                            repository_id,
                        )
                        snapshots = json.loads(output)
                        if not isinstance(snapshots, list):
                            raise TypeError("restic snapshots did not return a list")
                        snapshots_by_repository[repository_id] = snapshots
                    snapshots = snapshots_by_repository[repository_id]
                    if snapshots is None:
                        raise ValueError("repository snapshots are unavailable")
                    tag = str(item.get("tag", job_id))
                    tagged = [
                        snapshot
                        for snapshot in snapshots
                        if tag in snapshot.get("tags", [])
                    ]
                    if tagged:
                        latest = max(
                            tagged, key=lambda value: str(value.get("time", ""))
                        )
                        snapshot_id = str(
                            latest.get("short_id", str(latest.get("id", ""))[:8])
                        )
                        snapshot_time = str(latest.get("time", "—"))
                    else:
                        snapshot_id = "none"
                except (BackupError, json.JSONDecodeError, TypeError, ValueError):
                    snapshots_by_repository[repository_id] = None
                    snapshot_id = "error"
                    failed = True
            table.add_row(
                job_id,
                repository_id,
                snapshot_id,
                snapshot_time,
                str(manifest.get("updated-at", "—")) if manifest else "not run",
                component_status,
            )
    console.print(table)

    if job is not None:
        manifest = manifests[job]
        if manifest is not None:
            detail = Table(title=f"{job} component details", box=box.ROUNDED)
            detail.add_column("Component")
            detail.add_column("Status")
            detail.add_column("Error")
            for component, result in workflow.manifest_components(manifest):
                detail.add_row(component, result["status"], result.get("error", "—"))
            console.print(detail)
    if failed:
        raise typer.Exit(1)


@app.command("data-dir")
def data_dir_command(
    job: str | None = typer.Argument(
        None, help="Job ID; prompts when omitted.", metavar="JOB_ID"
    ),
) -> None:
    """Print the managed local workspace for a GitHub job."""
    _, _, _, backups = validated()
    job_id = choose_job(job, backups)
    try:
        typer.echo(workflow.data_dir(job_id))
    except BackupError as exc:
        fail(str(exc))


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
            marker = "Latest" if index == 0 else ""
            choices.append(
                questionary.Choice(
                    f"{marker:<8}{timestamp}  {short_id}  {hostname}  "
                    f"{count} {'repository' if count == 1 else 'repositories'}",
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


@app.command("restore")
def restore_command(
    job: Annotated[
        str | None,
        typer.Argument(help="GitHub job ID; prompts when omitted.", metavar="JOB_ID"),
    ] = None,
    repository_id: Annotated[
        str | None,
        typer.Option("--repository", "-r", help="Restic repository ID."),
    ] = None,
    snapshot_id: Annotated[
        str | None,
        typer.Option("--snapshot", help="Full or unambiguous snapshot ID prefix."),
    ] = None,
    github_repository: Annotated[
        str | None,
        typer.Option(
            "--github-repository", help="Repository to restore as OWNER/REPOSITORY."
        ),
    ] = None,
    mode: Annotated[
        RestoreMode | None,
        typer.Option("--mode", help="Restore a bare mirror or normal working clone."),
    ] = None,
    target: Annotated[
        Path | None,
        typer.Option("--target", "-t", help="Absent or empty destination directory."),
    ] = None,
) -> None:
    """Restore one GitHub repository from a selected snapshot."""
    _, storage, repositories, jobs = validated()
    job_id = choose_job(job, jobs)
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
                menu_choice("Bare repository", "Preserve the Git mirror", "bare"),
                menu_choice("Clone", "Create a normal working checkout", "clone"),
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
            "github-repository",
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
    error_console.print(
        Text(f"Restored {label} as {mode.value} to {target}", style="bold green")
    )

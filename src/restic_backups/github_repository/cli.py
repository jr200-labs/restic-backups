"""CLI and TUI for GitHub repository backups."""

from __future__ import annotations

import json
import sys
from typing import Annotated, Any, NoReturn

import questionary
import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .. import config
from ..errors import BackupError
from ..generic import restic
from ..generic.cli import choose_dry_run, print_typer_help
from ..generic.tui import select
from . import workflow

app = typer.Typer(
    help="Incrementally back up GitHub repositories and related GitHub data.",
    invoke_without_command=True,
    no_args_is_help=False,
)
console = Console()
error_console = Console(stderr=True)


def menu_choice(label: str, description: str, value: str) -> questionary.Choice:
    return questionary.Choice(
        [("fg:ansicyan bold", f"{label:<18}"), ("", description)], value
    )


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
            if not repositories[repository_id]["enabled"]:
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

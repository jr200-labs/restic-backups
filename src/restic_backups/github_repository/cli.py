"""CLI and TUI for GitHub repository backups."""

from __future__ import annotations

import sys
from typing import Annotated, Any, NoReturn

import questionary
import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .. import audit, config
from ..errors import BackupError
from ..generic.cli import choose_dry_run, choose_repositories, print_typer_help
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
    return {job_id: item for job_id, item in backups.items() if "github" in item}


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
                f"{item_id}  ({item['github']['repository-url']})", item_id
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
                    "Show status", "Show the latest component results", "status"
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
    table.add_column("Repository")
    table.add_column("Destinations")
    table.add_column("Components")
    for job_id, item in github_jobs(backups).items():
        components = [
            name for name, enabled in item["github"]["components"].items() if enabled
        ]
        table.add_row(
            job_id,
            item["github"]["repository-url"],
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
    """Update one GitHub export and snapshot it to selected repositories."""
    _, storage, repositories, backups = validated()
    job_id = choose_job(job, backups)
    selected = choose_repositories(job_id, repository_ids, repositories, backups)
    args = [
        "github-repository",
        "backup",
        job_id,
        *[
            value
            for repository_id in selected
            for value in ("--repository", repository_id)
        ],
        *(["--dry-run"] if dry_run else []),
    ]
    event_id = audit.record("restic-backups", args)
    statuses: dict[str, str] = {}
    destinations: dict[str, bool] = {}
    try:
        statuses, destinations = workflow.backup(
            job_id,
            backups[job_id],
            selected,
            storage,
            repositories,
            backups,
            dry_run=dry_run,
        )
    except (BackupError, OSError) as exc:
        audit.finish(event_id, False)
        fail(str(exc))
    successful = all(
        value not in {"failed", "stale"} for value in statuses.values()
    ) and all(destinations.values())
    audit.finish(
        event_id,
        successful,
        {"components": statuses, "destinations": destinations},
    )
    for component, status in statuses.items():
        style = (
            "green"
            if status in {"updated", "unchanged", "not-present", "planned"}
            else "yellow"
        )
        error_console.print(Text(f"{job_id}: {component}: {status}", style=style))
    for repository_id, result in destinations.items():
        action = (
            "would snapshot"
            if dry_run
            else "snapshot created"
            if result
            else "snapshot failed"
        )
        error_console.print(
            Text(
                f"{job_id}: {repository_id}: {action}",
                style="green" if result else "red",
            )
        )
    if not successful:
        raise typer.Exit(1)


@app.command("status")
def status_command(
    job: str | None = typer.Argument(
        None, help="Job ID; prompts when omitted.", metavar="JOB_ID"
    ),
) -> None:
    """Show the most recent local component results."""
    _, _, _, backups = validated()
    job_id = choose_job(job, backups)
    try:
        manifest = workflow.read_manifest(job_id)
    except (BackupError, OSError) as exc:
        fail(str(exc))
    if manifest is None:
        fail(f"GitHub repository backup job '{job_id}' has not run")
    table = Table(title=f"{job_id} — {manifest['updated-at']}", box=box.ROUNDED)
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Error")
    for component, result in manifest["components"].items():
        table.add_row(component, result["status"], result.get("error", "—"))
    console.print(table)


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

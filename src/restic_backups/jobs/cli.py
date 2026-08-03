"""Unified CLI and job-first TUI."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any, NoReturn

import questionary
import typer
from rich import box
from rich.console import Console
from rich.table import Table

from .. import audit, config, metrics
from ..errors import BackupError
from ..generic import cli as generic_cli
from ..generic import restic
from ..generic.tui import group_disabled_choices, select
from ..generic.tui import menu_choice as tui_menu_choice
from ..github_repository import restore as github_restore
from ..github_repository import workflow as github_workflow
from . import workflow

app = typer.Typer(
    help="List, run, and inspect configured jobs.", invoke_without_command=True
)
console = Console()
error_console = Console(stderr=True)
logger = logging.getLogger(__name__)


def menu_choice(
    label: str,
    description: str,
    value: str,
    width: int = 20,
) -> questionary.Choice:
    return tui_menu_choice(label, description, value, width)


def fail(message: str) -> NoReturn:
    logger.error("%s", message)
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


def choose_job(
    job_id: str | None,
    jobs: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
) -> str:
    if job_id is not None:
        if job_id not in jobs:
            fail(f"job '{job_id}' not found in {config.config_path()}")
        return job_id
    if not sys.stdin.isatty():
        fail("job ID is required when stdin is not interactive")
    width = max(20, max(map(len, jobs)))
    choices: list[questionary.Choice | questionary.Separator] = []
    disabled_choices: list[tuple[str, str]] = []
    for item_id, job in jobs.items():
        description = f"[{job['type']}]  {str(job.get('description', '')).strip()}"
        available = any(
            generic_cli.repository_is_available(repositories[value])
            for value in config.job_repository_ids(job, item_id)
        )
        if available:
            choices.append(menu_choice(item_id, description, item_id, width))
        else:
            disabled_choices.append(
                (f"{item_id:<{width}}  {description}", "no available repositories")
            )
    choices = group_disabled_choices(
        choices,
        disabled_choices,
        heading="Disabled jobs",
        label_width=width,
    )
    choices.extend(
        [
            menu_choice("Back", "Return to the Jobs menu", "back", width),
            questionary.Separator(" "),
        ]
    )
    selected = select(
        "Job:",
        choices=choices,
    ).unsafe_ask()
    if selected in {None, "back"}:
        raise typer.Abort()
    return str(selected)


@app.callback()
def menu(context: typer.Context) -> None:
    if context.invoked_subcommand is None:
        if not sys.stdin.isatty():
            typer.echo(context.get_help())
        else:
            interactive_menu()


def interactive_menu() -> None:
    while True:
        selected = select(
            "Jobs:",
            choices=[
                menu_choice("Run", "Run or inspect one configured job", "select", 10),
                menu_choice("List", "Show every configured job", "list", 10),
                menu_choice(
                    "Status", "Show latest snapshots for every job", "status", 10
                ),
                menu_choice("Help", "Show commands and flags", "help", 10),
                menu_choice("Back", "Return to the top-level menu", "back", 10),
                questionary.Separator(" "),
            ],
        ).unsafe_ask()
        if selected in {None, "back"}:
            return
        if selected == "list":
            try:
                list_command()
            except typer.Exit:
                continue
        elif selected == "status":
            try:
                status_command(None)
            except typer.Exit:
                continue
        elif selected == "help":
            generic_cli.print_typer_help(app, "restic-backups job")
        else:
            _, storage, repositories, jobs = validated()
            generic_cli.probe_repository_initialization(storage, repositories)
            try:
                job_menu(choose_job(None, jobs, repositories))
            except typer.Abort:
                continue


def job_menu(job_id: str) -> None:
    while True:
        _, _, _, jobs = validated()
        job = jobs[job_id]
        choices = [
            menu_choice("Run", "Prepare and snapshot this job", "run"),
        ]
        if job["type"] in {"github-owner", "github-repository"}:
            choices.append(
                menu_choice(
                    "Restore",
                    "Recover a bare repository or working clone",
                    "github-restore",
                )
            )
        if job["type"] == "voice-memos":
            choices.append(
                menu_choice(
                    "Tools",
                    "Transcribe, diarize, restore, and inspect",
                    "voice",
                )
            )
        choices.extend(
            [
                menu_choice(
                    "Status", "Show latest snapshots and job details", "status"
                ),
                menu_choice("Snapshots", "List immutable restore points", "snapshots"),
                menu_choice("Restic", "Run a native Restic command", "restic"),
            ]
        )
        if job["type"] in {"github-owner", "github-repository"}:
            choices.append(
                menu_choice("Data", "Show the managed GitHub workspace", "data-dir")
            )
        choices.extend(
            [
                menu_choice("Help", "Show commands and flags", "help"),
                menu_choice("Back", "Choose another job", "back"),
                questionary.Separator(" "),
            ]
        )
        selected = select(
            f"Job {job_id} [{job['type']}]:", choices=choices
        ).unsafe_ask()
        if selected in {None, "back"}:
            return
        if selected == "run":
            try:
                run_command(job_id, None, None)
            except typer.Abort:
                continue
        elif selected == "status":
            try:
                status_command(job_id)
            except typer.Exit:
                continue
        elif selected == "snapshots":
            try:
                generic_cli.snapshots_command(job_id)
            except (typer.Abort, typer.Exit):
                continue
            continue
        elif selected == "restic":
            generic_cli.restic_menu(job_id)
        elif selected == "data-dir":
            try:
                data_dir_command(job_id)
            except typer.Exit:
                continue
        elif selected == "github-restore":
            restore_command(job_id, None, None, None, None, None)
        elif selected == "voice":
            from ..voice_memos import workflow as voice_workflow
            from ..voice_memos.cli import interactive_menu as voice_menu

            previous = os.environ.get(voice_workflow.JOB_ENV)
            os.environ[voice_workflow.JOB_ENV] = job_id
            try:
                voice_menu()
            finally:
                if previous is None:
                    os.environ.pop(voice_workflow.JOB_ENV, None)
                else:
                    os.environ[voice_workflow.JOB_ENV] = previous
        else:
            generic_cli.print_typer_help(app, "restic-backups job")


def source_summary(job: dict[str, Any]) -> str:
    source = job["source"]
    if job["type"] == "files":
        return "\n".join(source.get("paths", [])) or "—"
    if job["type"] == "github-repository":
        return "\n".join(source["repository-urls"])
    if job["type"] == "github-owner":
        return str(source["owner-url"])
    return str(source.get("recordings-dir", "macOS Voice Memos"))


@app.command("list")
def list_command() -> None:
    """Show every configured job, regardless of type."""
    _, storage, repositories, jobs = validated()
    generic_cli.probe_repository_initialization(storage, repositories)
    table = Table(title="Jobs", box=box.ROUNDED)
    table.add_column("Job ID", style="cyan")
    table.add_column("Type")
    table.add_column("Description")
    table.add_column("Repositories")
    table.add_column("Source")
    table.add_column("Tag")
    for job_id, job in jobs.items():
        repository_ids = config.job_repository_ids(job, job_id)
        enabled = sum(
            generic_cli.repository_is_available(repositories[value])
            for value in repository_ids
        )
        table.add_row(
            job_id,
            job["type"],
            str(job.get("description", "—")),
            "\n".join(
                f"{value}{f' ({reason})' if (reason := generic_cli.repository_disabled_reason(repositories[value])) else ''}"
                for value in repository_ids
            ),
            source_summary(job),
            str(job.get("tag", job_id)),
            style=None if enabled == len(repository_ids) else "yellow",
        )
    console.print(table)


@app.command("run")
def run_command(
    job: Annotated[
        str | None, typer.Argument(help="Job ID; prompts when omitted.")
    ] = None,
    dry_run: Annotated[bool | None, typer.Option("--dry-run")] = None,
    repository_ids: Annotated[
        list[str] | None,
        typer.Option("--repository", "-r", help="Repeat for each destination."),
    ] = None,
) -> None:
    """Run any configured job type."""
    _, storage, repositories, jobs = validated()
    if job is None or repository_ids is None:
        generic_cli.probe_repository_initialization(storage, repositories)
    job_id = choose_job(job, jobs, repositories)
    selected = generic_cli.choose_repositories(
        job_id, repository_ids, repositories, jobs
    )
    if dry_run is None:
        dry_run = (
            generic_cli.choose_dry_run(
                generic_cli.copyable_cli_command(
                    "job",
                    "run",
                    job_id,
                    *[value for item in selected for value in ("--repository", item)],
                )
            )
            if sys.stdin.isatty()
            else False
        )
    event_id = (
        audit.record_repository_write(
            "restic-backups",
            [
                "job",
                "run",
                job_id,
                *[v for r in selected for v in ("--repository", r)],
            ],
        )
        if not dry_run
        else None
    )
    started = time.monotonic()
    try:
        states, destinations = workflow.run(
            job_id, jobs[job_id], selected, storage, repositories, jobs, dry_run=dry_run
        )
    except (BackupError, OSError) as exc:
        audit.finish(event_id, False)
        metrics.record_job(
            job_id,
            jobs[job_id]["type"],
            False,
            time.monotonic() - started,
            {repository_id: False for repository_id in selected},
            dry_run=dry_run,
        )
        fail(str(exc))
    successful = all(
        value not in {"failed", "stale"} for value in states.values()
    ) and all(destinations.values())
    audit.finish(event_id, successful, {"job": states, "destinations": destinations})
    metrics.record_job(
        job_id,
        jobs[job_id]["type"],
        successful,
        time.monotonic() - started,
        destinations,
        dry_run=dry_run,
    )
    for name, state in states.items():
        logger.info("%s: %s: %s", job_id, name, state)
    for repository_id, result in destinations.items():
        message = (
            "would snapshot"
            if dry_run
            else "snapshot created"
            if result
            else "snapshot failed"
        )
        (logger.info if result else logger.error)(
            "%s: %s: %s", job_id, repository_id, message
        )
    if not successful:
        raise typer.Exit(1)


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
        typer.Option("--github-repository", help="Repository as OWNER/REPOSITORY."),
    ] = None,
    mode: Annotated[
        github_restore.RestoreMode | None,
        typer.Option("--mode", help="Restore a bare mirror or working clone."),
    ] = None,
    target: Annotated[
        Path | None,
        typer.Option("--target", "-t", help="Absent or empty destination directory."),
    ] = None,
) -> None:
    """Restore one repository from a GitHub job snapshot."""
    _, storage, repositories, jobs = validated()
    if job is None or repository_id is None:
        generic_cli.probe_repository_initialization(storage, repositories)
    github_jobs = {
        job_id: item
        for job_id, item in jobs.items()
        if item["type"] in {"github-owner", "github-repository"}
    }
    job_id = choose_job(job, github_jobs, repositories)
    github_restore.run(
        job_id,
        repository_id,
        snapshot_id,
        github_repository,
        mode,
        target,
        storage,
        repositories,
        jobs,
    )


@app.command("data-dir")
def data_dir_command(
    job: Annotated[
        str | None, typer.Argument(help="GitHub job ID; prompts when omitted.")
    ] = None,
) -> None:
    """Show the managed local workspace for a GitHub job."""
    _, _, repositories, jobs = validated()
    github_jobs = {
        job_id: item
        for job_id, item in jobs.items()
        if item["type"] in {"github-owner", "github-repository"}
    }
    job_id = choose_job(job, github_jobs, repositories)
    typer.echo(github_workflow.data_dir(job_id))


@app.command("status")
def status_command(job: str | None = typer.Argument(None)) -> None:
    """Show latest Restic snapshots for all jobs or one job."""
    _, storage, repositories, jobs = validated()
    generic_cli.probe_repository_initialization(storage, repositories)
    selected_jobs = jobs
    if job is not None:
        job_id = choose_job(job, jobs, repositories)
        selected_jobs = {job_id: jobs[job_id]}
    snapshots_by_repository: dict[str, list[dict[str, Any]] | None] = {}
    table = Table(title="Job status", box=box.ROUNDED)
    for heading in ("Job ID", "Type", "Destination", "Snapshot", "Time", "Job state"):
        table.add_column(heading, style="cyan" if heading == "Job ID" else None)
    failed = False
    for job_id, item in selected_jobs.items():
        state = "—"
        if item["type"] in {"github-owner", "github-repository"}:
            manifest = github_workflow.read_manifest(job_id)
            state = (
                ", ".join(
                    f"{key}: {value['status']}"
                    for key, value in github_workflow.manifest_components(manifest)
                )
                if manifest
                else "not run"
            )
        for repository_id in config.job_repository_ids(item, job_id):
            snapshot_id = snapshot_time = "—"
            if not generic_cli.repository_is_available(repositories[repository_id]):
                snapshot_id = str(
                    generic_cli.repository_disabled_reason(repositories[repository_id])
                )
            else:
                try:
                    if repository_id not in snapshots_by_repository:
                        loaded = json.loads(
                            restic.command_output(
                                job_id,
                                ["snapshots", "--json"],
                                storage,
                                repositories,
                                jobs,
                                repository_id,
                            )
                        )
                        if not isinstance(loaded, list):
                            raise TypeError
                        snapshots_by_repository[repository_id] = loaded
                    snapshots = snapshots_by_repository[repository_id] or []
                    tag = str(item.get("tag", job_id))
                    tagged = [
                        value for value in snapshots if tag in value.get("tags", [])
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
                except (BackupError, json.JSONDecodeError, TypeError):
                    snapshots_by_repository[repository_id] = None
                    snapshot_id = "error"
                    failed = True
            table.add_row(
                job_id, item["type"], repository_id, snapshot_id, snapshot_time, state
            )
    console.print(table)
    if failed:
        raise typer.Exit(1)

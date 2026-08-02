"""Unified CLI and job-first TUI."""

from __future__ import annotations

import json
import os
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
from ..generic import cli as generic_cli
from ..generic import restic
from ..generic.tui import select
from ..github_repository import workflow as github_workflow
from . import workflow

app = typer.Typer(
    help="List, run, and inspect configured jobs.", invoke_without_command=True
)
console = Console()
error_console = Console(stderr=True)


def menu_choice(
    label: str, description: str, value: str, width: int = 20
) -> questionary.Choice:
    return questionary.Choice(
        [("fg:ansicyan bold", f"{label:<{width}}"), ("", description)], value
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


def choose_job(job_id: str | None, jobs: dict[str, dict[str, Any]]) -> str:
    if job_id is not None:
        if job_id not in jobs:
            fail(f"job '{job_id}' not found in {config.config_path()}")
        return job_id
    if not sys.stdin.isatty():
        fail("job ID is required when stdin is not interactive")
    selected = select(
        "Job:",
        choices=[
            menu_choice(
                job_id,
                f"[{job['type']}]  {job.get('description', '')}",
                job_id,
                max(20, max(map(len, jobs))),
            )
            for job_id, job in jobs.items()
        ]
        + [questionary.Separator(" ")],
    ).unsafe_ask()
    if selected is None:
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
                menu_choice("List jobs", "Show every configured job", "list"),
                menu_choice("Status", "Show latest snapshots for every job", "status"),
                menu_choice(
                    "Select job", "Run or inspect one configured job", "select"
                ),
                menu_choice("Help", "Show job commands and flags", "help"),
                menu_choice("Back", "Return to the top-level menu", "back"),
                questionary.Separator(" "),
            ],
        ).unsafe_ask()
        if selected in {None, "back"}:
            return
        if selected == "list":
            list_command()
        elif selected == "status":
            status_command(None)
        elif selected == "help":
            generic_cli.print_typer_help(app, "restic-backups job")
        else:
            _, _, _, jobs = validated()
            try:
                job_menu(choose_job(None, jobs))
            except typer.Abort:
                continue


def job_menu(job_id: str) -> None:
    while True:
        _, _, _, jobs = validated()
        job = jobs[job_id]
        choices = [
            menu_choice("Run backup", "Prepare and snapshot this job", "run"),
            menu_choice("Status", "Show latest snapshots and job details", "status"),
            menu_choice("Snapshots", "List immutable restore points", "snapshots"),
            menu_choice("Advanced restic", "Run a native Restic command", "restic"),
        ]
        if job["type"] in {"github-owner", "github-repository"}:
            choices.append(
                menu_choice(
                    "Data directory", "Show the managed GitHub workspace", "data-dir"
                )
            )
        if job["type"] == "voice-memos":
            choices.append(
                menu_choice(
                    "Voice Memos tools",
                    "Transcribe, diarize, restore, and inspect",
                    "voice",
                )
            )
        choices.extend(
            [
                menu_choice("Help", "Show job commands and flags", "help"),
                menu_choice("Back", "Choose another job", "back"),
                questionary.Separator(" "),
            ]
        )
        selected = select(
            f"Job: {job_id} [{job['type']}]", choices=choices
        ).unsafe_ask()
        if selected in {None, "back"}:
            return
        if selected == "run":
            run_command(job_id, generic_cli.choose_dry_run(), None)
        elif selected == "status":
            status_command(job_id)
        elif selected == "snapshots":
            generic_cli.snapshots_command(job_id)
        elif selected == "restic":
            generic_cli.restic_menu(job_id)
        elif selected == "data-dir":
            typer.echo(github_workflow.data_dir(job_id))
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
    _, _, repositories, jobs = validated()
    table = Table(title="Jobs", box=box.ROUNDED)
    table.add_column("Job ID", style="cyan")
    table.add_column("Type")
    table.add_column("Description")
    table.add_column("Repositories")
    table.add_column("Source")
    table.add_column("Tag")
    for job_id, job in jobs.items():
        repository_ids = config.job_repository_ids(job, job_id)
        enabled = sum(repositories[value]["enabled"] for value in repository_ids)
        table.add_row(
            job_id,
            job["type"],
            str(job.get("description", "—")),
            "\n".join(repository_ids),
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
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    repository_ids: Annotated[
        list[str] | None,
        typer.Option("--repository", "-r", help="Repeat for each destination."),
    ] = None,
) -> None:
    """Run any configured job type."""
    _, storage, repositories, jobs = validated()
    job_id = choose_job(job, jobs)
    selected = generic_cli.choose_repositories(
        job_id, repository_ids, repositories, jobs
    )
    event_id = audit.record(
        "restic-backups",
        [
            "job",
            "run",
            job_id,
            *[v for r in selected for v in ("--repository", r)],
            *(["--dry-run"] if dry_run else []),
        ],
    )
    try:
        states, destinations = workflow.run(
            job_id, jobs[job_id], selected, storage, repositories, jobs, dry_run=dry_run
        )
    except (BackupError, OSError) as exc:
        audit.finish(event_id, False)
        fail(str(exc))
    successful = all(
        value not in {"failed", "stale"} for value in states.values()
    ) and all(destinations.values())
    audit.finish(event_id, successful, {"job": states, "destinations": destinations})
    for name, state in states.items():
        error_console.print(
            Text(
                f"{job_id}: {name}: {state}",
                style="green" if state in {"updated", "planned"} else "yellow",
            )
        )
    for repository_id, result in destinations.items():
        message = (
            "would snapshot"
            if dry_run
            else "snapshot created"
            if result
            else "snapshot failed"
        )
        error_console.print(
            Text(
                f"{job_id}: {repository_id}: {message}",
                style="green" if result else "red",
            )
        )
    if not successful:
        raise typer.Exit(1)


@app.command("status")
def status_command(job: str | None = typer.Argument(None)) -> None:
    """Show latest Restic snapshots for all jobs or one job."""
    _, storage, repositories, jobs = validated()
    selected_jobs = jobs
    if job is not None:
        job_id = choose_job(job, jobs)
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
            if not repositories[repository_id]["enabled"]:
                snapshot_id = "disabled"
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

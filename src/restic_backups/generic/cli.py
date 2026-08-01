"""CLI for generic configured restic repositories."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Annotated, Any, NoReturn

import questionary
import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .. import config
from ..errors import BackupError
from . import repository, restic, s3

app = typer.Typer(
    help="Generic configured restic repository commands.",
    invoke_without_command=True,
    no_args_is_help=False,
)
console = Console()
error_console = Console(stderr=True)


@app.callback()
def menu(context: typer.Context) -> None:
    """Choose a generic backup operation when run interactively."""
    if context.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty():
        typer.echo(context.get_help())
        return
    selected = questionary.select(
        "Command:",
        choices=[
            questionary.Choice("List configuration", "list"),
            questionary.Choice("Initialize repositories", "init"),
            questionary.Choice("Back up configured paths", "backup"),
            questionary.Choice("Forget a snapshot", "forget"),
            questionary.Choice("Destroy a repository", "destroy"),
            questionary.Choice("Show managed data directory", "data-dir"),
            questionary.Choice("Run a restic command", "run"),
            questionary.Choice("Exit", "exit"),
        ],
    ).ask()
    if selected == "list":
        list_command()
    elif selected == "init":
        init_command()
    elif selected == "backup":
        backup_command(None)
    elif selected == "forget":
        forget_command(None)
    elif selected == "destroy":
        destroy_command(None)
    elif selected == "data-dir":
        data_dir_command(None)
    elif selected == "run":
        try:
            commands = restic.available_commands()
        except BackupError as exc:
            fail(str(exc))
        command = questionary.select(
            "Restic command:",
            choices=[
                questionary.Choice(f"{name:<12} {description}", name)
                for name, description in commands
            ],
        ).ask()
        if command is None:
            raise typer.Abort()
        arguments = questionary.text(
            f"Arguments for 'restic {command}' (optional; use --help for options):"
        ).ask()
        if arguments is not None:
            try:
                run_args(None, [str(command), *shlex.split(arguments)])
            except ValueError as exc:
                fail(f"invalid arguments: {exc}")
    elif selected is None:
        raise typer.Abort()


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


def choose_backup(
    backup_id: str | None,
    stores: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
) -> str:
    if backup_id is not None:
        if backup_id not in backups:
            fail(f"backup '{backup_id}' not found in {config.config_path()}")
        return backup_id
    if not sys.stdin.isatty():
        fail("--backup is required when stdin is not interactive")
    choices = [
        questionary.Choice(
            f"{item_id}  ({stores[item['restic-store-id']]['endpoint']})",
            value=item_id,
        )
        for item_id, item in backups.items()
        if stores[item["restic-store-id"]]["enabled"]
    ]
    if not choices:
        fail("no enabled backups are available")
    selected = questionary.select("Backup:", choices=choices).ask()
    if selected is None:
        raise typer.Abort()
    return str(selected)


@app.command("list")
def list_command() -> None:
    """Show configured repositories and backups."""
    _, _, stores, backups = validated()

    store_table = Table(title="Repositories", box=box.ROUNDED)
    store_table.add_column("ID", style="cyan", no_wrap=True)
    store_table.add_column("Description")
    store_table.add_column("Endpoint")
    store_table.add_column("State")
    for store_id, store in stores.items():
        state = "enabled" if store["enabled"] else "disabled"
        store_table.add_row(
            Text(store_id),
            Text(str(store.get("description", "—"))),
            Text(str(store["endpoint"])),
            Text(state, style="green" if store["enabled"] else "yellow"),
        )
    console.print(store_table)

    backup_table = Table(title="Backups", box=box.ROUNDED)
    backup_table.add_column("ID", style="cyan", no_wrap=True)
    backup_table.add_column("Description")
    backup_table.add_column("Repository")
    backup_table.add_column("Paths")
    backup_table.add_column("Tag")
    backup_table.add_column("State")
    for backup_id, backup in backups.items():
        store = stores[backup["restic-store-id"]]
        state = "enabled" if store["enabled"] else "disabled"
        backup_table.add_row(
            Text(backup_id),
            Text(str(backup.get("description", "—"))),
            Text(str(store["id"])),
            Text("\n".join(backup.get("paths", [])) or "—"),
            Text(str(backup.get("tag", backup_id))),
            Text(state, style="green" if store["enabled"] else "yellow"),
        )
    console.print(backup_table)


@app.command("backup")
def backup_command(
    backup: Annotated[
        str | None,
        typer.Argument(help="Backup ID; prompts when omitted."),
    ] = None,
) -> None:
    """Create a snapshot from a backup's configured paths."""
    _, credentials, stores, backups = validated()
    backup_id = choose_backup(backup, stores, backups)
    paths = backups[backup_id].get("paths")
    if not paths:
        fail(f"backup '{backup_id}' has no configured paths")
    expanded_paths = [str(Path(path).expanduser()) for path in paths]
    error_console.print(
        Text(
            f"{backup_id}: backing up {len(expanded_paths)} configured path(s)",
            style="cyan",
        )
    )
    try:
        code = restic.command(
            backup_id,
            ["backup", *expanded_paths],
            credentials,
            stores,
            backups,
        )
    except BackupError as exc:
        fail(str(exc))
    if code:
        raise typer.Exit(code)
    error_console.print(Text(f"{backup_id}: snapshot created", style="bold green"))


@app.command("data-dir")
def data_dir_command(
    backup: str | None = typer.Argument(None, help="Backup ID; prompts when omitted."),
) -> None:
    """Print the managed local data directory for a backup."""
    _, credentials, stores, backups = validated()
    backup_id = choose_backup(backup, stores, backups)
    try:
        store, _ = repository.resolve(backup_id, credentials, stores, backups)
        typer.echo(repository.data_dir(backup_id, store))
    except BackupError as exc:
        fail(str(exc))


@app.command("init")
def init_command() -> None:
    """Initialize every enabled restic store that does not already exist."""
    _, credentials, stores, _ = validated()
    for store_id, store in stores.items():
        if not store["enabled"]:
            error_console.print(Text(f"{store_id}: disabled; skipping", style="yellow"))
            continue
        credential = credentials[store["credentials-id"]]
        error_console.print(Text(f"{store_id}: checking repository", style="cyan"))
        try:
            code = restic.store_command(
                store,
                credential,
                ["cat", "config"],
                quiet=True,
            )
            if code == 0:
                error_console.print(
                    Text(f"{store_id}: already initialized; skipping", style="yellow")
                )
                continue
            if code != 10:
                raise typer.Exit(code)
            error_console.print(
                Text(f"{store_id}: not initialized; initializing", style="cyan")
            )
            code = restic.store_command(store, credential, ["init"])
        except BackupError as exc:
            fail(str(exc))
        if code:
            raise typer.Exit(code)
        error_console.print(Text(f"{store_id}: initialized", style="green"))


@app.command("forget")
def forget_command(
    backup: Annotated[
        str | None,
        typer.Argument(help="Backup ID; prompts when omitted."),
    ] = None,
) -> None:
    """Forget one snapshot and prune its unreferenced data."""
    if not sys.stdin.isatty():
        fail("forget requires an interactive terminal")
    _, credentials, stores, backups = validated()
    backup_id = choose_backup(backup, stores, backups)
    tag = str(backups[backup_id].get("tag", backup_id))
    try:
        output = restic.command_output(
            backup_id,
            ["snapshots", "--tag", tag, "--json"],
            credentials,
            stores,
            backups,
        )
        snapshots = json.loads(output)
    except (BackupError, json.JSONDecodeError) as exc:
        fail(str(exc))
    if not isinstance(snapshots, list):
        fail("restic snapshots returned invalid JSON")
    if not snapshots:
        error_console.print(
            Text(f"{backup_id}: no snapshots tagged '{tag}'", style="yellow")
        )
        return

    choices: list[questionary.Choice] = []
    snapshot_by_id: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str):
            fail("restic snapshots returned invalid JSON")
        snapshot_id = snapshot["id"]
        snapshot_by_id[snapshot_id] = snapshot
        timestamp = str(snapshot.get("time", "unknown")).replace("T", " ")[:19]
        short_id = str(snapshot.get("short_id", snapshot_id[:8]))
        hostname = str(snapshot.get("hostname", "unknown host"))
        snapshot_paths = snapshot.get("paths", [])
        if not isinstance(snapshot_paths, list):
            fail("restic snapshots returned invalid JSON")
        paths = ", ".join(str(path) for path in snapshot_paths)
        choices.append(
            questionary.Choice(
                f"{timestamp}  {short_id}  {hostname}  {paths}", snapshot_id
            )
        )

    selected = questionary.select("Snapshot to forget:", choices).ask()
    if selected is None:
        raise typer.Abort()
    snapshot_id = str(selected)
    short_id = str(snapshot_by_id[snapshot_id].get("short_id", snapshot_id[:8]))
    confirmed = questionary.confirm(
        f"Forget snapshot '{short_id}' and prune its unreferenced data?",
        default=False,
    ).ask()
    if confirmed is not True:
        error_console.print(Text("Cancelled; nothing was forgotten.", style="yellow"))
        return
    try:
        code = restic.command(
            backup_id,
            ["forget", snapshot_id, "--prune"],
            credentials,
            stores,
            backups,
        )
    except BackupError as exc:
        fail(str(exc))
    if code:
        raise typer.Exit(code)
    error_console.print(
        Text(f"{backup_id}: forgot snapshot {short_id}", style="bold green")
    )


@app.command("destroy")
def destroy_command(
    repository_id: Annotated[
        str | None,
        typer.Argument(help="Repository ID; prompts when omitted."),
    ] = None,
) -> None:
    """Permanently erase a configured repository from S3."""
    if not sys.stdin.isatty():
        fail("destroy requires an interactive terminal")
    _, credentials, stores, _ = validated()
    if repository_id is None:
        selected = questionary.select(
            "Repository to permanently destroy:",
            choices=[
                questionary.Choice(
                    f"{store_id}  ({store['endpoint']}/{store['bucket']}/{store['key_prefix']})",
                    store_id,
                )
                for store_id, store in stores.items()
            ],
        ).ask()
        if selected is None:
            raise typer.Abort()
        repository_id = str(selected)
    store = stores.get(repository_id)
    if store is None:
        fail(f"repository '{repository_id}' not found in {config.config_path()}")

    target = (
        f"{store['endpoint'].rstrip('/')}/{store['bucket']}/"
        f"{store['key_prefix'].strip('/')}"
    )
    confirmed = questionary.confirm(
        f"Permanently destroy '{repository_id}' and all data at {target}?",
        default=False,
    ).ask()
    if confirmed is not True:
        error_console.print(Text("Cancelled; nothing was destroyed.", style="yellow"))
        return
    typed = questionary.text(
        f"Type '{repository_id}' to confirm permanent destruction:"
    ).ask()
    if typed != repository_id:
        fail("repository ID did not match; nothing was destroyed")

    credential = credentials[store["credentials-id"]]
    deleted = s3.delete_repository(store, credential)
    error_console.print(
        Text(
            f"{repository_id}: permanently destroyed {deleted} objects and versions",
            style="bold green",
        )
    )


@app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run_command(
    context: typer.Context,
    backup: Annotated[
        str | None,
        typer.Option("--backup", "-b", help="Backup ID; prompts when omitted."),
    ] = None,
) -> None:
    """Run restic with all trailing arguments passed through unchanged."""
    run_args(backup, list(context.args))


def run_args(backup: str | None, args: list[str]) -> None:
    _, credentials, stores, backups = validated()
    backup_id = choose_backup(backup, stores, backups)
    if not args:
        fail("a restic command is required after 'run'")
    try:
        raise typer.Exit(restic.command(backup_id, args, credentials, stores, backups))
    except BackupError as exc:
        fail(str(exc))

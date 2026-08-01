"""CLI for generic configured restic repositories."""

from __future__ import annotations

import shlex
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
from . import repository, restic

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
            questionary.Choice("Show managed data directory", "data-dir"),
            questionary.Choice("Run a restic command", "run"),
            questionary.Choice("Exit", "exit"),
        ],
    ).ask()
    if selected == "list":
        list_command()
    elif selected == "init":
        init_command()
    elif selected == "data-dir":
        data_dir_command(None)
    elif selected == "run":
        command = questionary.text("Restic command and arguments:").ask()
        if command:
            try:
                run_args(None, shlex.split(command))
            except ValueError as exc:
                fail(f"invalid command: {exc}")
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
    """Show configured repositories and backup jobs."""
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
    backup_table.add_column("State")
    for backup_id, backup in backups.items():
        store = stores[backup["restic-store-id"]]
        state = "enabled" if store["enabled"] else "disabled"
        backup_table.add_row(
            Text(backup_id),
            Text(str(backup.get("description", "—"))),
            Text(str(store["id"])),
            Text(state, style="green" if store["enabled"] else "yellow"),
        )
    console.print(backup_table)


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

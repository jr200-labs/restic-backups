"""CLI for generic configured restic repositories."""

from __future__ import annotations

import sys
from typing import Annotated, Any, NoReturn

import questionary
import typer

from .. import config
from ..errors import BackupError
from . import repository, restic

app = typer.Typer(
    help="Generic configured restic repository commands.", no_args_is_help=True
)


def fail(message: str) -> NoReturn:
    typer.echo(f"restic-backups: {message}", err=True)
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
    """List backup IDs and their configured restic stores."""
    _, _, stores, backups = validated()
    for backup_id, backup in backups.items():
        store = stores[backup["restic-store-id"]]
        state = "enabled" if store["enabled"] else "disabled"
        typer.echo(f"{backup_id}\t{store['id']}\t{store['endpoint']}\t{state}")


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
            typer.echo(f"{store_id}: disabled; skipping")
            continue
        credential = credentials[store["credentials-id"]]
        typer.echo(f"{store_id}: checking repository")
        try:
            code = restic.store_command(
                store,
                credential,
                ["cat", "config"],
                quiet=True,
            )
            if code == 0:
                typer.echo(f"{store_id}: already initialized; skipping")
                continue
            if code != 10:
                raise typer.Exit(code)
            typer.echo(f"{store_id}: not initialized; initializing")
            code = restic.store_command(store, credential, ["init"])
        except BackupError as exc:
            fail(str(exc))
        if code:
            raise typer.Exit(code)
        typer.echo(f"{store_id}: initialized")


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
    _, credentials, stores, backups = validated()
    backup_id = choose_backup(backup, stores, backups)
    if not context.args:
        fail("a restic command is required after 'run'")
    try:
        raise typer.Exit(
            restic.command(backup_id, list(context.args), credentials, stores, backups)
        )
    except BackupError as exc:
        fail(str(exc))

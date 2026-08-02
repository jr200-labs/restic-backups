"""CLI for generic configured restic repositories."""

from __future__ import annotations

import json
import os
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
from . import repository, restic, s3, sops

app = typer.Typer(
    help="Generic configured restic repository commands.",
    invoke_without_command=True,
    no_args_is_help=False,
)
repository_app = typer.Typer(help="Manage configured restic repositories.")
backup_app = typer.Typer(help="Manage configured backup jobs.")
snapshot_app = typer.Typer(help="List and forget restic snapshots.")
restic_app = typer.Typer(help="Run advanced restic commands.")
app.add_typer(repository_app, name="repository")
app.add_typer(backup_app, name="backup")
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(restic_app, name="restic")
console = Console()
error_console = Console(stderr=True)
ALL_REPOSITORIES = "__all_repositories__"


def menu_choice(
    label: str, description: str, value: str, width: int = 23
) -> questionary.Choice:
    return questionary.Choice(
        [("fg:ansicyan bold", f"{label:<{width}}"), ("", description)], value
    )


@app.callback()
def menu(context: typer.Context) -> None:
    """Choose a generic backup operation when run interactively."""
    if context.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty():
        typer.echo(context.get_help())
        return
    interactive_menu()


def interactive_menu() -> None:
    """Navigate generic operations with arrow-key menus."""
    while True:
        selected = questionary.select(
            "Section:",
            choices=[
                menu_choice(
                    "Repositories",
                    "List, initialize, cache, or destroy repositories",
                    "repository",
                    17,
                ),
                menu_choice(
                    "Backups", "List or run configured backup jobs", "backup", 17
                ),
                menu_choice(
                    "Snapshots",
                    "List or forget immutable restore points",
                    "snapshot",
                    17,
                ),
                menu_choice(
                    "Advanced restic",
                    "Run any command supported by installed restic",
                    "restic",
                    17,
                ),
                menu_choice("Help", "Show help for generic commands", "help", 17),
                menu_choice("Back", "Return to the previous menu", "back", 17),
                questionary.Separator(" "),
            ],
        ).ask()
        if selected in {None, "back"}:
            return
        if selected == "help":
            print_typer_help(app, "restic-backups generic")
        elif selected == "repository":
            repository_menu()
        elif selected == "backup":
            backup_menu()
        elif selected == "snapshot":
            snapshot_menu()
        elif selected == "restic":
            restic_menu()


def print_typer_help(application: typer.Typer, name: str) -> None:
    command = typer.main.get_command(application)
    console.print(command.get_help(typer.Context(command, info_name=name)))


def repository_menu() -> None:
    while True:
        selected = questionary.select(
            "Repository command:",
            choices=[
                menu_choice(
                    "List repositories",
                    "Show configured storage destinations",
                    "list",
                ),
                menu_choice(
                    "Initialize repository",
                    "Create one repository, or explicitly all",
                    "init",
                ),
                menu_choice(
                    "Prime local cache",
                    "Download and validate repository metadata",
                    "prime-cache",
                ),
                menu_choice(
                    "Destroy repository",
                    "Permanently erase repository objects",
                    "destroy",
                ),
                menu_choice("Help", "Show repository command flags", "help"),
                menu_choice("Back", "Return to Generic sections", "back"),
                questionary.Separator(" "),
            ],
        ).ask()
        if selected in {None, "back"}:
            return
        if selected == "help":
            print_typer_help(repository_app, "restic-backups generic repository")
        elif selected == "list":
            repository_list_command()
            return
        elif selected == "init":
            init_command()
            return
        elif selected == "prime-cache":
            prime_cache_command(None)
            return
        elif selected == "destroy":
            destroy_command(None)
            return


def backup_menu() -> None:
    while True:
        selected = questionary.select(
            "Backup command:",
            choices=[
                menu_choice("List backups", "Show configured backup jobs", "list", 18),
                menu_choice(
                    "Run backup",
                    "Create a snapshot from configured paths",
                    "run",
                    18,
                ),
                menu_choice(
                    "Show data path",
                    "Print the managed local metadata directory",
                    "data-dir",
                    18,
                ),
                menu_choice("Help", "Show backup command flags", "help", 18),
                menu_choice("Back", "Return to Generic sections", "back", 18),
                questionary.Separator(" "),
            ],
        ).ask()
        if selected in {None, "back"}:
            return
        if selected == "help":
            print_typer_help(backup_app, "restic-backups generic backup")
        elif selected == "list":
            backup_list_command()
            return
        elif selected == "run":
            backup_command(None)
            return
        elif selected == "data-dir":
            data_dir_command(None)
            return


def snapshot_menu() -> None:
    while True:
        selected = questionary.select(
            "Snapshot command:",
            choices=[
                menu_choice(
                    "List snapshots",
                    "Show restore points for a configured backup",
                    "list",
                    18,
                ),
                menu_choice(
                    "Forget snapshot",
                    "Delete one restore point and prune data",
                    "forget",
                    18,
                ),
                menu_choice("Help", "Show snapshot command flags", "help", 18),
                menu_choice("Back", "Return to Generic sections", "back", 18),
                questionary.Separator(" "),
            ],
        ).ask()
        if selected in {None, "back"}:
            return
        if selected == "help":
            print_typer_help(snapshot_app, "restic-backups generic snapshot")
        elif selected == "list":
            snapshots_command(None)
            return
        elif selected == "forget":
            forget_command(None)
            return


def restic_menu() -> None:
    try:
        commands = restic.available_commands()
    except BackupError as exc:
        fail(str(exc))
    while True:
        selected = questionary.select(
            "Restic command:",
            choices=[
                *[
                    menu_choice(name, description, name, 13)
                    for name, description in commands
                ],
                menu_choice(
                    "Help", "Show help for advanced restic passthrough", "help", 13
                ),
                menu_choice("Back", "Return to Generic sections", "back", 13),
                questionary.Separator(" "),
            ],
        ).ask()
        if selected in {None, "back"}:
            return
        if selected == "help":
            print_typer_help(restic_app, "restic-backups generic restic")
            continue
        command = str(selected)
        try:
            usage = restic.command_usage(command)
        except BackupError as exc:
            fail(str(exc))
        console.print(Text(f"Usage: {usage}", style="dim"))
        while True:
            action = questionary.select(
                f"restic {command}:",
                choices=[
                    menu_choice(
                        "Enter arguments",
                        "Build and optionally run this command",
                        "run",
                        17,
                    ),
                    menu_choice("Help", "Show full flags for this command", "help", 17),
                    menu_choice("Back", "Choose another restic command", "back", 17),
                    questionary.Separator(" "),
                ],
            ).ask()
            if action in {None, "back"}:
                break
            if action == "help":
                try:
                    console.print(restic.command_help(command))
                except BackupError as exc:
                    fail(str(exc))
                continue
            arguments = questionary.text(
                f"Arguments for 'restic {command}' (optional):"
            ).ask()
            if arguments is None:
                continue
            try:
                args = shlex.split(arguments)
            except ValueError as exc:
                fail(f"invalid arguments: {exc}")
            if args in (["--help"], ["-h"]):
                try:
                    console.print(restic.command_help(command))
                except BackupError as exc:
                    fail(str(exc))
                continue
            run_args(None, [command, *args], interactive=True)
            return


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
            fail(f"backup job '{backup_id}' not found in {config.config_path()}")
        return backup_id
    if not sys.stdin.isatty():
        fail("job ID is required when stdin is not interactive")
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
    choices.append(questionary.Separator(" "))
    selected = questionary.select("Backup job:", choices=choices).ask()
    if selected is None:
        raise typer.Abort()
    return str(selected)


def show_repositories(stores: dict[str, dict[str, Any]]) -> None:
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


def show_backups(
    stores: dict[str, dict[str, Any]], backups: dict[str, dict[str, Any]]
) -> None:
    backup_table = Table(title="Backups", box=box.ROUNDED)
    backup_table.add_column("Job ID", style="cyan", no_wrap=True)
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


@repository_app.command("list")
def repository_list_command() -> None:
    """Show configured restic repositories."""
    _, _, stores, _ = validated()
    show_repositories(stores)


@backup_app.command("list")
def backup_list_command() -> None:
    """Show configured backup jobs."""
    _, _, stores, backups = validated()
    show_backups(stores, backups)


@app.command("list", hidden=True)
def list_command() -> None:
    """Show configured repositories and backups."""
    _, _, stores, backups = validated()
    show_repositories(stores)
    show_backups(stores, backups)


@backup_app.command("run")
@app.command("backup", hidden=True)
def backup_command(
    backup: Annotated[
        str | None,
        typer.Argument(help="Job ID; prompts when omitted.", metavar="JOB_ID"),
    ] = None,
) -> None:
    """Create a snapshot from a backup job's configured paths."""
    _, credentials, stores, backups = validated()
    backup_id = choose_backup(backup, stores, backups)
    paths = backups[backup_id].get("paths")
    if not paths:
        fail(f"backup job '{backup_id}' has no configured paths")
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


@backup_app.command("data-dir")
@app.command("data-dir", hidden=True)
def data_dir_command(
    backup: str | None = typer.Argument(
        None, help="Job ID; prompts when omitted.", metavar="JOB_ID"
    ),
) -> None:
    """Print the managed local data directory for a backup."""
    _, credentials, stores, backups = validated()
    backup_id = choose_backup(backup, stores, backups)
    try:
        store, _ = repository.resolve(backup_id, credentials, stores, backups)
        typer.echo(repository.data_dir(backup_id, store))
    except BackupError as exc:
        fail(str(exc))


@repository_app.command("init")
@app.command("init", hidden=True)
def init_command(
    repository_id: Annotated[
        str | None,
        typer.Argument(help="Repository ID; prompts when omitted."),
    ] = None,
    all_repositories: Annotated[
        bool,
        typer.Option("--all", help="Initialize every enabled repository."),
    ] = False,
) -> None:
    """Initialize one repository, or every enabled repository with --all."""
    _, credentials, stores, _ = validated()
    if repository_id is not None and all_repositories:
        fail("repository ID and --all cannot be used together")
    if repository_id is None and not all_repositories:
        if not sys.stdin.isatty():
            fail("repository ID or --all is required when stdin is not interactive")
        selected = questionary.select(
            "Repository to initialize:",
            choices=[
                questionary.Choice("All repositories", ALL_REPOSITORIES),
                *[
                    questionary.Choice(
                        f"{store_id}{'' if store['enabled'] else ' (disabled)'}",
                        store_id,
                    )
                    for store_id, store in stores.items()
                ],
                questionary.Separator(" "),
            ],
        ).ask()
        if selected is None:
            raise typer.Abort()
        if selected == ALL_REPOSITORIES:
            all_repositories = True
        else:
            repository_id = str(selected)

    if all_repositories:
        selected_stores = list(stores.items())
    else:
        store = stores.get(str(repository_id))
        if store is None:
            fail(f"repository '{repository_id}' not found in {config.config_path()}")
        selected_stores = [(str(repository_id), store)]

    for store_id, store in selected_stores:
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


@repository_app.command("prime-cache")
@app.command("prime-cache", hidden=True)
def prime_cache_command(
    repository_id: Annotated[
        str | None,
        typer.Argument(help="Repository ID; prompts when omitted."),
    ] = None,
) -> None:
    """Download and validate repository metadata into its local cache."""
    _, credentials, stores, _ = validated()
    if repository_id is None:
        if not sys.stdin.isatty():
            fail("repository ID is required when stdin is not interactive")
        selected = questionary.select(
            "Repository cache to prime:",
            choices=[
                questionary.Choice(store_id, store_id)
                for store_id, store in stores.items()
                if store["enabled"]
            ]
            + [questionary.Separator(" ")],
        ).ask()
        if selected is None:
            raise typer.Abort()
        repository_id = str(selected)
    store = stores.get(repository_id)
    if store is None:
        fail(f"repository '{repository_id}' not found in {config.config_path()}")
    if not store["enabled"]:
        fail(f"restic store '{repository_id}' is disabled")

    error_console.print(Text(f"{repository_id}: priming local cache", style="cyan"))
    try:
        code = restic.store_command(
            store,
            credentials[store["credentials-id"]],
            ["check", "--with-cache"],
        )
    except BackupError as exc:
        fail(str(exc))
    if code:
        raise typer.Exit(code)
    error_console.print(Text(f"{repository_id}: cache primed", style="bold green"))


def load_snapshots(
    backup_id: str,
    credentials: dict[str, dict[str, Any]],
    stores: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    tag = str(backups[backup_id].get("tag", backup_id))
    try:
        loaded = json.loads(
            restic.command_output(
                backup_id,
                ["snapshots", "--tag", tag, "--json"],
                credentials,
                stores,
                backups,
            )
        )
    except (BackupError, json.JSONDecodeError) as exc:
        fail(str(exc))
    if not isinstance(loaded, list) or any(
        not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str)
        for snapshot in loaded
    ):
        fail("restic snapshots returned invalid JSON")
    return tag, loaded


@snapshot_app.command("list")
@app.command("snapshots", hidden=True)
def snapshots_command(
    backup: Annotated[
        str | None,
        typer.Argument(help="Job ID; prompts when omitted.", metavar="JOB_ID"),
    ] = None,
) -> None:
    """List snapshots for a configured backup."""
    _, credentials, stores, backups = validated()
    backup_id = choose_backup(backup, stores, backups)
    tag, snapshots = load_snapshots(backup_id, credentials, stores, backups)
    if not snapshots:
        error_console.print(
            Text(f"{backup_id}: no snapshots tagged '{tag}'", style="yellow")
        )
        return

    table = Table(title=f"Snapshots: {backup_id}", box=box.ROUNDED)
    table.add_column("Snapshot", style="cyan", no_wrap=True)
    table.add_column("Time", no_wrap=True)
    table.add_column("Host")
    table.add_column("Paths")
    table.add_column("Tags")
    for snapshot in snapshots:
        snapshot_id = snapshot["id"]
        paths = snapshot.get("paths", [])
        tags = snapshot.get("tags", [])
        if not isinstance(paths, list) or not isinstance(tags, list):
            fail("restic snapshots returned invalid JSON")
        table.add_row(
            str(snapshot.get("short_id", snapshot_id[:8])),
            str(snapshot.get("time", "unknown")).replace("T", " ")[:19],
            str(snapshot.get("hostname", "—")),
            "\n".join(str(path) for path in paths) or "—",
            ", ".join(str(tag) for tag in tags) or "—",
        )
    console.print(table)


@snapshot_app.command("forget")
@app.command("forget", hidden=True)
def forget_command(
    backup: Annotated[
        str | None,
        typer.Argument(help="Job ID; prompts when omitted.", metavar="JOB_ID"),
    ] = None,
) -> None:
    """Forget one snapshot and prune its unreferenced data."""
    if not sys.stdin.isatty():
        fail("forget requires an interactive terminal")
    _, credentials, stores, backups = validated()
    backup_id = choose_backup(backup, stores, backups)
    tag, snapshots = load_snapshots(backup_id, credentials, stores, backups)
    if not snapshots:
        error_console.print(
            Text(f"{backup_id}: no snapshots tagged '{tag}'", style="yellow")
        )
        return

    choices: list[questionary.Choice] = []
    snapshot_by_id: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
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

    choices.append(questionary.Separator(" "))
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


@repository_app.command("destroy")
@app.command("destroy", hidden=True)
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
            ]
            + [questionary.Separator(" ")],
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


@restic_app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
@app.command(
    "run",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run_command(
    context: typer.Context,
    backup: Annotated[
        str | None,
        typer.Option(
            "--backup", "-b", help="Job ID; prompts when omitted.", metavar="JOB_ID"
        ),
    ] = None,
) -> None:
    """Run restic with all trailing arguments passed through unchanged."""
    run_args(backup, list(context.args))


def copyable_command(backup_id: str, args: list[str]) -> str:
    command = [
        "uv",
        "run",
        "restic-backups",
        "--config",
        str(config.config_path().resolve()),
    ]
    if os.environ.get(sops.SOPS_ENV) == "1":
        command.append("--sops")
    command.extend(["generic", "restic", "run", "--backup", backup_id, *args])
    return shlex.join(command)


def run_args(backup: str | None, args: list[str], *, interactive: bool = False) -> None:
    _, credentials, stores, backups = validated()
    backup_id = choose_backup(backup, stores, backups)
    if not args:
        fail("a restic command is required after 'run'")
    if interactive:
        console.print(Text("Command:", style="bold"))
        console.print(Text(copyable_command(backup_id, args), style="cyan"))
        action = questionary.select(
            "Action:",
            choices=[
                questionary.Choice("Run", "run"),
                questionary.Choice("Print only", "print"),
                questionary.Choice("Cancel", "cancel"),
                questionary.Separator(" "),
            ],
        ).ask()
        if action != "run":
            if action == "cancel":
                error_console.print(Text("Cancelled; nothing was run.", style="yellow"))
            return
    try:
        raise typer.Exit(restic.command(backup_id, args, credentials, stores, backups))
    except BackupError as exc:
        fail(str(exc))

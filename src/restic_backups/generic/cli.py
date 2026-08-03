"""CLI for generic configured restic repositories."""

from __future__ import annotations

import json
import os
import shlex
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
from . import local, repository, restic, s3, sops
from .tui import checkbox, select
from .tui import menu_choice as tui_menu_choice

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
    return tui_menu_choice(label, description, value, width)


def choose_dry_run() -> bool:
    selected = checkbox(
        "Options:",
        choices=[
            menu_choice("Dry run", "Show what would happen without writing", "dry-run"),
            questionary.Separator(" "),
        ],
    ).unsafe_ask()
    return selected is not None and "dry-run" in selected


@app.callback()
def menu(context: typer.Context) -> None:
    """Choose a generic backup operation when run interactively."""
    if context.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty():
        typer.echo(context.get_help())
        return
    try:
        interactive_menu()
    except KeyboardInterrupt:
        return


def interactive_menu() -> None:
    """Navigate generic operations with arrow-key menus."""
    while True:
        selected = select(
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
        ).unsafe_ask()
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
        selected = select(
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
                    "Prune / compact",
                    "Remove unused data or repack repository files",
                    "prune",
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
        ).unsafe_ask()
        if selected in {None, "back"}:
            return
        try:
            if selected == "help":
                print_typer_help(repository_app, "restic-backups generic repository")
            elif selected == "list":
                repository_list_command()
                return
            elif selected == "init":
                init_command(dry_run=choose_dry_run())
                return
            elif selected == "prime-cache":
                prime_cache_command(None)
                return
            elif selected == "prune":
                prune_menu()
                return
            elif selected == "destroy":
                destroy_command(None, choose_dry_run())
                return
        except typer.Abort:
            continue


def prune_menu() -> None:
    """Choose native restic prune behavior with described arrow-key options."""
    while True:
        selected = select(
            "Repository maintenance:",
            choices=[
                menu_choice(
                    "Standard prune",
                    "Remove unused data with restic defaults",
                    "standard",
                    22,
                ),
                menu_choice(
                    "Minimize bandwidth",
                    "Keep partly used data packs",
                    "bandwidth",
                    22,
                ),
                menu_choice(
                    "Metadata only",
                    "Repack only cacheable metadata",
                    "metadata",
                    22,
                ),
                menu_choice(
                    "Compact small packs",
                    "Combine packs below a size threshold",
                    "small-packs",
                    22,
                ),
                menu_choice(
                    "Limit repacking",
                    "Cap data rewritten in this run",
                    "limit",
                    22,
                ),
                menu_choice("Help", "Show restic prune flags", "help", 22),
                menu_choice("Back", "Return to repository commands", "back", 22),
                questionary.Separator(" "),
            ],
        ).unsafe_ask()
        if selected in {None, "back"}:
            return
        if selected == "help":
            try:
                console.print(restic.command_help("prune"))
            except BackupError as exc:
                fail(str(exc))
            continue

        options: dict[str, Any] = {}
        if selected == "bandwidth":
            options["max_unused"] = "unlimited"
        elif selected == "metadata":
            options["repack_cacheable_only"] = True
        elif selected in {"small-packs", "limit"}:
            label = (
                "Repack packs smaller than (for example 20M):"
                if selected == "small-packs"
                else "Maximum data to repack (for example 1G):"
            )
            size = questionary.text(label).unsafe_ask()
            if not size or not size.strip():
                error_console.print(Text("A size is required.", style="yellow"))
                continue
            key = (
                "repack_smaller_than"
                if selected == "small-packs"
                else "max_repack_size"
            )
            options[key] = size.strip()
        prune_command(None, dry_run=choose_dry_run(), **options)
        return


def backup_menu() -> None:
    while True:
        selected = select(
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
        ).unsafe_ask()
        if selected in {None, "back"}:
            return
        try:
            if selected == "help":
                print_typer_help(backup_app, "restic-backups generic backup")
            elif selected == "list":
                backup_list_command()
                return
            elif selected == "run":
                backup_command(None, choose_dry_run())
                return
            elif selected == "data-dir":
                data_dir_command(None)
                return
        except typer.Abort:
            continue


def snapshot_menu() -> None:
    while True:
        selected = select(
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
        ).unsafe_ask()
        if selected in {None, "back"}:
            return
        try:
            if selected == "help":
                print_typer_help(snapshot_app, "restic-backups generic snapshot")
            elif selected == "list":
                snapshots_command(None)
                return
            elif selected == "forget":
                forget_command(None, choose_dry_run())
                return
        except typer.Abort:
            continue


def restic_menu(backup_id: str | None = None) -> None:
    try:
        commands = restic.available_commands()
    except BackupError as exc:
        fail(str(exc))
    while True:
        selected = select(
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
        ).unsafe_ask()
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
            action = select(
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
            ).unsafe_ask()
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
            ).unsafe_ask()
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
            if (
                "--dry-run" not in args
                and restic.supports_dry_run(command)
                and choose_dry_run()
            ):
                args.insert(0, "--dry-run")
            try:
                run_args(backup_id, [command, *args], interactive=True)
                return
            except typer.Abort:
                continue


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


def audit_command(*args: str) -> None:
    try:
        audit.record("restic-backups", ["generic", *args])
    except BackupError as exc:
        fail(str(exc))


def choose_backup(
    backup_id: str | None,
    repositories: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
    *,
    include_github: bool = True,
) -> str:
    if backup_id is not None:
        if backup_id not in backups:
            fail(f"backup job '{backup_id}' not found in {config.config_path()}")
        if not include_github and backups[backup_id]["type"] in {
            "github-owner",
            "github-repository",
        }:
            fail(f"backup job '{backup_id}' must use the github-repository workflow")
        return backup_id
    if not sys.stdin.isatty():
        fail("job ID is required when stdin is not interactive")
    choices = [
        questionary.Choice(
            f"{item_id}  ({', '.join(config.backup_repository_ids(item, item_id))})",
            value=item_id,
        )
        for item_id, item in backups.items()
        if include_github or item["type"] not in {"github-owner", "github-repository"}
        if any(
            config.repository_is_enabled(repositories[value])
            for value in config.backup_repository_ids(item, item_id)
        )
    ]
    if not choices:
        fail("no enabled backups are available")
    choices.append(questionary.Separator(" "))
    selected = select("Backup job:", choices=choices).unsafe_ask()
    if selected is None:
        raise typer.Abort()
    return str(selected)


def choose_repositories(
    backup_id: str,
    requested: list[str] | None,
    repositories: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
) -> list[str]:
    configured = config.backup_repository_ids(backups[backup_id], backup_id)
    if requested:
        selected = requested
    elif not sys.stdin.isatty():
        fail("at least one --repository is required when stdin is not interactive")
    else:
        enabled = [
            value
            for value in configured
            if config.repository_is_enabled(repositories[value])
        ]
        if not enabled:
            fail(f"backup job '{backup_id}' has no available repositories")
        selected = checkbox(
            "Repositories:",
            choices=[
                questionary.Choice(
                    f"{repository_id}{f' ({reason})' if (reason := config.repository_disabled_reason(repositories[repository_id])) else ''}",
                    repository_id,
                    disabled=reason,
                    checked=len(enabled) == 1 and repository_id == enabled[0],
                )
                for repository_id in configured
            ]
            + [questionary.Separator(" ")],
        ).unsafe_ask()
        if selected is None:
            raise typer.Abort()
    if not selected:
        fail("select at least one repository")
    if len(selected) != len(set(selected)):
        fail("repository selections must be unique")
    for repository_id in selected:
        if repository_id not in configured:
            fail(
                f"repository '{repository_id}' is not configured for backup job '{backup_id}'"
            )
        if not repositories[repository_id]["enabled"]:
            fail(f"restic repository '{repository_id}' is disabled")
        if not repositories[repository_id].get("_storage-enabled", True):
            fail(f"storage '{repositories[repository_id]['storage-id']}' is disabled")
    return selected


def choose_repository(
    backup_id: str,
    requested: str | None,
    repositories: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
) -> str:
    configured = config.backup_repository_ids(backups[backup_id], backup_id)
    if requested is not None:
        return choose_repositories(backup_id, [requested], repositories, backups)[0]
    enabled = [
        value
        for value in configured
        if config.repository_is_enabled(repositories[value])
    ]
    if not enabled:
        fail(f"backup job '{backup_id}' has no available repositories")
    if len(enabled) == 1:
        return enabled[0]
    if not sys.stdin.isatty():
        fail("--repository is required when stdin is not interactive")
    selected = select(
        "Repository:",
        choices=[questionary.Choice(value, value) for value in enabled]
        + [questionary.Separator(" ")],
    ).unsafe_ask()
    if selected is None:
        raise typer.Abort()
    return str(selected)


def show_repositories(
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
) -> None:
    store_table = Table(title="Repositories", box=box.ROUNDED)
    store_table.add_column("ID", style="cyan", no_wrap=True)
    store_table.add_column("Description")
    store_table.add_column("Storage")
    store_table.add_column("Type")
    store_table.add_column("Location")
    store_table.add_column("State")
    for repository_id, restic_repository in repositories.items():
        backend = storage[restic_repository["storage-id"]]
        reason = config.repository_disabled_reason(restic_repository)
        state = "enabled" if reason is None else reason
        store_table.add_row(
            Text(repository_id),
            Text(str(restic_repository.get("description", "—"))),
            Text(str(backend["id"])),
            Text(str(backend["type"])),
            Text(repository.location(restic_repository, backend)),
            Text(state, style="green" if reason is None else "yellow"),
        )
    console.print(store_table)


def show_backups(
    repositories: dict[str, dict[str, Any]], backups: dict[str, dict[str, Any]]
) -> None:
    backup_table = Table(title="Backups", box=box.ROUNDED)
    backup_table.add_column("Job ID", style="cyan", no_wrap=True)
    backup_table.add_column("Type")
    backup_table.add_column("Description")
    backup_table.add_column("Repositories")
    backup_table.add_column("Paths")
    backup_table.add_column("Tag")
    backup_table.add_column("State")
    for backup_id, backup in backups.items():
        repository_ids = config.backup_repository_ids(backup, backup_id)
        enabled = sum(
            config.repository_is_enabled(repositories[value])
            for value in repository_ids
        )
        state = (
            "enabled"
            if enabled == len(repository_ids)
            else "disabled"
            if enabled == 0
            else "partially enabled"
        )
        backup_table.add_row(
            Text(backup_id),
            Text(str(backup["type"])),
            Text(str(backup.get("description", "—"))),
            Text("\n".join(repository_ids)),
            Text("\n".join(backup["source"].get("paths", [])) or "—"),
            Text(str(backup.get("tag", backup_id))),
            Text(state, style="green" if enabled == len(repository_ids) else "yellow"),
        )
    console.print(backup_table)


@repository_app.command("list")
def repository_list_command() -> None:
    """Show configured restic repositories."""
    audit_command("repository", "list")
    _, storage, repositories, _ = validated()
    show_repositories(storage, repositories)


@backup_app.command("list")
def backup_list_command() -> None:
    """Show configured backup jobs."""
    audit_command("backup", "list")
    _, _, repositories, backups = validated()
    show_backups(repositories, backups)


@app.command("list", hidden=True)
def list_command() -> None:
    """Show configured repositories and backups."""
    audit_command("list")
    _, storage, repositories, backups = validated()
    show_repositories(storage, repositories)
    show_backups(repositories, backups)


@backup_app.command("run")
@app.command("backup", hidden=True)
def backup_command(
    backup: Annotated[
        str | None,
        typer.Argument(help="Job ID; prompts when omitted.", metavar="JOB_ID"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would happen without writing."),
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

    run_command(backup, dry_run, repository_ids)


@backup_app.command("data-dir")
@app.command("data-dir", hidden=True)
def data_dir_command(
    backup: str | None = typer.Argument(
        None, help="Job ID; prompts when omitted.", metavar="JOB_ID"
    ),
    repository_id: Annotated[
        str | None,
        typer.Option("--repository", "-r", help="Repository ID."),
    ] = None,
) -> None:
    """Print the managed local data directory for a backup."""
    _, storage, repositories, backups = validated()
    backup_id = choose_backup(backup, repositories, backups, include_github=False)
    repository_id = choose_repository(backup_id, repository_id, repositories, backups)
    audit_command("backup", "data-dir", backup_id, "--repository", repository_id)
    try:
        restic_repository, backend = repository.resolve(
            backup_id, storage, repositories, backups, repository_id
        )
        typer.echo(repository.data_dir(backup_id, restic_repository, backend))
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
        typer.Option("--all", help="Initialize every available repository."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Check without initializing repositories."),
    ] = False,
) -> None:
    """Initialize one repository, or every available repository with --all."""
    _, storage, repositories, _ = validated()
    if repository_id is not None and all_repositories:
        fail("repository ID and --all cannot be used together")
    if repository_id is None and not all_repositories:
        if not sys.stdin.isatty():
            fail("repository ID or --all is required when stdin is not interactive")
        selected = select(
            "Repository to initialize:",
            choices=[
                questionary.Choice("All repositories", ALL_REPOSITORIES),
                *[
                    questionary.Choice(
                        f"{repository_id}{f' ({reason})' if (reason := config.repository_disabled_reason(item)) else ''}",
                        repository_id,
                        disabled=reason,
                    )
                    for repository_id, item in repositories.items()
                ],
                questionary.Separator(" "),
            ],
        ).unsafe_ask()
        if selected is None:
            raise typer.Abort()
        if selected == ALL_REPOSITORIES:
            all_repositories = True
        else:
            repository_id = str(selected)

    if all_repositories:
        selected_repositories = list(repositories.items())
        audit_command(
            "repository", "init", "--all", *(["--dry-run"] if dry_run else [])
        )
    else:
        restic_repository = repositories.get(str(repository_id))
        if restic_repository is None:
            fail(f"repository '{repository_id}' not found in {config.config_path()}")
        selected_repositories = [(str(repository_id), restic_repository)]
        audit_command(
            "repository",
            "init",
            str(repository_id),
            *(["--dry-run"] if dry_run else []),
        )

    for repository_id, restic_repository in selected_repositories:
        reason = config.repository_disabled_reason(restic_repository)
        if reason is not None:
            error_console.print(
                Text(f"{repository_id}: {reason}; skipping", style="yellow")
            )
            continue
        backend = storage[restic_repository["storage-id"]]
        error_console.print(Text(f"{repository_id}: checking repository", style="cyan"))
        try:
            code = restic.repository_command(
                restic_repository,
                backend,
                ["cat", "config"],
                quiet=True,
            )
            if code == 0:
                error_console.print(
                    Text(
                        f"{repository_id}: already initialized; skipping",
                        style="yellow",
                    )
                )
                continue
            if code != 10:
                raise typer.Exit(code)
            if dry_run:
                error_console.print(
                    Text(
                        f"{repository_id}: dry run; would initialize repository",
                        style="green",
                    )
                )
                continue
            error_console.print(
                Text(f"{repository_id}: not initialized; initializing", style="cyan")
            )
            code = restic.repository_command(restic_repository, backend, ["init"])
        except BackupError as exc:
            fail(str(exc))
        if code:
            raise typer.Exit(code)
        error_console.print(Text(f"{repository_id}: initialized", style="green"))


@repository_app.command("prime-cache")
@app.command("prime-cache", hidden=True)
def prime_cache_command(
    repository_id: Annotated[
        str | None,
        typer.Argument(help="Repository ID; prompts when omitted."),
    ] = None,
) -> None:
    """Download and validate repository metadata into its local cache."""
    _, storage, repositories, _ = validated()
    if repository_id is None:
        if not sys.stdin.isatty():
            fail("repository ID is required when stdin is not interactive")
        selected = select(
            "Repository cache to prime:",
            choices=[
                questionary.Choice(
                    f"{repository_id}{f' ({reason})' if (reason := config.repository_disabled_reason(item)) else ''}",
                    repository_id,
                    disabled=reason,
                )
                for repository_id, item in repositories.items()
            ]
            + [questionary.Separator(" ")],
        ).unsafe_ask()
        if selected is None:
            raise typer.Abort()
        repository_id = str(selected)
    restic_repository = repositories.get(repository_id)
    if restic_repository is None:
        fail(f"repository '{repository_id}' not found in {config.config_path()}")
    if not restic_repository["enabled"]:
        fail(f"restic repository '{repository_id}' is disabled")
    if not restic_repository.get("_storage-enabled", True):
        fail(f"storage '{restic_repository['storage-id']}' is disabled")

    audit_command("repository", "prime-cache", repository_id)
    error_console.print(Text(f"{repository_id}: priming local cache", style="cyan"))
    try:
        code = restic.repository_command(
            restic_repository,
            storage[restic_repository["storage-id"]],
            ["check", "--with-cache"],
        )
    except BackupError as exc:
        fail(str(exc))
    if code:
        raise typer.Exit(code)
    error_console.print(Text(f"{repository_id}: cache primed", style="bold green"))


@repository_app.command("prune")
def prune_command(
    repository_id: Annotated[
        str | None,
        typer.Argument(help="Repository ID; prompts when omitted."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what prune would do without writing."),
    ] = False,
    max_unused: Annotated[
        str | None,
        typer.Option("--max-unused", help="Restic's allowed unused-data limit."),
    ] = None,
    max_repack_size: Annotated[
        str | None,
        typer.Option("--max-repack-size", help="Limit data repacked in this run."),
    ] = None,
    repack_cacheable_only: Annotated[
        bool,
        typer.Option("--repack-cacheable-only", help="Repack only cacheable metadata."),
    ] = False,
    repack_smaller_than: Annotated[
        str | None,
        typer.Option(
            "--repack-smaller-than", help="Repack pack files below this size."
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Run prune without interactive confirmation."),
    ] = False,
) -> None:
    """Remove unused data and optionally repack repository files."""
    _, storage, repositories, _ = validated()
    if repository_id is None:
        if not sys.stdin.isatty():
            fail("repository ID is required when stdin is not interactive")
        selected = select(
            "Repository to prune:",
            choices=[
                questionary.Choice(
                    f"{item_id}  ({repository.location(item, storage[item['storage-id']])}){f' ({reason})' if (reason := config.repository_disabled_reason(item)) else ''}",
                    item_id,
                    disabled=reason,
                )
                for item_id, item in repositories.items()
            ]
            + [questionary.Separator(" ")],
        ).unsafe_ask()
        if selected is None:
            raise typer.Abort()
        repository_id = str(selected)

    restic_repository = repositories.get(repository_id)
    if restic_repository is None:
        fail(f"repository '{repository_id}' not found in {config.config_path()}")
    if not restic_repository["enabled"]:
        fail(f"restic repository '{repository_id}' is disabled")
    if not restic_repository.get("_storage-enabled", True):
        fail(f"storage '{restic_repository['storage-id']}' is disabled")
    sizes = (max_unused, max_repack_size, repack_smaller_than)
    if any(value is not None and not value.strip() for value in sizes):
        fail("prune size and limit values cannot be empty")

    args = ["prune"]
    for flag, value in (
        ("--max-unused", max_unused),
        ("--max-repack-size", max_repack_size),
        ("--repack-smaller-than", repack_smaller_than),
    ):
        if value is not None:
            args.extend((flag, value))
    if repack_cacheable_only:
        args.append("--repack-cacheable-only")
    if dry_run:
        args.append("--dry-run")

    if not dry_run and not yes:
        if not sys.stdin.isatty():
            fail("prune requires --yes when stdin is not interactive")
        confirmed = questionary.confirm(
            f"Prune '{repository_id}'? Restic may download, re-upload, and delete repository pack files.",
            default=False,
        ).unsafe_ask()
        if confirmed is not True:
            error_console.print(
                Text("Cancelled; repository unchanged.", style="yellow")
            )
            return

    audit_command("repository", "prune", repository_id, *args[1:])
    mode = "previewing prune" if dry_run else "pruning repository"
    error_console.print(Text(f"{repository_id}: {mode}", style="cyan"))
    try:
        code = restic.repository_command(
            restic_repository,
            storage[restic_repository["storage-id"]],
            args,
        )
    except BackupError as exc:
        fail(str(exc))
    if code:
        raise typer.Exit(code)
    message = "dry run complete; repository unchanged" if dry_run else "prune complete"
    error_console.print(Text(f"{repository_id}: {message}", style="bold green"))


def load_snapshots(
    backup_id: str,
    repository_id: str,
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    tag = str(backups[backup_id].get("tag", backup_id))
    try:
        loaded = json.loads(
            restic.command_output(
                backup_id,
                ["snapshots", "--tag", tag, "--json"],
                storage,
                repositories,
                backups,
                repository_id,
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


def choose_snapshot(snapshots: list[dict[str, Any]], prompt: str) -> str:
    choices: list[questionary.Choice | questionary.Separator] = []
    for snapshot in snapshots:
        snapshot_id = snapshot["id"]
        timestamp = str(snapshot.get("time", "unknown")).replace("T", " ")[:19]
        short_id = str(snapshot.get("short_id", snapshot_id[:8]))
        hostname = str(snapshot.get("hostname", "unknown host"))
        paths = snapshot.get("paths", [])
        if not isinstance(paths, list):
            fail("restic snapshots returned invalid JSON")
        choices.append(
            questionary.Choice(
                f"{timestamp}  {short_id}  {hostname}  {', '.join(map(str, paths))}",
                snapshot_id,
            )
        )
    choices.append(questionary.Separator(" "))
    selected = select(prompt, choices).unsafe_ask()
    if selected is None:
        raise typer.Abort()
    return str(selected)


@snapshot_app.command("list")
@app.command("snapshots", hidden=True)
def snapshots_command(
    backup: Annotated[
        str | None,
        typer.Argument(help="Job ID; prompts when omitted.", metavar="JOB_ID"),
    ] = None,
    repository_id: Annotated[
        str | None,
        typer.Option("--repository", "-r", help="Repository ID."),
    ] = None,
) -> None:
    """List snapshots for a configured backup."""
    _, storage, repositories, backups = validated()
    backup_id = choose_backup(backup, repositories, backups)
    repository_id = choose_repository(backup_id, repository_id, repositories, backups)
    audit_command("snapshot", "list", backup_id, "--repository", repository_id)
    tag, snapshots = load_snapshots(
        backup_id, repository_id, storage, repositories, backups
    )
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
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Show what would be forgotten without deleting."
        ),
    ] = False,
    repository_id: Annotated[
        str | None,
        typer.Option("--repository", "-r", help="Repository ID."),
    ] = None,
) -> None:
    """Forget one snapshot and prune its unreferenced data."""
    if not sys.stdin.isatty():
        fail("forget requires an interactive terminal")
    _, storage, repositories, backups = validated()
    backup_id = choose_backup(backup, repositories, backups)
    repository_id = choose_repository(backup_id, repository_id, repositories, backups)
    tag, snapshots = load_snapshots(
        backup_id, repository_id, storage, repositories, backups
    )
    if not snapshots:
        error_console.print(
            Text(f"{backup_id}: no snapshots tagged '{tag}'", style="yellow")
        )
        return

    snapshot_by_id: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        snapshot_id = snapshot["id"]
        snapshot_by_id[snapshot_id] = snapshot
    snapshot_id = choose_snapshot(snapshots, "Snapshot to forget:")
    short_id = str(snapshot_by_id[snapshot_id].get("short_id", snapshot_id[:8]))
    if not dry_run:
        confirmed = questionary.confirm(
            f"Forget snapshot '{short_id}' and prune its unreferenced data?",
            default=False,
        ).unsafe_ask()
        if confirmed is not True:
            error_console.print(
                Text("Cancelled; nothing was forgotten.", style="yellow")
            )
            return
    args = ["forget", snapshot_id, "--prune", *(["--dry-run"] if dry_run else [])]
    audit_command(
        "snapshot",
        "forget",
        backup_id,
        "--repository",
        repository_id,
        snapshot_id,
        *(["--dry-run"] if dry_run else []),
    )
    try:
        code = restic.command(
            backup_id,
            args,
            storage,
            repositories,
            backups,
            repository_id=repository_id,
        )
    except BackupError as exc:
        fail(str(exc))
    if code:
        raise typer.Exit(code)
    message = (
        f"dry run complete; snapshot {short_id} not forgotten"
        if dry_run
        else f"forgot snapshot {short_id}"
    )
    error_console.print(Text(f"{backup_id}: {message}", style="bold green"))


@repository_app.command("destroy")
@app.command("destroy", hidden=True)
def destroy_command(
    repository_id: Annotated[
        str | None,
        typer.Argument(help="Repository ID; prompts when omitted."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show the target without deleting objects."),
    ] = False,
) -> None:
    """Permanently erase a configured restic repository."""
    if not sys.stdin.isatty():
        fail("destroy requires an interactive terminal")
    _, storage, repositories, _ = validated()
    if repository_id is None:
        selected = select(
            "Repository to permanently destroy:",
            choices=[
                questionary.Choice(
                    f"{repository_id}  ({repository.location(item, storage[item['storage-id']])})",
                    repository_id,
                    disabled=(
                        None
                        if storage[item["storage-id"]].get("enabled", True)
                        else "storage disabled"
                    ),
                )
                for repository_id, item in repositories.items()
            ]
            + [questionary.Separator(" ")],
        ).unsafe_ask()
        if selected is None:
            raise typer.Abort()
        repository_id = str(selected)
    restic_repository = repositories.get(repository_id)
    if restic_repository is None:
        fail(f"repository '{repository_id}' not found in {config.config_path()}")
    backend = storage[restic_repository["storage-id"]]
    if not backend.get("enabled", True):
        fail(f"storage '{backend['id']}' is disabled")
    target = repository.location(restic_repository, backend)
    if dry_run:
        audit_command("repository", "destroy", repository_id, "--dry-run")
        error_console.print(
            Text(
                f"{repository_id}: dry run complete; nothing deleted at {target}",
                style="green",
            )
        )
        return
    try:
        config.ensure_repository_ready(restic_repository, backend)
    except config.ConfigError as exc:
        fail(str(exc))
    confirmed = questionary.confirm(
        f"Permanently destroy '{repository_id}' and all data at {target}?",
        default=False,
    ).unsafe_ask()
    if confirmed is not True:
        error_console.print(Text("Cancelled; nothing was destroyed.", style="yellow"))
        return
    typed = questionary.text(
        f"Type '{repository_id}' to confirm permanent destruction:"
    ).unsafe_ask()
    if typed != repository_id:
        fail("repository ID did not match; nothing was destroyed")

    audit_command("repository", "destroy", repository_id)
    delete_repository = (
        local.delete_repository if backend["type"] == "local" else s3.delete_repository
    )
    deleted = delete_repository(restic_repository, backend)
    error_console.print(
        Text(
            f"{repository_id}: permanently destroyed {deleted} entries",
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
    repository_id: Annotated[
        str | None,
        typer.Option("--repository", "-r", help="Repository ID."),
    ] = None,
) -> None:
    """Run restic with all trailing arguments passed through unchanged."""
    run_args(backup, list(context.args), repository_id=repository_id)


def copyable_command(backup_id: str, repository_id: str, args: list[str]) -> str:
    command = [
        "uv",
        "run",
        "restic-backups",
        "--config",
        str(config.config_path().resolve()),
    ]
    if os.environ.get(sops.SOPS_ENV) == "1":
        command.append("--sops")
    command.extend(
        [
            "generic",
            "restic",
            "run",
            "--backup",
            backup_id,
            "--repository",
            repository_id,
            *args,
        ]
    )
    return shlex.join(command)


def run_args(
    backup: str | None,
    args: list[str],
    *,
    interactive: bool = False,
    repository_id: str | None = None,
) -> None:
    _, storage, repositories, backups = validated()
    backup_id = choose_backup(backup, repositories, backups)
    repository_id = choose_repository(backup_id, repository_id, repositories, backups)
    if not args:
        fail("a restic command is required after 'run'")
    if interactive and args == ["ls"]:
        tag, snapshots = load_snapshots(
            backup_id, repository_id, storage, repositories, backups
        )
        if not snapshots:
            fail(f"{backup_id}: no snapshots tagged '{tag}'")
        args.append(choose_snapshot(snapshots, "Snapshot to inspect:"))
    if interactive:
        console.print(Text("Command:", style="bold"))
        console.print(
            Text(copyable_command(backup_id, repository_id, args), style="cyan")
        )
        action = select(
            "Action:",
            choices=[
                questionary.Choice("Run", "run"),
                questionary.Choice("Print only", "print"),
                questionary.Choice("Cancel", "cancel"),
                questionary.Separator(" "),
            ],
        ).unsafe_ask()
        if action is None:
            raise typer.Abort()
        if action != "run":
            if action == "cancel":
                error_console.print(Text("Cancelled; nothing was run.", style="yellow"))
            return
    audit_command(
        "restic",
        "run",
        "--backup",
        backup_id,
        "--repository",
        repository_id,
        *args,
    )
    try:
        raise typer.Exit(
            restic.command(
                backup_id,
                args,
                storage,
                repositories,
                backups,
                repository_id=repository_id,
            )
        )
    except BackupError as exc:
        fail(str(exc))

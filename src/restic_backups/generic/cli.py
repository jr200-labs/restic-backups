"""CLI for generic configured restic repositories."""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Callable
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
from .tui import checkbox, group_disabled_choices, select
from .tui import menu_choice as tui_menu_choice

app = typer.Typer(
    help="Generic configured restic repository commands.",
)
repository_app = typer.Typer(help="Manage configured restic repositories.")
snapshot_app = typer.Typer(help="List and forget restic snapshots.")
restic_app = typer.Typer(help="Run advanced restic commands.")
app.add_typer(repository_app, name="repository")
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(restic_app, name="restic")
console = Console()
error_console = Console(stderr=True)
ALL_REPOSITORIES = "__all_repositories__"


def repository_disabled_reason(item: dict[str, Any]) -> str | None:
    return config.repository_disabled_reason(item) or (
        "not initialized" if item.get("_initialized") is False else None
    )


def repository_is_available(item: dict[str, Any]) -> bool:
    return repository_disabled_reason(item) is None


def probe_repository_initialization(
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
) -> None:
    """Mark configured repositories whose Restic config object is missing."""
    for item in repositories.values():
        if config.repository_disabled_reason(item) is not None:
            continue
        storage_id = item.get("storage-id")
        if storage_id not in storage:
            continue
        try:
            backend = storage[storage_id]
            initialized = (
                local.is_initialized(item, backend)
                if backend["type"] == "local"
                else s3.is_initialized(item, backend)
            )
        except (BackupError, KeyError):
            continue
        item["_initialized"] = initialized


def menu_choice(
    label: str, description: str, value: str, width: int = 23
) -> questionary.Choice:
    return tui_menu_choice(label, description, value, width)


def repository_choices(
    repositories: dict[str, dict[str, Any]],
    label: Callable[[str, dict[str, Any]], str],
    *,
    checked: str | None = None,
    disabled_reason: Callable[
        [dict[str, Any]], str | None
    ] = repository_disabled_reason,
) -> list[questionary.Choice | questionary.Separator]:
    """Build repository choices with unavailable entries grouped consistently."""
    available: list[questionary.Choice | questionary.Separator] = []
    disabled: list[tuple[str, str]] = []
    for repository_id, item in repositories.items():
        text = label(repository_id, item)
        reason = disabled_reason(item)
        if reason:
            disabled.append((text, reason))
        else:
            available.append(
                questionary.Choice(
                    text,
                    repository_id,
                    checked=repository_id == checked,
                )
            )
    return group_disabled_choices(
        available,
        disabled,
        heading="Disabled repositories",
        label_width=max(20, max(map(len, repositories))),
    )


def repository_initialization_states(
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, str | None]]:
    states: dict[str, tuple[str, str | None]] = {}
    for repository_id, item in repositories.items():
        reason = config.repository_disabled_reason(item)
        if reason:
            states[repository_id] = ("disabled", reason)
            continue
        try:
            code = restic.repository_command(
                item,
                storage[item["storage-id"]],
                ["cat", "config"],
                quiet=True,
            )
        except BackupError as exc:
            states[repository_id] = ("disabled", str(exc))
            continue
        if code == 0:
            states[repository_id] = ("initialized", None)
        elif code == 10:
            states[repository_id] = ("uninitialized", None)
        else:
            states[repository_id] = (
                "disabled",
                f"state check failed (exit {code})",
            )
    return states


def initialization_repository_choices(
    repositories: dict[str, dict[str, Any]],
    states: dict[str, tuple[str, str | None]],
    *,
    source: bool = False,
    exclude: set[str] | None = None,
) -> list[questionary.Choice | questionary.Separator]:
    """Group repositories by whether initialization may select them."""
    uninitialized: list[str] = []
    initialized: list[str] = []
    disabled: list[tuple[str, str]] = []
    for repository_id in repositories:
        if repository_id in (exclude or set()):
            continue
        state, reason = states[repository_id]
        if state == "initialized":
            initialized.append(repository_id)
        elif state == "uninitialized":
            uninitialized.append(repository_id)
        else:
            disabled.append((repository_id, str(reason)))

    selectable = initialized if source else uninitialized
    unavailable = uninitialized if source else initialized
    choices: list[questionary.Choice | questionary.Separator] = [
        questionary.Choice(repository_id, repository_id) for repository_id in selectable
    ]
    choices = group_disabled_choices(
        choices,
        [
            (repository_id, "not initialized" if source else "already initialized")
            for repository_id in unavailable
        ],
        heading="Uninitialized repositories" if source else "Initialized repositories",
        label_width=max(20, max(map(len, repositories))),
    )
    return group_disabled_choices(
        choices,
        disabled,
        heading="Disabled repositories",
        label_width=max(20, max(map(len, repositories))),
    )


def copyable_cli_command(*args: str) -> str:
    command = [
        "uv",
        "run",
        "restic-backups",
        "--config",
        str(config.config_path().resolve()),
    ]
    if os.environ.get(sops.SOPS_ENV) == "1":
        command.append("--sops")
    return shlex.join([*command, *args])


def choose_dry_run(command: str) -> bool:
    console.print(Text("Command:", style="bold"))
    console.print(Text(command, style="cyan"))
    selected = checkbox(
        "Options:",
        choices=[
            menu_choice("Dry run", "Show what would happen without writing", "dry-run"),
            questionary.Separator(" "),
        ],
    ).unsafe_ask()
    if selected is None:
        raise typer.Abort()
    return "dry-run" in selected


def print_typer_help(application: typer.Typer, name: str) -> None:
    command = typer.main.get_command(application)
    console.print(command.get_help(typer.Context(command, info_name=name)))


def repository_menu() -> None:
    while True:
        selected = select(
            "Repository command:",
            choices=[
                menu_choice(
                    "List",
                    "Show configured storage destinations",
                    "list",
                ),
                menu_choice(
                    "Prime cache",
                    "Download and validate repository metadata",
                    "prime-cache",
                ),
                menu_choice(
                    "Copy",
                    "Copy snapshots between repositories",
                    "copy",
                ),
                menu_choice(
                    "Compact",
                    "Remove unused data or repack repository files",
                    "prune",
                ),
                menu_choice(
                    "Initialize",
                    "Create one destination, or explicitly all",
                    "init",
                ),
                menu_choice(
                    "Destroy",
                    "Permanently erase repository objects",
                    "destroy",
                ),
                menu_choice("Help", "Show command flags", "help"),
                menu_choice("Back", "Return to the main menu", "back"),
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
            elif selected == "init":
                init_menu()
                return
            elif selected == "prime-cache":
                prime_cache_command(None)
                return
            elif selected == "copy":
                copy_command()
                return
            elif selected == "prune":
                prune_menu()
                return
            elif selected == "destroy":
                destroy_command()
                return
        except (typer.Abort, typer.Exit):
            continue


def init_menu() -> None:
    while True:
        selected = select(
            "Initialize repository:",
            choices=[
                menu_choice("Empty", "Create a new independent repository", "empty"),
                menu_choice(
                    "From existing",
                    "Reuse another repository's chunker parameters",
                    "from-existing",
                ),
                menu_choice("Help", "Show initialization flags", "help"),
                menu_choice("Back", "Return to repository commands", "back"),
                questionary.Separator(" "),
            ],
        ).unsafe_ask()
        if selected in {None, "back"}:
            return
        if selected == "help":
            print_typer_help(repository_app, "restic-backups generic repository")
            continue
        if selected == "empty":
            init_command()
        else:
            init_from_menu()
        return


def init_from_menu() -> None:
    _, storage, repositories, _ = validated()
    states = repository_initialization_states(storage, repositories)
    if not any(state == "initialized" for state, _ in states.values()):
        fail("no initialized source repository is available")
    selected = select(
        "Source repository:",
        choices=initialization_repository_choices(
            repositories,
            states,
            source=True,
        )
        + [questionary.Separator(" ")],
    ).unsafe_ask()
    if selected is None:
        raise typer.Abort()
    source_id = str(selected)
    destinations = {
        repository_id: item
        for repository_id, item in repositories.items()
        if repository_id != source_id
    }
    if not any(
        states[repository_id][0] == "uninitialized" for repository_id in destinations
    ):
        fail("no uninitialized destination repository is available")
    selected = select(
        "Destination repository:",
        choices=initialization_repository_choices(
            repositories,
            states,
            exclude={source_id},
        )
        + [questionary.Separator(" ")],
    ).unsafe_ask()
    if selected is None:
        raise typer.Abort()
    destination_id = str(selected)
    scope = select(
        "Snapshots after initialization:",
        choices=[
            menu_choice("None", "Initialize without copying snapshots", "none"),
            menu_choice("All", "Copy every snapshot from the source", "all"),
            menu_choice("Select", "Choose one or more snapshots", "select"),
            menu_choice("Back", "Return to initialization options", "back"),
            questionary.Separator(" "),
        ],
    ).unsafe_ask()
    if scope in {None, "back"}:
        raise typer.Abort()
    snapshot_ids: list[str] = []
    if scope == "select":
        choices = repository_snapshot_choices(
            repositories[source_id],
            storage[repositories[source_id]["storage-id"]],
        )
        if not choices:
            fail(f"repository '{source_id}' contains no snapshots")
        selected = checkbox(
            "Snapshots:",
            required=True,
            choices=[*choices, questionary.Separator(" ")],
        ).unsafe_ask()
        if selected is None:
            raise typer.Abort()
        snapshot_ids = list(map(str, selected))
    init_command(
        destination_id,
        from_repository=source_id,
        copy_all_snapshots=scope == "all",
        copy_snapshots=snapshot_ids,
    )


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
        prune_command(None, **options)
        return


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
            try:
                run_args(
                    backup_id,
                    [command, *args],
                    interactive=True,
                    allow_dry_run=(
                        "--dry-run" not in args and restic.supports_dry_run(command)
                    ),
                )
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
) -> str:
    if backup_id is not None:
        if backup_id not in backups:
            fail(f"backup job '{backup_id}' not found in {config.config_path()}")
        return backup_id
    if not sys.stdin.isatty():
        fail("job ID is required when stdin is not interactive")
    choices: list[questionary.Choice | questionary.Separator] = []
    disabled: list[tuple[str, str]] = []
    for item_id, item in backups.items():
        label = f"{item_id}  ({', '.join(config.job_repository_ids(item, item_id))})"
        if any(
            repository_is_available(repositories[value])
            for value in config.job_repository_ids(item, item_id)
        ):
            choices.append(questionary.Choice(label, value=item_id))
        else:
            disabled.append((label, "no available repositories"))
    if not choices:
        fail("no enabled backups are available")
    choices = group_disabled_choices(
        choices,
        disabled,
        heading="Disabled jobs",
        label_width=max(20, max(map(len, backups))),
    )
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
    configured = config.job_repository_ids(backups[backup_id], backup_id)
    if requested:
        selected = requested
    elif not sys.stdin.isatty():
        fail("at least one --repository is required when stdin is not interactive")
    else:
        enabled = [
            value
            for value in configured
            if repository_is_available(repositories[value])
        ]
        if not enabled:
            fail(f"backup job '{backup_id}' has no available repositories")
        configured_repositories = {
            repository_id: repositories[repository_id] for repository_id in configured
        }
        selected = checkbox(
            "Repositories:",
            required=True,
            choices=repository_choices(
                configured_repositories,
                lambda repository_id, _: repository_id,
                checked=enabled[0] if len(enabled) == 1 else None,
            )
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
    configured = config.job_repository_ids(backups[backup_id], backup_id)
    if requested is not None:
        return choose_repositories(backup_id, [requested], repositories, backups)[0]
    enabled = [
        value for value in configured if repository_is_available(repositories[value])
    ]
    if not enabled:
        fail(f"backup job '{backup_id}' has no available repositories")
    if len(enabled) == 1:
        return enabled[0]
    if not sys.stdin.isatty():
        fail("--repository is required when stdin is not interactive")
    configured_repositories = {
        repository_id: repositories[repository_id] for repository_id in configured
    }
    selected = select(
        "Repository:",
        choices=repository_choices(
            configured_repositories,
            lambda repository_id, _: repository_id,
        )
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
        reason = repository_disabled_reason(restic_repository)
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


@repository_app.command("list")
def repository_list_command() -> None:
    """Show configured restic repositories."""
    audit_command("repository", "list")
    _, storage, repositories, _ = validated()
    probe_repository_initialization(storage, repositories)
    show_repositories(storage, repositories)


@repository_app.command("init")
def init_command(
    repository_id: Annotated[
        str | None,
        typer.Argument(help="Repository ID; prompts when omitted."),
    ] = None,
    all_repositories: Annotated[
        bool,
        typer.Option("--all", help="Initialize every available repository."),
    ] = False,
    from_repository: Annotated[
        str | None,
        typer.Option(
            "--from-repository",
            help="Copy chunker parameters from this repository ID.",
        ),
    ] = None,
    copy_all_snapshots: Annotated[
        bool,
        typer.Option(
            "--copy-all-snapshots",
            help="Copy every source snapshot after initialization.",
        ),
    ] = False,
    copy_snapshots: Annotated[
        list[str] | None,
        typer.Option(
            "--copy-snapshot",
            help="Snapshot ID to copy after initialization; repeat as needed.",
        ),
    ] = None,
    dry_run: Annotated[
        bool | None,
        typer.Option("--dry-run", help="Check without initializing repositories."),
    ] = None,
) -> None:
    """Initialize repositories from scratch or a source, optionally copying snapshots."""
    _, storage, repositories, _ = validated()
    if (copy_all_snapshots or copy_snapshots) and from_repository is None:
        fail("snapshot copying requires --from-repository")
    if copy_all_snapshots and copy_snapshots:
        fail("--copy-all-snapshots and --copy-snapshot cannot be used together")
    if from_repository is not None:
        if all_repositories:
            fail("--from-repository and --all cannot be used together")
        if repository_id is None:
            fail("destination repository ID is required with --from-repository")
        source = repositories.get(from_repository)
        destination = repositories.get(repository_id)
        if source is None:
            fail(f"repository '{from_repository}' not found in {config.config_path()}")
        if destination is None:
            fail(f"repository '{repository_id}' not found in {config.config_path()}")
        for selected_id, item in (
            (from_repository, source),
            (repository_id, destination),
        ):
            reason = config.repository_disabled_reason(item)
            if reason:
                fail(f"repository '{selected_id}' is unavailable: {reason}")
        if from_repository == repository_id:
            fail("source and destination repositories must be different")
        copy_args = (
            ["--copy-all-snapshots"]
            if copy_all_snapshots
            else [
                value
                for snapshot_id in copy_snapshots or []
                for value in ("--copy-snapshot", snapshot_id)
            ]
        )
        command = copyable_cli_command(
            "generic",
            "repository",
            "init",
            repository_id,
            "--from-repository",
            from_repository,
            *copy_args,
        )
        if dry_run is None:
            dry_run = choose_dry_run(command) if sys.stdin.isatty() else False
        audit_command(
            "repository",
            "init",
            repository_id,
            "--from-repository",
            from_repository,
            *copy_args,
            *(["--dry-run"] if dry_run else []),
        )
        try:
            if copy_all_snapshots or copy_snapshots:
                code = restic.copy_repository(
                    source,
                    storage[source["storage-id"]],
                    destination,
                    storage[destination["storage-id"]],
                    list(copy_snapshots or []),
                    dry_run=dry_run,
                )
            else:
                code = restic.initialize_repository_from_source(
                    source,
                    storage[source["storage-id"]],
                    destination,
                    storage[destination["storage-id"]],
                    dry_run=dry_run,
                )
        except BackupError as exc:
            fail(str(exc))
        if code:
            raise typer.Exit(code)
        if dry_run:
            message = "dry run complete; repository unchanged"
        elif copy_all_snapshots or copy_snapshots:
            message = "initialization and snapshot copy complete"
        else:
            message = "initialization complete"
        error_console.print(
            Text(
                f"{from_repository} → {repository_id}: {message}",
                style="bold green",
            )
        )
        return
    if repository_id is not None and all_repositories:
        fail("repository ID and --all cannot be used together")
    if repository_id is None and not all_repositories:
        if not sys.stdin.isatty():
            fail("repository ID or --all is required when stdin is not interactive")
        states = repository_initialization_states(storage, repositories)
        if not any(state == "uninitialized" for state, _ in states.values()):
            fail("no uninitialized repository is available")
        selected = select(
            "Repository to initialize:",
            choices=[
                questionary.Choice("All uninitialized repositories", ALL_REPOSITORIES)
            ]
            + initialization_repository_choices(
                repositories,
                states,
            )
            + [questionary.Separator(" ")],
        ).unsafe_ask()
        if selected is None:
            raise typer.Abort()
        if selected == ALL_REPOSITORIES:
            all_repositories = True
        else:
            repository_id = str(selected)

    if all_repositories:
        selected_repositories = list(repositories.items())
    else:
        restic_repository = repositories.get(str(repository_id))
        if restic_repository is None:
            fail(f"repository '{repository_id}' not found in {config.config_path()}")
        selected_repositories = [(str(repository_id), restic_repository)]

    if dry_run is None:
        dry_run = (
            choose_dry_run(
                copyable_cli_command(
                    "generic",
                    "repository",
                    "init",
                    *(["--all"] if all_repositories else [str(repository_id)]),
                )
            )
            if sys.stdin.isatty()
            else False
        )
    audit_command(
        "repository",
        "init",
        *(["--all"] if all_repositories else [str(repository_id)]),
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
def prime_cache_command(
    repository_id: Annotated[
        str | None,
        typer.Argument(help="Repository ID; prompts when omitted."),
    ] = None,
) -> None:
    """Download and validate repository metadata into its local cache."""
    _, storage, repositories, _ = validated()
    if repository_id is None:
        probe_repository_initialization(storage, repositories)
        if not sys.stdin.isatty():
            fail("repository ID is required when stdin is not interactive")
        selected = select(
            "Repository cache to prime:",
            choices=repository_choices(
                repositories,
                lambda repository_id, _: repository_id,
            )
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


def repository_snapshot_choices(
    restic_repository: dict[str, Any], storage: dict[str, Any]
) -> list[questionary.Choice | questionary.Separator]:
    try:
        code, output = restic.repository_run(
            restic_repository,
            storage,
            ["snapshots", "--json"],
            quiet=True,
            capture=True,
        )
        if code:
            raise typer.Exit(code)
        snapshots = json.loads(output)
    except (BackupError, json.JSONDecodeError) as exc:
        fail(str(exc))
    if not isinstance(snapshots, list):
        fail("restic snapshots returned invalid JSON")
    choices: list[questionary.Choice | questionary.Separator] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str):
            fail("restic snapshots returned invalid JSON")
        snapshot_id = snapshot["id"]
        timestamp = str(snapshot.get("time", "unknown")).replace("T", " ")[:19]
        short_id = str(snapshot.get("short_id", snapshot_id[:8]))
        tags = ", ".join(map(str, snapshot.get("tags", []))) or "no tags"
        choices.append(
            questionary.Choice(
                f"{timestamp}  {short_id}  {tags}",
                snapshot_id,
            )
        )
    return choices


@repository_app.command("copy")
def copy_command(
    source_id: Annotated[
        str | None,
        typer.Argument(help="Source repository ID; prompts when omitted."),
    ] = None,
    destination_id: Annotated[
        str | None,
        typer.Argument(help="Destination repository ID; prompts when omitted."),
    ] = None,
    snapshot_ids: Annotated[
        list[str] | None,
        typer.Argument(help="Snapshot IDs; omit to copy every snapshot."),
    ] = None,
    dry_run: Annotated[
        bool | None,
        typer.Option("--dry-run", help="Show what would be copied without writing."),
    ] = None,
) -> None:
    """Copy snapshots from one configured repository to another."""
    _, storage, repositories, _ = validated()
    prompted = source_id is None or destination_id is None
    if source_id is None:
        if not sys.stdin.isatty():
            fail("source repository ID is required when stdin is not interactive")
        selected = select(
            "Source repository:",
            choices=repository_choices(
                repositories,
                lambda repository_id, _: repository_id,
            )
            + [questionary.Separator(" ")],
        ).unsafe_ask()
        if selected is None:
            raise typer.Abort()
        source_id = str(selected)

    source = repositories.get(source_id)
    if source is None:
        fail(f"repository '{source_id}' not found in {config.config_path()}")
    reason = config.repository_disabled_reason(source)
    if reason:
        fail(f"repository '{source_id}' is unavailable: {reason}")

    if destination_id is None:
        if not sys.stdin.isatty():
            fail("destination repository ID is required when stdin is not interactive")
        destinations = {
            repository_id: item
            for repository_id, item in repositories.items()
            if repository_id != source_id
        }
        if not destinations:
            fail("no destination repository is configured")
        selected = select(
            "Destination repository:",
            choices=repository_choices(
                destinations,
                lambda repository_id, _: repository_id,
            )
            + [questionary.Separator(" ")],
        ).unsafe_ask()
        if selected is None:
            raise typer.Abort()
        destination_id = str(selected)

    destination = repositories.get(destination_id)
    if destination is None:
        fail(f"repository '{destination_id}' not found in {config.config_path()}")
    reason = config.repository_disabled_reason(destination)
    if reason:
        fail(f"repository '{destination_id}' is unavailable: {reason}")
    if source_id == destination_id:
        fail("source and destination repositories must be different")

    selected_snapshots = list(snapshot_ids or [])
    if prompted:
        scope = select(
            "Snapshots to copy:",
            choices=[
                menu_choice("All", "Copy every snapshot not already copied", "all"),
                menu_choice("Select", "Choose one or more snapshots", "select"),
                menu_choice("Back", "Return to repository commands", "back"),
                questionary.Separator(" "),
            ],
        ).unsafe_ask()
        if scope in {None, "back"}:
            raise typer.Abort()
        if scope == "select":
            choices = repository_snapshot_choices(source, storage[source["storage-id"]])
            if not choices:
                fail(f"repository '{source_id}' contains no snapshots")
            selected = checkbox(
                "Snapshots:",
                required=True,
                choices=[*choices, questionary.Separator(" ")],
            ).unsafe_ask()
            if selected is None:
                raise typer.Abort()
            selected_snapshots = list(map(str, selected))

    command = copyable_cli_command(
        "generic",
        "repository",
        "copy",
        source_id,
        destination_id,
        *selected_snapshots,
    )
    if dry_run is None:
        dry_run = choose_dry_run(command) if sys.stdin.isatty() else False
    audit_command(
        "repository",
        "copy",
        source_id,
        destination_id,
        *selected_snapshots,
        *(["--dry-run"] if dry_run else []),
    )
    if not dry_run:
        error_console.print(
            Text(
                f"{source_id} → {destination_id}: copying snapshots; cloud reads and writes may incur charges",
                style="cyan",
            )
        )
    try:
        code = restic.copy_repository(
            source,
            storage[source["storage-id"]],
            destination,
            storage[destination["storage-id"]],
            selected_snapshots,
            dry_run=dry_run,
        )
    except BackupError as exc:
        fail(str(exc))
    if code:
        raise typer.Exit(code)
    message = "dry run complete; nothing copied" if dry_run else "copy complete"
    error_console.print(
        Text(f"{source_id} → {destination_id}: {message}", style="bold green")
    )


@repository_app.command("prune")
def prune_command(
    repository_id: Annotated[
        str | None,
        typer.Argument(help="Repository ID; prompts when omitted."),
    ] = None,
    dry_run: Annotated[
        bool | None,
        typer.Option("--dry-run", help="Show what prune would do without writing."),
    ] = None,
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
        probe_repository_initialization(storage, repositories)
        if not sys.stdin.isatty():
            fail("repository ID is required when stdin is not interactive")
        selected = select(
            "Repository to prune:",
            choices=repository_choices(
                repositories,
                lambda item_id, item: (
                    f"{item_id}  "
                    f"({repository.location(item, storage[item['storage-id']])})"
                ),
            )
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
    prompted = dry_run is None and sys.stdin.isatty()
    if dry_run is None:
        dry_run = (
            choose_dry_run(
                copyable_cli_command(
                    "generic", "repository", "prune", repository_id, *args[1:]
                )
            )
            if prompted
            else False
        )
    if dry_run:
        args.append("--dry-run")

    if not dry_run and not yes and not prompted:
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
    if backup is None or repository_id is None:
        probe_repository_initialization(storage, repositories)
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
def forget_command(
    backup: Annotated[
        str | None,
        typer.Argument(help="Job ID; prompts when omitted.", metavar="JOB_ID"),
    ] = None,
    dry_run: Annotated[
        bool | None,
        typer.Option(
            "--dry-run", help="Show what would be forgotten without deleting."
        ),
    ] = None,
    repository_id: Annotated[
        str | None,
        typer.Option("--repository", "-r", help="Repository ID."),
    ] = None,
) -> None:
    """Forget one snapshot and prune its unreferenced data."""
    if not sys.stdin.isatty():
        fail("forget requires an interactive terminal")
    _, storage, repositories, backups = validated()
    if backup is None or repository_id is None:
        probe_repository_initialization(storage, repositories)
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
    prompted = dry_run is None
    if dry_run is None:
        dry_run = choose_dry_run(
            copyable_cli_command(
                "generic",
                "snapshot",
                "forget",
                backup_id,
                "--repository",
                repository_id,
                snapshot_id,
            )
        )
    if not dry_run and not prompted:
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
def destroy_command(
    repository_id: Annotated[
        str | None,
        typer.Argument(help="Repository ID; prompts when omitted."),
    ] = None,
    dry_run: Annotated[
        bool | None,
        typer.Option("--dry-run", help="Show the target without deleting objects."),
    ] = None,
) -> None:
    """Permanently erase a configured restic repository."""
    if not sys.stdin.isatty():
        fail("destroy requires an interactive terminal")
    _, storage, repositories, _ = validated()
    if repository_id is None:
        probe_repository_initialization(storage, repositories)
        selected = select(
            "Repository to permanently destroy:",
            choices=repository_choices(
                repositories,
                lambda repository_id, item: (
                    f"{repository_id}  "
                    f"({repository.location(item, storage[item['storage-id']])})"
                ),
                disabled_reason=lambda item: (
                    "storage disabled"
                    if not storage[item["storage-id"]].get("enabled", True)
                    else "not initialized"
                    if item.get("_initialized") is False
                    else None
                ),
            )
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
    if dry_run is None:
        dry_run = choose_dry_run(
            copyable_cli_command("generic", "repository", "destroy", repository_id)
        )
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
    return copyable_cli_command(
        *[
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


def run_args(
    backup: str | None,
    args: list[str],
    *,
    interactive: bool = False,
    repository_id: str | None = None,
    allow_dry_run: bool = False,
) -> None:
    _, storage, repositories, backups = validated()
    if backup is None or repository_id is None:
        probe_repository_initialization(storage, repositories)
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
        options = checkbox(
            "Options:",
            choices=[
                menu_choice("Print only", "Do not execute the command", "print"),
                *(
                    [
                        menu_choice(
                            "Dry run",
                            "Show what would happen without writing",
                            "dry-run",
                        )
                    ]
                    if allow_dry_run
                    else []
                ),
                questionary.Separator(" "),
            ],
        ).unsafe_ask()
        if options is None:
            raise typer.Abort()
        if "dry-run" in options:
            args.insert(1, "--dry-run")
        if "print" in options:
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

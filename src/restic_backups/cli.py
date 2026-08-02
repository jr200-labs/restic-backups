"""Command-line interface for configured restic backups."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Annotated, Any, NoReturn

import questionary
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

from . import audit
from . import config as config_module
from .errors import BackupError
from .generic import cli as generic_cli
from .generic import repository
from .generic import sops as sops_module
from .generic.tui import select

app = typer.Typer(
    help="Configured backup commands.",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(
    generic_cli.app,
    name="generic",
    invoke_without_command=True,
    no_args_is_help=False,
)
VERBOSE_ENV = "RESTIC_BACKUPS_VERBOSE"
error_console = Console(stderr=True)


def menu_choice(label: str, description: str, value: str) -> questionary.Choice:
    return questionary.Choice(
        [("fg:ansicyan bold", f"{label:<17}"), ("", description)], value
    )


@app.callback()
def configure(
    context: typer.Context,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            envvar=config_module.CONFIG_ENV,
            dir_okay=False,
            help=f"YAML configuration file. Env: {config_module.CONFIG_ENV}.",
        ),
    ] = None,
    use_sops: Annotated[
        bool,
        typer.Option(
            "--sops",
            envvar=sops_module.SOPS_ENV,
            help=f"Decrypt the configuration with SOPS. Env: {sops_module.SOPS_ENV}.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            envvar=VERBOSE_ENV,
            help=f"Show command details. Env: {VERBOSE_ENV}.",
        ),
    ] = False,
) -> None:
    """Configure storage before running a command."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        handlers=[
            RichHandler(
                console=error_console,
                show_path=False,
                show_time=False,
            )
        ],
        force=True,
    )
    logging.getLogger("restic_backups").setLevel(
        logging.DEBUG if verbose else logging.INFO
    )
    if config_file is not None:
        os.environ[config_module.CONFIG_ENV] = str(config_file)
    os.environ[sops_module.SOPS_ENV] = "1" if use_sops else "0"
    if context.invoked_subcommand is None:
        if not sys.stdin.isatty():
            typer.echo(context.get_help())
            return
        try:
            interactive_menu()
        except KeyboardInterrupt:
            return


def interactive_menu() -> None:
    """Navigate all user-facing workflows with an arrow-key menu."""
    while True:
        selected = select(
            "Workflow:",
            choices=[
                menu_choice(
                    "Generic backups",
                    "Manage repositories, backups, and snapshots",
                    "generic",
                ),
                menu_choice(
                    "Voice Memos",
                    "Back up, restore, transcribe, and diarize memos",
                    "voice-memos",
                ),
                menu_choice(
                    "Check config",
                    "Decrypt and validate configuration locally",
                    "check-config",
                ),
                menu_choice("Help", "Show top-level commands and flags", "help"),
                menu_choice("Exit", "Return without doing anything", "exit"),
                questionary.Separator(" "),
            ],
        ).unsafe_ask()
        if selected in {None, "exit"}:
            return
        if selected == "help":
            generic_cli.print_typer_help(app, "restic-backups")
        elif selected == "generic":
            generic_cli.interactive_menu()
        elif selected == "voice-memos":
            from .voice_memos.cli import interactive_menu as voice_memos_menu

            voice_memos_menu(prepare_voice_memos)
        elif selected == "check-config":
            check_config_command()
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
        return config_module.load_validated()
    except BackupError as exc:
        fail(str(exc))


@app.command("check-config")
def check_config_command() -> None:
    """Validate configuration without contacting remote storage."""
    audit.record("restic-backups", ["check-config"])
    validated()
    typer.echo("config ok")


@app.command(
    "voice-memos",
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        "help_option_names": [],
    },
)
def voice_memos_command(context: typer.Context) -> None:
    """Back up, transcribe, summarise, and diarize macOS Voice Memos."""
    from .voice_memos.cli import cli
    from .voice_memos.cli import interactive_menu as voice_memos_menu

    if not context.args:
        if sys.stdin.isatty():
            try:
                voice_memos_menu(prepare_voice_memos)
            except KeyboardInterrupt:
                return
        else:
            cli.main(
                args=["--help"],
                prog_name="restic-backups voice-memos",
                standalone_mode=True,
            )
        return
    if "--help" not in context.args:
        prepare_voice_memos()

    cli.main(
        args=list(context.args),
        prog_name="restic-backups voice-memos",
        standalone_mode=True,
    )


def prepare_voice_memos() -> None:
    if "SUMMARIES_DIR" in os.environ:
        return
    _, credentials, stores, backups = validated()
    try:
        store, _ = repository.resolve("voice-memos", credentials, stores, backups)
        path = repository.data_dir("voice-memos", store) / "summaries"
    except BackupError as exc:
        fail(str(exc))
    os.environ["SUMMARIES_DIR"] = str(path)


def main() -> None:
    """Audit and run the installed command."""
    try:
        audit.record("restic-backups", list(sys.argv[1:]))
    except BackupError as exc:
        error_console.print(Text(f"restic-backups: {exc}", style="bold red"))
        raise SystemExit(1) from exc
    try:
        app()
    except KeyboardInterrupt:
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()

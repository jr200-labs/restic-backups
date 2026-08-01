"""Command-line interface for configured restic backups."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

from . import config as config_module
from .errors import BackupError
from .generic import cli as generic_cli
from .generic import repository
from .generic import sops as sops_module

app = typer.Typer(
    help="Configured backup commands.",
    no_args_is_help=True,
)
app.add_typer(
    generic_cli.app,
    name="generic",
    invoke_without_command=True,
    no_args_is_help=False,
)
VERBOSE_ENV = "RESTIC_BACKUPS_VERBOSE"
error_console = Console(stderr=True)


@app.callback()
def configure(
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
    if "SUMMARIES_DIR" not in os.environ and "--help" not in context.args:
        _, credentials, stores, backups = validated()
        try:
            store, _ = repository.resolve("voice-memos", credentials, stores, backups)
            path = repository.data_dir("voice-memos", store) / "summaries"
        except BackupError as exc:
            fail(str(exc))
        os.environ["SUMMARIES_DIR"] = str(path)

    from .voice_memos.cli import cli

    cli.main(
        args=list(context.args),
        prog_name="restic-backups voice-memos",
        standalone_mode=True,
    )


if __name__ == "__main__":
    app()

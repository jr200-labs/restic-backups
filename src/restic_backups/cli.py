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
from rich.text import Text

from . import audit
from . import config as config_module
from .errors import BackupError
from .generic import cli as generic_cli
from .generic import repository
from .generic import sops as sops_module
from .generic.tui import menu_choice as tui_menu_choice
from .generic.tui import select
from .jobs import cli as jobs_cli

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
app.add_typer(
    jobs_cli.app,
    name="job",
    invoke_without_command=True,
    no_args_is_help=False,
)
VERBOSE_ENV = "RESTIC_BACKUPS_VERBOSE"
error_console = Console(stderr=True)


def menu_choice(label: str, description: str, value: str) -> questionary.Choice:
    return tui_menu_choice(label, description, value, 12)


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
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
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
                    "Jobs",
                    "List, run, and inspect every configured job",
                    "jobs",
                ),
                menu_choice(
                    "Repositories",
                    "List, initialize, cache, prune, or destroy repositories",
                    "repositories",
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
        elif selected == "jobs":
            jobs_cli.interactive_menu()
        elif selected == "repositories":
            generic_cli.repository_menu()
        elif selected == "check-config":
            try:
                check_config_command()
            except typer.Exit:
                continue


def fail(message: str) -> NoReturn:
    error_console.print(Text(f"restic-backups: {message}", style="bold red"))
    raise typer.Exit(1)


def validated(
    *, check_placeholders: bool = False
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    try:
        return config_module.load_validated(check_placeholders=check_placeholders)
    except BackupError as exc:
        fail(str(exc))


@app.command("check-config")
def check_config_command() -> None:
    """Validate configuration without contacting remote storage."""
    validated(check_placeholders=True)
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
    """Inspect, restore, transcribe, and diarize macOS Voice Memos."""
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
    from .voice_memos import pipeline as voice_pipeline
    from .voice_memos import workflow as voice_workflow

    _, storage, repositories, jobs = validated()
    try:
        job_id = voice_workflow.job_id(jobs)
        source = jobs[job_id]["source"]
        if "summaries-dir" in source:
            path = Path(source["summaries-dir"]).expanduser()
            os.environ["SUMMARIES_DIR"] = str(path)
            voice_pipeline.SUMMARIES_DIR = path
            voice_workflow.SUMMARIES_DIR = path
            return
        repository_id = next(
            value
            for value in config_module.job_repository_ids(jobs[job_id], job_id)
            if config_module.repository_is_enabled(repositories[value])
        )
        restic_repository, backend = repository.resolve(
            job_id, storage, repositories, jobs, repository_id
        )
        path = repository.data_dir(job_id, restic_repository, backend) / "summaries"
    except (BackupError, StopIteration) as exc:
        fail(str(exc))
    os.environ["SUMMARIES_DIR"] = str(path)
    voice_pipeline.SUMMARIES_DIR = path
    voice_workflow.SUMMARIES_DIR = path


def main() -> None:
    """Run the installed command."""
    successful = False
    try:
        app()
        successful = True
    except SystemExit as exc:
        successful = exc.code is None or exc.code == 0
        raise
    except KeyboardInterrupt:
        successful = True
        raise SystemExit(0) from None
    finally:
        audit.finish_all(successful)


if __name__ == "__main__":
    main()

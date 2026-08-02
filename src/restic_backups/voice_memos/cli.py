"""Command-line interface for Voice Memos workflows."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from pathlib import Path

import click
import questionary

from .. import config
from ..errors import BackupError
from ..generic import sops
from . import parallel, pipeline, workflow


def menu_choice(
    label: str, description: str, value: str, width: int = 21
) -> questionary.Choice:
    return questionary.Choice(
        [("fg:ansicyan bold", f"{label:<{width}}"), ("", description)], value
    )


def operation(action: Callable[[], int | None]) -> None:
    try:
        code = action() or 0
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    if code:
        raise click.exceptions.Exit(code)


def interactive_menu(before_run: Callable[[], None] | None = None) -> None:
    """Navigate Voice Memos commands with an arrow-key menu."""
    while True:
        selected = questionary.select(
            "Voice Memos command:",
            choices=[
                *[
                    menu_choice(
                        name,
                        command.help or "Run this Voice Memos command",
                        name,
                    )
                    for name, command in cli.commands.items()
                    if not command.hidden
                ],
                menu_choice("Help", "Show Voice Memos commands and flags", "help"),
                menu_choice("Back", "Return to the previous menu", "back"),
            ],
        ).ask()
        if selected in {None, "back"}:
            return
        if selected == "help":
            click.echo(
                cli.get_help(click.Context(cli, info_name="restic-backups voice-memos"))
            )
            continue

        command = cli.commands[str(selected)]
        parent = click.Context(cli, info_name="restic-backups voice-memos")
        context = click.Context(command, info_name=str(selected), parent=parent)
        click.echo(command.get_usage(context).strip())
        while True:
            command_action = questionary.select(
                f"voice-memos {selected}:",
                choices=[
                    menu_choice(
                        "Enter arguments",
                        "Build and optionally run this command",
                        "run",
                        17,
                    ),
                    menu_choice("Help", "Show full flags for this command", "help", 17),
                    menu_choice(
                        "Back", "Choose another Voice Memos command", "back", 17
                    ),
                ],
            ).ask()
            if command_action in {None, "back"}:
                break
            if command_action == "help":
                click.echo(command.get_help(context))
                continue
            arguments = questionary.text(
                f"Arguments for 'voice-memos {selected}' (optional):"
            ).ask()
            if arguments is None:
                continue
            try:
                args = shlex.split(arguments)
            except ValueError as exc:
                raise click.ClickException(f"invalid arguments: {exc}") from exc
            if args in (["--help"], ["-h"]):
                click.echo(command.get_help(context))
                continue

            copyable = [
                "uv",
                "run",
                "restic-backups",
                "--config",
                str(config.config_path().resolve()),
            ]
            if os.environ.get(sops.SOPS_ENV) == "1":
                copyable.append("--sops")
            copyable.extend(["voice-memos", str(selected), *args])
            click.echo("Command:")
            click.echo(shlex.join(copyable))
            while True:
                action = questionary.select(
                    "Action:",
                    choices=[
                        questionary.Choice("Run", "run"),
                        questionary.Choice("Print only", "print"),
                        menu_choice(
                            "Help", "Show full flags for this command", "help", 12
                        ),
                        menu_choice("Back", "Change the command arguments", "back", 12),
                        questionary.Choice("Cancel", "cancel"),
                    ],
                ).ask()
                if action == "help":
                    click.echo(command.get_help(context))
                    continue
                if action == "back":
                    break
                if action != "run":
                    if action == "cancel":
                        click.echo("Cancelled; nothing was run.", err=True)
                    return
                if before_run is not None:
                    before_run()
                cli.main(
                    args=[str(selected), *args],
                    prog_name="restic-backups voice-memos",
                    standalone_mode=True,
                )
                return


@click.group()
def cli() -> None:
    """Back up, transcribe, summarise, and diarize macOS Voice Memos."""


@cli.command()
@click.option(
    "--recordings-dir",
    type=click.Path(path_type=Path),
    default=pipeline.DEFAULT_RECORDINGS_DIR,
    show_default=True,
)
def backup(recordings_dir: Path) -> None:
    """Back up recordings and summaries in one restic snapshot."""
    operation(lambda: workflow.backup(recordings_dir))


@cli.command()
def snapshots() -> None:
    """List repository snapshots."""
    operation(lambda: workflow.run_restic(["snapshots"]))


@cli.command("check")
def check_repository() -> None:
    """Verify repository structure and metadata."""
    operation(lambda: workflow.run_restic(["check"]))


@cli.command()
def stats() -> None:
    """Show size and file count for the latest Voice Memos snapshot."""
    operation(lambda: workflow.run_restic(["stats", "latest"], tagged=True))


@cli.command("files")
@click.argument("snapshot", default="latest")
def list_files(snapshot: str) -> None:
    """List files in a snapshot."""
    operation(lambda: workflow.run_restic(["ls", snapshot]))


@cli.command()
@click.argument("snapshot", default="latest")
@click.option("--target", type=click.Path(path_type=Path), required=True)
def restore(snapshot: str, target: Path) -> None:
    """Restore a snapshot to a target directory."""
    operation(
        lambda: workflow.run_restic(["restore", snapshot, "--target", str(target)])
    )


@cli.command()
@click.argument("query")
@click.option("--restore", "restore_missing", is_flag=True)
@click.option("--target", type=click.Path(path_type=Path), default=None)
@click.option("--reveal/--no-reveal", default=True, show_default=True)
def get(query: str, restore_missing: bool, target: Path | None, reveal: bool) -> None:
    """Resolve a summary UUID to its recording and reveal it in Finder."""
    try:
        path = workflow.find_audio(query, restore_missing, target)
        click.echo(path)
        if reveal and path.exists():
            workflow.reveal(path)
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("transcribe")
@click.option("--all", "mode_all", is_flag=True, help="Reprocess every memo.")
@click.option("--uuids", default=None, help="Comma-separated UUIDs to process.")
@click.option("--summary-only", is_flag=True, help="Reuse transcripts; redo summaries.")
@click.option("--no-summary", is_flag=True, help="Transcribe without the LLM step.")
@click.option("--force", is_flag=True, help="Re-transcribe existing transcripts.")
@click.option(
    "--engine", default="whisper-mlx", type=click.Choice(sorted(pipeline.ENGINES))
)
@click.option("--language", default="en")
@click.option("--llm-base-url", default="http://localhost:1234/v1")
@click.option("--llm-model", default="google_gemma-4-e4b-it")
@click.option("--db", default=str(pipeline.DEFAULT_DB), show_default=True)
@click.option(
    "--recordings-dir", default=str(pipeline.DEFAULT_RECORDINGS_DIR), show_default=True
)
@click.option("--limit", type=int, default=0)
def transcribe(**options: object) -> None:
    """Transcribe and optionally summarise memos; incremental by default."""
    pipeline.run(**options)


@cli.command()
@click.option("--db", default=str(pipeline.DEFAULT_DB), show_default=True)
@click.option(
    "--recordings-dir", default=str(pipeline.DEFAULT_RECORDINGS_DIR), show_default=True
)
def status(db: str, recordings_dir: str) -> None:
    """Show total, processed, pending, stale, and errored counts."""
    pipeline.status(db, recordings_dir)


@cli.command()
@click.option("--uuids", default=None)
@click.option("--force", is_flag=True)
@click.option("--limit", type=int, default=0)
@click.option(
    "--recordings-dir", default=str(pipeline.DEFAULT_RECORDINGS_DIR), show_default=True
)
@click.option(
    "--order",
    type=click.Choice(["short-first", "long-first", "natural"]),
    default="short-first",
    show_default=True,
)
@click.option("--min-duration", type=float, default=0.0)
@click.option("--min-speakers", type=int, default=2, show_default=True)
@click.option("--max-speakers", type=int, default=4, show_default=True)
@click.option("--num-speakers", type=int, default=None)
def diarize(**options: object) -> None:
    """Add speaker diarization to existing transcript records."""
    pipeline.diarize(**options)


@cli.command("diarize-parallel")
@click.option("--workers", type=click.IntRange(min=1), default=3, show_default=True)
@click.option(
    "--order",
    type=click.Choice(["short-first", "long-first", "natural"]),
    default="short-first",
    show_default=True,
)
@click.option("--min-duration", type=float, default=0.0)
@click.option("--min-speakers", type=int, default=2, show_default=True)
@click.option("--max-speakers", type=int, default=4, show_default=True)
@click.option("--detach", is_flag=True, help="Return while workers continue.")
@click.option("--dashboard", "show_dashboard", is_flag=True)
def diarize_parallel(
    workers: int,
    order: str,
    min_duration: float,
    min_speakers: int,
    max_speakers: int,
    detach: bool,
    show_dashboard: bool,
) -> None:
    """Run diarization across multiple worker processes."""
    try:
        log_dir = parallel.start(
            workers,
            order,
            min_duration,
            min_speakers,
            max_speakers,
            wait=not (detach or show_dashboard),
        )
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"worker logs: {log_dir}")
    if show_dashboard:
        from .dashboard import main

        main(str(log_dir))


@cli.command("diarize-list", hidden=True)
@click.option(
    "--order",
    type=click.Choice(["short-first", "long-first", "natural"]),
    default="short-first",
)
@click.option("--min-duration", type=float, default=0.0)
def diarize_list(order: str, min_duration: float) -> None:
    """Print UUIDs eligible for diarization."""
    pipeline.diarize_list(order, min_duration)


@cli.command("diarize-status")
def diarize_status() -> None:
    """Count transcripts with and without diarization."""
    pipeline.diarize_status()


@cli.command("migrate-layout")
def migrate_layout() -> None:
    """Move flat summary JSON into monthly directories."""
    pipeline.migrate_layout()


@cli.command()
@click.option("--db", default=str(pipeline.DEFAULT_DB), show_default=True)
@click.option("--uuid", default=None)
def peek(db: str, uuid: str | None) -> None:
    """Inspect the Voice Memos database schema."""
    pipeline.peek(db, uuid)


@cli.command("prune-index")
@click.option("--yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--db", default=str(pipeline.DEFAULT_DB), show_default=True)
def prune_index(yes: bool, dry_run: bool, db: str) -> None:
    """Remove index entries whose summary JSON is missing."""
    pipeline.prune_index(yes, dry_run, db)


@cli.command("rebuild-index")
@click.option("--dry-run", is_flag=True)
def rebuild_index(dry_run: bool) -> None:
    """Rebuild the summary index from JSON files on disk."""
    pipeline.rebuild_index(dry_run)


@cli.command()
@click.argument("log_dir", required=False)
def dashboard(log_dir: str | None) -> None:
    """Show progress for a parallel diarization run."""
    from .dashboard import main

    main(log_dir)

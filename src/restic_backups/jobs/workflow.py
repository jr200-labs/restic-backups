"""Dispatch configured job types through one snapshot interface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import BackupError
from ..generic import restic
from ..github_repository import workflow as github_workflow


def run(
    job_id: str,
    job: dict[str, Any],
    selected_repositories: list[str],
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
    *,
    dry_run: bool = False,
) -> tuple[dict[str, str], dict[str, bool]]:
    """Run any configured job and return type and destination results."""
    if job["type"] == "github-repository":
        return github_workflow.backup(
            job_id,
            job,
            selected_repositories,
            storage,
            repositories,
            jobs,
            dry_run=dry_run,
        )

    source = job["source"]
    if job["type"] == "files":
        configured_paths = source.get("paths")
        if not configured_paths:
            raise BackupError(f"{job_id}.source.paths is required to run a files job")
        paths = [str(Path(path).expanduser()) for path in configured_paths]
        extra: list[str] = []
    else:
        from ..voice_memos.pipeline import DEFAULT_RECORDINGS_DIR, SUMMARIES_DIR

        recordings = Path(
            source.get("recordings-dir", DEFAULT_RECORDINGS_DIR)
        ).expanduser()
        summaries = Path(source.get("summaries-dir", SUMMARIES_DIR)).expanduser()
        if not dry_run:
            summaries.mkdir(parents=True, exist_ok=True)
        paths = [str(recordings), str(summaries)]
        extra = ["--tag", "summaries", "--host", "mac-icloud"]

    args = ["backup", *(["--dry-run"] if dry_run else []), *paths, *extra]
    destinations: dict[str, bool] = {}
    for repository_id in selected_repositories:
        try:
            destinations[repository_id] = (
                restic.command(
                    job_id,
                    args,
                    storage,
                    repositories,
                    jobs,
                    repository_id=repository_id,
                )
                == 0
            )
        except BackupError:
            destinations[repository_id] = False
    state = "planned" if dry_run else "updated"
    return ({job["type"]: state}, destinations)

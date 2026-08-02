"""Focused checks for GitHub repository backups."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any, BinaryIO, cast
from unittest.mock import patch

import pytest

from restic_backups import audit, config
from restic_backups.errors import BackupError
from restic_backups.github_repository import workflow


def github_job(**components: bool) -> dict[str, object]:
    enabled = {
        "git": True,
        "lfs": False,
        "wiki": False,
        "metadata": False,
        "release-assets": False,
        **components,
    }
    return {
        "job-id": "example-repository",
        "restic-repository-ids": ["first", "second"],
        "github": {
            "repository-url": "git@github.com:example/example-repository.git",
            "components": enabled,
            "migration-timeout-seconds": 60,
        },
    }


def complete_config(job: dict[str, object]) -> dict[str, object]:
    return {
        "storage": [{"id": "disk", "type": "local", "path": "/Volumes/backup"}],
        "restic-repositories": [
            {
                "id": repository_id,
                "storage-id": "disk",
                "enabled": True,
                "path": repository_id,
                "password": "password",
            }
            for repository_id in ("first", "second")
        ],
        "backups": [job],
    }


def test_github_config_validates_components_urls_and_credentials() -> None:
    config.validate(complete_config(github_job()))

    invalid = github_job(lfs=True, git=False)
    with pytest.raises(config.ConfigError, match="lfs requires git"):
        config.validate(complete_config(invalid))

    invalid = github_job()
    invalid["github"]["repository-url"] = "https://token@github.com/example/repo.git"  # type: ignore[index]
    with pytest.raises(config.ConfigError, match="safe github.com URL"):
        config.validate(complete_config(invalid))

    invalid = github_job(metadata=True)
    with pytest.raises(config.ConfigError, match="api.token is required"):
        config.validate(complete_config(invalid))


def test_dry_run_does_not_write_or_run_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "config.yaml"))
    job = github_job()
    with patch("restic_backups.github_repository.workflow._run") as run:
        statuses, destinations = workflow.backup(
            "example-repository",
            job,
            ["first"],
            {},
            {},
            {"example-repository": job},
            dry_run=True,
        )
    assert statuses["git"] == "planned"
    assert destinations == {"first": True}
    assert not (tmp_path / "data").exists()
    run.assert_not_called()


def test_git_mirror_disables_automatic_repacking(tmp_path: Path) -> None:
    destination = tmp_path / "repository.git"
    completed = subprocess.CompletedProcess([], 0, "", "")
    with patch(
        "restic_backups.github_repository.workflow._run", return_value=completed
    ) as run:
        workflow._git_mirror("git@github.com:example/repository.git", destination, {})
    commands = [item.args[0] for item in run.call_args_list]
    assert commands[0][1:5] == ["-c", "gc.auto=0", "-c", "maintenance.auto=false"]
    assert ["config", "gc.auto", "0"] == commands[1][-3:]

    destination.mkdir()
    with patch(
        "restic_backups.github_repository.workflow._run", return_value=completed
    ) as run:
        workflow._git_mirror("git@github.com:example/repository.git", destination, {})
    assert run.call_args.args[0][-3:] == ["remote", "update", "--prune"]


def test_safe_extract_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "migration.tar"
    with tarfile.open(archive, "w") as target:
        member = tarfile.TarInfo("../outside")
        member.size = 1
        target.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(BackupError, match="unsafe path"):
        workflow._safe_extract(archive, tmp_path / "extract")
    assert not (tmp_path / "outside").exists()


def test_metadata_export_replaces_previous_directory_atomically(tmp_path: Path) -> None:
    destination = tmp_path / "github-export"
    destination.mkdir()
    (destination / "old.json").write_text("old")
    archive = tmp_path / "migration.tar"
    with tarfile.open(archive, "w") as target:
        member = tarfile.TarInfo("new.json")
        member.size = 3
        target.addfile(member, io.BytesIO(b"new"))
    archive_bytes = archive.read_bytes()

    def run(_args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        output = kwargs.get("output")
        if output is not None:
            cast(BinaryIO, output).write(archive_bytes)
        return subprocess.CompletedProcess([], 0, "", "")

    with (
        patch(
            "restic_backups.github_repository.workflow._gh_json",
            side_effect=[
                {"owner": {"type": "Organization"}},
                {"id": 7},
                {"state": "exported"},
            ],
        ),
        patch("restic_backups.github_repository.workflow._run", side_effect=run),
    ):
        assert workflow._metadata("example", "repo", destination, 30, {}) == "updated"
    assert (destination / "new.json").read_text() == "new"
    assert not (destination / "old.json").exists()


def test_release_assets_skip_unchanged_and_remove_deleted(tmp_path: Path) -> None:
    destination = tmp_path / "release-assets"
    old: dict[str, Any] = {
        "path": "1/10-unchanged.zip",
        "name": "unchanged.zip",
        "size": 3,
        "updated_at": "2026-01-01T00:00:00Z",
        "release_id": 1,
    }
    deleted = {**old, "path": "1/11-deleted.zip", "name": "deleted.zip"}
    (destination / "1").mkdir(parents=True)
    (destination / old["path"]).write_bytes(b"old")
    (destination / deleted["path"]).write_bytes(b"bye")
    (destination / "releases.json").write_text(
        json.dumps({"assets": {"10": old, "11": deleted}})
    )
    releases = [
        [
            {
                "id": 1,
                "assets": [
                    {
                        "id": 10,
                        **{key: old[key] for key in ("name", "size", "updated_at")},
                    },
                    {
                        "id": 12,
                        "name": "new.zip",
                        "size": 3,
                        "updated_at": "2026-02-01T00:00:00Z",
                        "url": "unused",
                    },
                ],
            }
        ]
    ]

    def download(
        _args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        cast(BinaryIO, kwargs["output"]).write(b"new")
        return subprocess.CompletedProcess([], 0, "", "")

    with (
        patch(
            "restic_backups.github_repository.workflow._gh_json", return_value=releases
        ),
        patch(
            "restic_backups.github_repository.workflow._run", side_effect=download
        ) as run,
    ):
        assert workflow._releases("example", "repo", destination, {}) == "updated"
    assert run.call_count == 1
    assert (destination / "1/10-unchanged.zip").read_bytes() == b"old"
    assert (destination / "1/12-new.zip").read_bytes() == b"new"
    assert not (destination / "1/11-deleted.zip").exists()


def test_component_failure_is_manifested_and_other_destinations_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "config.yaml"))
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    job = github_job(git=True, metadata=True)
    job["github"]["authentication"] = {  # type: ignore[index]
        "git": {
            "ssh": {
                "private-key": {"env": "MISSING_SSH_KEY"},
                "known-hosts": {"env": "MISSING_KNOWN_HOSTS"},
            }
        },
        "api": {"token": {"env": "GITHUB_TOKEN"}},
    }

    def metadata(_owner: str, _name: str, path: Path, *_: object) -> str:
        path.mkdir(parents=True)
        return "updated"

    with (
        patch(
            "restic_backups.github_repository.workflow._git_mirror",
            side_effect=BackupError("git failed"),
        ),
        patch(
            "restic_backups.github_repository.workflow._metadata",
            side_effect=metadata,
        ),
        patch(
            "restic_backups.github_repository.workflow.restic.command",
            side_effect=[1, 0],
        ) as restic_command,
    ):
        statuses, destinations = workflow.backup(
            "example-repository",
            job,
            ["first", "second"],
            {},
            {},
            {"example-repository": job},
        )

    assert statuses["git"] == "failed"
    assert statuses["metadata"] == "updated"
    assert destinations == {"first": False, "second": True}
    assert restic_command.call_count == 2
    manifest = json.loads(
        (workflow.data_dir("example-repository") / "backup-manifest.json").read_text()
    )
    assert manifest["components"]["git"]["status"] == "failed"


def test_secret_values_never_enter_audit_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(audit.AUDIT_ENV, "1")
    monkeypatch.setenv("HTTPS_TOKEN", "top-secret-value")
    github = {"authentication": {"git": {"https": {"token": {"env": "HTTPS_TOKEN"}}}}}
    completed = subprocess.CompletedProcess([], 0, "ok", "")
    with (
        workflow.authentication(github) as env,
        patch(
            "restic_backups.github_repository.workflow.subprocess.run",
            return_value=completed,
        ),
    ):
        workflow._run(["git", "fetch"], env=env)
    contents = (tmp_path / "audit-log.json").read_text()
    assert "top-secret-value" not in contents
    assert '"command":"git"' in contents

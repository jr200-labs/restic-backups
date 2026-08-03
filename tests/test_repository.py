"""Configuration and repository regression checks."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from restic_backups import config
from restic_backups.errors import BackupError
from restic_backups.generic import repository, restic
from restic_backups.jobs import workflow as job_workflow


class ConfigLoadingTest(unittest.TestCase):
    def test_files_job_snapshots_every_selected_repository(self) -> None:
        job = {"type": "files", "source": {"paths": ["~/Documents"]}}
        with patch(
            "restic_backups.jobs.workflow.restic.command", return_value=0
        ) as run:
            states, destinations = job_workflow.run(
                "documents",
                job,
                ["first", "second"],
                {},
                {},
                {"documents": job},
                dry_run=True,
            )

        self.assertEqual(states, {"files": "planned"})
        self.assertEqual(destinations, {"first": True, "second": True})
        self.assertEqual(
            [item.kwargs["repository_id"] for item in run.call_args_list],
            ["first", "second"],
        )
        self.assertIn("--dry-run", run.call_args.args[1])

    def test_jobs_are_typed_and_legacy_backups_are_normalized(self) -> None:
        config_data: dict[str, Any] = {
            "storage": [{"id": "disk", "type": "local", "path": "/Volumes/disk"}],
            "restic-repositories": [
                {
                    "id": "repo",
                    "storage-id": "disk",
                    "enabled": False,
                    "path": "restic/repo",
                    "password": "CHANGE_ME",
                }
            ],
            "jobs": [
                {
                    "job-id": "documents",
                    "type": "files",
                    "restic-repository-ids": ["repo"],
                    "source": {"paths": ["~/Documents"]},
                },
                {
                    "job-id": "memos",
                    "type": "voice-memos",
                    "restic-repository-ids": ["repo"],
                    "source": {},
                },
            ],
        }

        _, _, jobs = config.validate(config_data)
        self.assertEqual(set(jobs), {"documents", "memos"})
        self.assertEqual(jobs["documents"]["source"]["paths"], ["~/Documents"])

        config_data["backups"] = []
        with self.assertRaisesRegex(config.ConfigError, "both jobs and backups"):
            config.validate(config_data)

        del config_data["jobs"]
        config_data["backups"] = [
            {
                "job-id": "legacy",
                "restic-repository-ids": ["repo"],
                "paths": ["/data"],
            }
        ]
        _, _, jobs = config.validate(config_data)
        self.assertEqual(jobs["legacy"]["type"], "files")
        self.assertEqual(jobs["legacy"]["source"], {"paths": ["/data"]})

    def test_available_commands_come_from_restic_help(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            stdout="""Available Commands:
  backup        Create a new backup
  snapshots     List all snapshots

Additional Commands:
  version       Print version
""",
            stderr="",
        )
        with patch("subprocess.run", return_value=result):
            self.assertEqual(
                restic.available_commands(),
                [
                    ("backup", "Create a new backup"),
                    ("snapshots", "List all snapshots"),
                ],
            )

    def test_command_usage_comes_from_restic_help(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            stdout="""The list command lists repository objects.

Usage:
  restic list [flags] [blobs|index|keys|locks|packs|snapshots]
""",
            stderr="",
        )
        with patch("subprocess.run", return_value=result):
            self.assertEqual(
                restic.command_usage("list"),
                "restic list [flags] [blobs|index|keys|locks|packs|snapshots]",
            )

    def test_data_dir_is_relative_to_config_not_installed_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            backend = {"id": "b2", "type": "s3"}
            restic_repository = {
                "id": "store",
                "bucket": "bucket",
                "key_prefix": "private/voice-memos",
            }
            with patch.dict(os.environ, {config.CONFIG_ENV: str(config_path)}):
                self.assertEqual(
                    repository.data_dir("voice-memos", restic_repository, backend),
                    Path(directory).resolve()
                    / "data/b2/bucket/private/voice-memos/voice-memos",
                )
                self.assertEqual(
                    repository.cache_dir({"cache-dir": ".restic-cache/store"}),
                    Path(directory).resolve() / ".restic-cache/store",
                )

    def test_plain_yaml_and_sops_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("value: plain\n")
            self.assertEqual(config.load_config(path, False), {"value": "plain"})

            result = type("Result", (), {"stdout": json.dumps({"value": "sops"})})()
            with patch("subprocess.run", return_value=result) as run:
                self.assertEqual(config.load_config(path, True), {"value": "sops"})
                run.assert_called_once_with(
                    ["sops", "--decrypt", "--output-type", "json", path],
                    check=True,
                    capture_output=True,
                    text=True,
                )

    def test_s3_repository(self) -> None:
        config_data: dict[str, Any] = {
            "storage": [
                {
                    "id": "b2",
                    "type": "s3",
                    "endpoint": "https://s3.us-west-004.backblazeb2.com",
                    "region": "us-west-004",
                    "credentials": {
                        "access-key-id": "key-id",
                        "secret-access-key": "secret",
                    },
                }
            ],
            "restic-repositories": [
                {
                    "id": "store",
                    "storage-id": "b2",
                    "enabled": True,
                    "bucket": "bucket",
                    "key_prefix": "voice-memos",
                    "password": "password",
                    "cache-dir": "/tmp/restic-cache",
                }
            ],
            "backups": [{"job-id": "voice-memos", "restic-repository-id": "store"}],
        }
        storage, repositories, backups = config.validate(config_data)
        result: subprocess.CompletedProcess[str] = subprocess.CompletedProcess([], 0)
        with patch("subprocess.run", return_value=result) as run:
            self.assertEqual(
                restic.command(
                    "voice-memos", ["snapshots"], storage, repositories, backups
                ),
                0,
            )
        self.assertEqual(
            run.call_args.args[0],
            ["restic", "-o", "s3.region=us-west-004", "snapshots"],
        )
        environment = run.call_args.kwargs["env"]
        self.assertEqual(
            environment["RESTIC_REPOSITORY"],
            "s3:https://s3.us-west-004.backblazeb2.com/bucket/voice-memos",
        )
        self.assertEqual(environment["RESTIC_CACHE_DIR"], "/tmp/restic-cache")

        repositories["store"]["archive"] = {
            "storage-class": "GLACIER_IR",
            "restore": None,
        }
        with patch("subprocess.run", return_value=result) as run:
            restic.command(
                "voice-memos", ["backup", "/tmp/source"], storage, repositories, backups
            )
        self.assertIn("s3.storage-class=GLACIER_IR", run.call_args.args[0])
        self.assertEqual(
            run.call_args.args[0][-4:],
            ["backup", "--tag", "voice-memos", "/tmp/source"],
        )

    def test_local_repository_requires_mounted_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = {"id": "disk", "type": "local", "path": directory}
            restic_repository = {
                "id": "local",
                "storage-id": "disk",
                "enabled": True,
                "path": "restic/personal",
                "password": "password",
            }
            result: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
                [], 0
            )
            with patch("subprocess.run", return_value=result) as run:
                restic.repository_command(restic_repository, storage, ["init"])
            self.assertEqual(run.call_args.args[0], ["restic", "init"])
            self.assertEqual(
                run.call_args.kwargs["env"]["RESTIC_REPOSITORY"],
                str(Path(directory).resolve() / "restic/personal"),
            )

        with self.assertRaisesRegex(BackupError, "not mounted"):
            restic.repository_command(restic_repository, storage, ["snapshots"])

    def test_restic_output_uses_python_logging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = {"id": "disk", "type": "local", "path": directory}
            restic_repository = {
                "id": "local",
                "storage-id": "disk",
                "enabled": True,
                "path": "restic/personal",
                "password": "password",
            }
            result = subprocess.CompletedProcess(
                [], 0, stdout="snapshot saved\n", stderr="repository opened\n"
            )
            with (
                patch("subprocess.run", return_value=result),
                self.assertLogs("restic_backups.generic.restic", level="INFO") as logs,
            ):
                restic.repository_command(restic_repository, storage, ["backup"])

        self.assertIn("local: restic stdout: snapshot saved", logs.output[0])
        self.assertIn("local: restic stderr: repository opened", logs.output[1])

    def test_invalid_repository_references_and_paths(self) -> None:
        config_data: dict[str, Any] = {
            "storage": [{"id": "disk", "type": "local", "path": "/Volumes/disk"}],
            "restic-repositories": [
                {
                    "id": "local",
                    "storage-id": "disk",
                    "enabled": False,
                    "path": "../escape",
                    "password": "CHANGE_ME",
                }
            ],
            "backups": [{"job-id": "files", "restic-repository-id": "missing"}],
        }
        with self.assertRaisesRegex(config.ConfigError, "safe relative path"):
            config.validate(config_data)
        config_data["restic-repositories"][0]["path"] = "restic/files"
        with self.assertRaisesRegex(config.ConfigError, "unknown restic repository"):
            config.validate(config_data)

    def test_backup_can_reference_multiple_repositories(self) -> None:
        config_data: dict[str, Any] = {
            "storage": [{"id": "disk", "type": "local", "path": "/Volumes/disk"}],
            "restic-repositories": [
                {
                    "id": repository_id,
                    "storage-id": "disk",
                    "enabled": False,
                    "path": f"restic/{repository_id}",
                    "password": "CHANGE_ME",
                }
                for repository_id in ("first", "second")
            ],
            "backups": [
                {
                    "job-id": "files",
                    "restic-repository-ids": ["first", "second"],
                    "paths": ["/data"],
                }
            ],
        }

        _, _, backups = config.validate(config_data)
        self.assertEqual(
            config.backup_repository_ids(backups["files"], "files"),
            ["first", "second"],
        )

    def test_repository_readiness_is_checked_at_execution(self) -> None:
        config_data: dict[str, Any] = {
            "storage": [{"id": "disk", "type": "local", "path": "/Volumes/unfinished"}],
            "restic-repositories": [
                {
                    "id": "unfinished",
                    "storage-id": "disk",
                    "enabled": True,
                    "path": "personal",
                    "password": "CHANGE_ME",
                }
            ],
            "backups": [{"job-id": "files", "restic-repository-id": "unfinished"}],
        }
        with self.assertRaisesRegex(config.ConfigError, "contains placeholders"):
            config.validate(config_data)

        storage, repositories, _ = config.validate(
            config_data, check_placeholders=False
        )
        with (
            patch("subprocess.run") as run,
            self.assertRaisesRegex(BackupError, "contains placeholders"),
        ):
            restic.repository_command(
                repositories["unfinished"], storage["disk"], ["init"]
            )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

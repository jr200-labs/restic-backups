"""Configuration loading regression checks."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from restic_backups import config
from restic_backups.generic import repository, restic


class ConfigLoadingTest(unittest.TestCase):
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

    def test_data_dir_is_relative_to_config_not_installed_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            store = {
                "id": "store",
                "bucket": "bucket",
                "key_prefix": "private/voice-memos",
            }
            with patch.dict(os.environ, {config.CONFIG_ENV: str(config_path)}):
                self.assertEqual(
                    repository.data_dir("voice-memos", store),
                    Path(directory).resolve()
                    / "data/store/bucket/private/voice-memos/voice-memos",
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

    def test_s3_store_without_archive_policy(self) -> None:
        config_data: dict[str, Any] = {
            "credentials": [
                {
                    "id": "b2",
                    "access-key-id": "key-id",
                    "secret-access-key": "secret",
                }
            ],
            "restic-stores": [
                {
                    "id": "store",
                    "credentials-id": "b2",
                    "enabled": True,
                    "endpoint": "https://s3.us-west-004.backblazeb2.com",
                    "region": "us-west-004",
                    "bucket": "bucket",
                    "key_prefix": "voice-memos",
                    "password": "password",
                    "cache-dir": "/tmp/restic-cache",
                }
            ],
            "backups": [{"id": "voice-memos", "restic-store-id": "store"}],
        }
        credentials, stores, backups = config.validate(config_data)
        result: subprocess.CompletedProcess[str] = subprocess.CompletedProcess([], 0)
        with patch("subprocess.run", return_value=result) as run:
            self.assertEqual(
                restic.command(
                    "voice-memos", ["snapshots"], credentials, stores, backups
                ),
                0,
            )
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(
            command, ["restic", "-o", "s3.region=us-west-004", "snapshots"]
        )
        self.assertEqual(
            environment["RESTIC_REPOSITORY"],
            "s3:https://s3.us-west-004.backblazeb2.com/bucket/voice-memos",
        )
        self.assertEqual(environment["RESTIC_CACHE_DIR"], "/tmp/restic-cache")

        result.stdout = "[]"
        with patch("subprocess.run", return_value=result) as run:
            self.assertEqual(
                restic.command_output(
                    "voice-memos",
                    ["snapshots", "--json"],
                    credentials,
                    stores,
                    backups,
                ),
                "[]",
            )
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.PIPE)

        stores["store"]["archive"] = {
            "storage-class": "GLACIER_IR",
            "restore": None,
        }
        credentials, stores, backups = config.validate(config_data)
        with patch("subprocess.run", return_value=result) as run:
            restic.command(
                "voice-memos", ["backup", "/tmp/source"], credentials, stores, backups
            )
        self.assertIn("s3.storage-class=GLACIER_IR", run.call_args.args[0])
        self.assertEqual(
            run.call_args.args[0][-4:],
            ["backup", "--tag", "voice-memos", "/tmp/source"],
        )

        config_data["backups"][0]["tag"] = 42
        with self.assertRaisesRegex(config.ConfigError, "voice-memos.tag"):
            config.validate(config_data)

        config_data["backups"][0]["tag"] = "voice-memos"
        config_data["backups"][0]["paths"] = []
        with self.assertRaisesRegex(config.ConfigError, "voice-memos.paths"):
            config.validate(config_data)

        config_data["backups"][0]["paths"] = ["/tmp/source"]
        config_data["restic-stores"][0]["cache-dir"] = 42
        with self.assertRaisesRegex(config.ConfigError, "store.cache-dir"):
            config.validate(config_data)


if __name__ == "__main__":
    unittest.main()

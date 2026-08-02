"""Small command-surface regression check."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from click import unstyle
from click.testing import CliRunner as ClickCliRunner
from typer.testing import CliRunner

from restic_backups.cli import app
from restic_backups.generic.cli import forget_command, init_command, run_args
from restic_backups.generic.cli import menu as generic_menu
from restic_backups.voice_memos.cli import cli as voice_memos_cli


class VoiceMemosCliTest(unittest.TestCase):
    def test_root_and_generic_help_expose_subcommands(self) -> None:
        runner = CliRunner()
        root = runner.invoke(app, ["--help"])
        self.assertEqual(root.exit_code, 0, root.output)
        root_help = unstyle(root.output)
        self.assertIn("generic", root_help)
        self.assertIn("voice-memos", root_help)
        self.assertIn("--verbose", root_help)

        generic = runner.invoke(app, ["generic", "--help"])
        self.assertEqual(generic.exit_code, 0, generic.output)
        generic_help = unstyle(generic.output)
        for command in ("repository", "backup", "snapshot", "restic"):
            self.assertIn(command, generic_help)
        for group, commands in {
            "repository": ("list", "init", "prime-cache", "destroy"),
            "backup": ("list", "run", "data-dir"),
            "snapshot": ("list", "forget"),
            "restic": ("run",),
        }.items():
            result = runner.invoke(app, ["generic", group, "--help"])
            self.assertEqual(result.exit_code, 0, result.output)
            help_text = unstyle(result.output)
            for command in commands:
                self.assertIn(command, help_text)

    def test_help_exposes_workflows(self) -> None:
        result = ClickCliRunner().invoke(voice_memos_cli, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        for command in ("backup", "transcribe", "diarize-parallel", "restore"):
            self.assertIn(command, result.output)

    def test_voice_memos_help_does_not_require_config(self) -> None:
        result = CliRunner().invoke(app, ["voice-memos", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)

    @patch("restic_backups.generic.cli.sys.stdin.isatty", return_value=True)
    @patch("restic_backups.generic.cli.questionary.select")
    @patch("restic_backups.generic.cli.repository_list_command")
    def test_generic_menu_selects_a_command(self, list_command, select, _) -> None:
        select.return_value.ask.side_effect = ["repository", "list"]

        generic_menu(Mock(invoked_subcommand=None))

        list_command.assert_called_once_with()

    def test_config_path_option_and_environment_variable(self) -> None:
        config = """\
credentials:
  - id: b2
    access-key-id: CHANGE_ME
    secret-access-key: CHANGE_ME
restic-stores:
  - id: store
    credentials-id: b2
    enabled: false
    endpoint: CHANGE_ME
    region: CHANGE_ME
    bucket: CHANGE_ME
    key_prefix: CHANGE_ME
    password: CHANGE_ME
backups:
  - id: voice-memos
    restic-store-id: store
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(config)
            runner = CliRunner()
            for args, env in (
                (["--config", str(path), "check-config"], None),
                (["check-config"], {"RESTIC_BACKUPS_CONFIG": str(path)}),
            ):
                result = runner.invoke(app, args, env=env)
                self.assertEqual(result.exit_code, 0, result.output)

    def test_forget_prunes_selected_tagged_snapshot(self) -> None:
        credentials: dict[str, dict[str, object]] = {"credentials": {}}
        stores = {"store": {"enabled": True}}
        backups = {"backup": {"restic-store-id": "store", "tag": "documents"}}
        snapshot_id = "a" * 64
        with (
            patch("restic_backups.generic.cli.sys.stdin.isatty", return_value=True),
            patch(
                "restic_backups.generic.cli.validated",
                return_value=({}, credentials, stores, backups),
            ),
            patch(
                "restic_backups.generic.cli.restic.command_output",
                return_value=json.dumps(
                    [
                        {
                            "id": snapshot_id,
                            "short_id": "aaaaaaaa",
                            "time": "2026-08-01T12:00:00Z",
                            "hostname": "host",
                            "paths": ["/data"],
                        }
                    ]
                ),
            ) as command_output,
            patch("restic_backups.generic.cli.questionary.select") as select,
            patch("restic_backups.generic.cli.questionary.confirm") as confirm,
            patch(
                "restic_backups.generic.cli.restic.command", return_value=0
            ) as command,
        ):
            select.return_value.ask.return_value = snapshot_id
            confirm.return_value.ask.return_value = True

            forget_command("backup")

        command_output.assert_called_once_with(
            "backup",
            ["snapshots", "--tag", "documents", "--json"],
            credentials,
            stores,
            backups,
        )
        command.assert_called_once_with(
            "backup",
            ["forget", snapshot_id, "--prune"],
            credentials,
            stores,
            backups,
        )

    def test_snapshots_lists_configured_tag_as_table(self) -> None:
        credentials: dict[str, dict[str, object]] = {"credentials": {}}
        stores = {"store": {"enabled": True}}
        backups = {"documents": {"restic-store-id": "store", "tag": "files"}}
        with (
            patch(
                "restic_backups.generic.cli.validated",
                return_value=({}, credentials, stores, backups),
            ),
            patch(
                "restic_backups.generic.cli.restic.command_output",
                return_value=json.dumps(
                    [
                        {
                            "id": "a" * 64,
                            "short_id": "aaaaaaaa",
                            "time": "2026-08-02T12:00:00Z",
                            "hostname": "laptop",
                            "paths": ["/data/documents"],
                            "tags": ["files"],
                        }
                    ]
                ),
            ) as command_output,
        ):
            result = CliRunner().invoke(
                app, ["generic", "snapshot", "list", "documents"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        for value in ("Snapshots: documents", "aaaaaaaa", "laptop", "files"):
            self.assertIn(value, result.output)
        command_output.assert_called_once_with(
            "documents",
            ["snapshots", "--tag", "files", "--json"],
            credentials,
            stores,
            backups,
        )

    def test_generic_backup_uses_configured_paths(self) -> None:
        credentials: dict[str, dict[str, object]] = {"credentials": {}}
        stores = {"store": {"enabled": True}}
        backups = {
            "documents": {
                "restic-store-id": "store",
                "paths": ["~/Documents", "/tmp/example"],
            }
        }
        with (
            patch(
                "restic_backups.generic.cli.validated",
                return_value=({}, credentials, stores, backups),
            ),
            patch(
                "restic_backups.generic.cli.restic.command", return_value=0
            ) as command,
        ):
            result = CliRunner().invoke(app, ["generic", "backup", "run", "documents"])

        self.assertEqual(result.exit_code, 0, result.output)
        command.assert_called_once_with(
            "documents",
            ["backup", str(Path("~/Documents").expanduser()), "/tmp/example"],
            credentials,
            stores,
            backups,
        )

    @patch("restic_backups.generic.cli.restic.store_command", return_value=0)
    @patch("restic_backups.generic.cli.validated")
    def test_prime_cache_checks_repository_with_cache(self, validated, command) -> None:
        credential = {"id": "credentials"}
        store = {
            "id": "store",
            "enabled": True,
            "credentials-id": "credentials",
        }
        validated.return_value = (
            {},
            {"credentials": credential},
            {"store": store},
            {},
        )

        result = CliRunner().invoke(
            app, ["generic", "repository", "prime-cache", "store"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("store: cache primed", result.output)
        command.assert_called_once_with(store, credential, ["check", "--with-cache"])

    def test_advanced_restic_can_print_without_running(self) -> None:
        credentials: dict[str, dict[str, object]] = {"credentials": {}}
        stores = {"store": {"enabled": True}}
        backups = {"documents": {"restic-store-id": "store"}}
        with (
            patch(
                "restic_backups.generic.cli.validated",
                return_value=({}, credentials, stores, backups),
            ),
            patch.dict(
                "os.environ",
                {
                    "RESTIC_BACKUPS_CONFIG": "/tmp/config.sops.yaml",
                    "RESTIC_BACKUPS_SOPS": "1",
                },
            ),
            patch("restic_backups.generic.cli.questionary.select") as select,
            patch("restic_backups.generic.cli.console.print") as print_line,
            patch("restic_backups.generic.cli.restic.command") as command,
        ):
            select.return_value.ask.return_value = "print"
            run_args("documents", ["list", "snapshots"], interactive=True)

        command.assert_not_called()
        output = "\n".join(str(item.args[0]) for item in print_line.call_args_list)
        self.assertIn(
            f"uv run restic-backups --config {Path('/tmp/config.sops.yaml').resolve()} "
            "--sops generic restic run --backup documents list snapshots",
            output,
        )

    @patch("restic_backups.generic.cli.restic.store_command")
    @patch("restic_backups.generic.cli.validated")
    def test_init_skips_existing_repositories(self, validated, command) -> None:
        credentials: dict[str, dict[str, object]] = {
            "credentials": {"id": "credentials"}
        }
        stores = {
            "existing": {
                "id": "existing",
                "enabled": True,
                "credentials-id": "credentials",
                "endpoint": "https://existing.example.com",
            },
            "new": {
                "id": "new",
                "enabled": True,
                "credentials-id": "credentials",
                "endpoint": "https://new.example.com",
            },
        }
        backups = {"first": {"restic-store-id": "existing"}}
        validated.return_value = ({}, credentials, stores, backups)
        command.side_effect = [0, 10, 0]
        runner = CliRunner()

        repositories = runner.invoke(app, ["generic", "repository", "list"])
        self.assertEqual(repositories.exit_code, 0, repositories.output)
        for text in ("Repositories", "existing", "new"):
            self.assertIn(text, repositories.output)

        configured_backups = runner.invoke(app, ["generic", "backup", "list"])
        self.assertEqual(configured_backups.exit_code, 0, configured_backups.output)
        for text in ("Backups", "first"):
            self.assertIn(text, configured_backups.output)

        legacy_list = runner.invoke(app, ["generic", "list"])
        self.assertEqual(legacy_list.exit_code, 0, legacy_list.output)
        for text in ("Repositories", "Backups"):
            self.assertIn(text, legacy_list.output)

        result = runner.invoke(app, ["generic", "repository", "init", "--all"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("existing: checking repository", result.output)
        self.assertIn("existing: already initialized; skipping", result.output)
        self.assertIn("new: not initialized; initializing", result.output)
        self.assertIn("new: initialized", result.output)
        self.assertEqual(
            command.call_args_list,
            [
                call(
                    stores["existing"],
                    credentials["credentials"],
                    ["cat", "config"],
                    quiet=True,
                ),
                call(
                    stores["new"],
                    credentials["credentials"],
                    ["cat", "config"],
                    quiet=True,
                ),
                call(stores["new"], credentials["credentials"], ["init"]),
            ],
        )

        command.reset_mock()
        command.side_effect = [0]
        result = runner.invoke(app, ["generic", "repository", "init", "existing"])
        self.assertEqual(result.exit_code, 0, result.output)
        command.assert_called_once_with(
            stores["existing"],
            credentials["credentials"],
            ["cat", "config"],
            quiet=True,
        )

    @patch("restic_backups.generic.cli.sys.stdin.isatty", return_value=True)
    @patch("restic_backups.generic.cli.questionary.select")
    @patch("restic_backups.generic.cli.restic.store_command", return_value=0)
    @patch("restic_backups.generic.cli.validated")
    def test_init_prompt_lists_all_first(self, validated, command, select, _) -> None:
        credential = {"id": "credentials"}
        store = {
            "id": "store",
            "enabled": True,
            "credentials-id": "credentials",
        }
        validated.return_value = (
            {},
            {"credentials": credential},
            {"store": store},
            {},
        )
        select.return_value.ask.return_value = "store"

        init_command()

        choices = select.call_args.kwargs["choices"]
        self.assertEqual(choices[0].title, "All repositories")
        command.assert_called_once_with(
            store, credential, ["cat", "config"], quiet=True
        )


if __name__ == "__main__":
    unittest.main()

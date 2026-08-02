"""Small command-surface regression check."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import questionary
from click import unstyle
from click.testing import CliRunner as ClickCliRunner
from typer.testing import CliRunner

from restic_backups.cli import app
from restic_backups.cli import interactive_menu as root_menu
from restic_backups.generic.cli import (
    choose_dry_run,
    choose_repositories,
    destroy_command,
    forget_command,
    init_command,
    repository_menu,
    restic_menu,
    run_args,
)
from restic_backups.generic.cli import menu as generic_menu
from restic_backups.voice_memos.cli import cli as voice_memos_cli
from restic_backups.voice_memos.cli import interactive_menu as voice_memos_menu


def choice_title(choice: questionary.Choice) -> str:
    title = choice.title
    return (
        "".join(part[1] for part in title) if isinstance(title, list) else title or ""
    )


class VoiceMemosCliTest(unittest.TestCase):
    def test_dry_run_is_a_spacebar_checkbox(self) -> None:
        with patch("restic_backups.generic.cli.questionary.checkbox") as checkbox:
            checkbox.return_value.unsafe_ask.return_value = ["dry-run"]

            self.assertTrue(choose_dry_run())

        self.assertIn("Space to toggle", checkbox.call_args.args[0])
        self.assertEqual(checkbox.call_args.kwargs["choices"][0].value, "dry-run")

    def test_backup_repositories_are_explicit_unchecked_choices(self) -> None:
        repositories = {
            "first": {"enabled": True},
            "second": {"enabled": True},
        }
        backups = {"documents": {"restic-repository-ids": ["first", "second"]}}
        with (
            patch("restic_backups.generic.cli.sys.stdin.isatty", return_value=True),
            patch("restic_backups.generic.cli.questionary.checkbox") as checkbox,
        ):
            checkbox.return_value.unsafe_ask.return_value = ["second"]
            selected = choose_repositories("documents", None, repositories, backups)

        self.assertEqual(selected, ["second"])
        choices = checkbox.call_args.kwargs["choices"][:-1]
        self.assertEqual([choice.value for choice in choices], ["first", "second"])
        self.assertTrue(all(choice.checked is False for choice in choices))

    def test_root_and_generic_help_expose_subcommands(self) -> None:
        runner = CliRunner()
        root = runner.invoke(app, ["--help"])
        self.assertEqual(root.exit_code, 0, root.output)
        root_help = unstyle(root.output)
        self.assertIn("generic", root_help)
        self.assertIn("github-repository", root_help)
        self.assertIn("voice-memos", root_help)
        self.assertIn("--verbose", root_help)

        generic = runner.invoke(app, ["generic", "--help"])
        self.assertEqual(generic.exit_code, 0, generic.output)
        generic_help = unstyle(generic.output)
        for command in ("repository", "backup", "snapshot", "restic"):
            self.assertIn(command, generic_help)
        for group, commands in {
            "repository": ("list", "init", "prime-cache", "prune", "destroy"),
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
        runner = CliRunner()
        for args in (["--help"], []):
            result = runner.invoke(app, ["voice-memos", *args])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("backup", result.output)

        root = runner.invoke(app, [])
        self.assertEqual(root.exit_code, 0, root.output)
        self.assertIn("generic", root.output)

    @patch("restic_backups.cli.generic_cli.interactive_menu")
    @patch("restic_backups.cli.select")
    def test_root_menu_selects_described_workflow(self, select, generic_menu) -> None:
        select.return_value.unsafe_ask.side_effect = ["generic", "exit"]

        root_menu()

        choices = select.call_args.kwargs["choices"]
        self.assertIn("Manage repositories", choice_title(choices[0]))
        self.assertIn("Git history", choice_title(choices[1]))
        self.assertIn("transcribe", choice_title(choices[2]))
        title = choices[0].title
        self.assertIsInstance(title, list)
        assert isinstance(title, list)
        self.assertNotEqual(title[0][0], title[1][0])
        self.assertIsInstance(choices[-1], questionary.Separator)
        self.assertEqual(choices[-1].title, " ")
        generic_menu.assert_called_once_with()

    @patch("restic_backups.generic.cli.sys.stdin.isatty", return_value=True)
    @patch("restic_backups.generic.cli.select")
    @patch("restic_backups.generic.cli.repository_list_command")
    def test_generic_menu_selects_a_command(self, list_command, select, _) -> None:
        select.return_value.unsafe_ask.side_effect = ["repository", "list", "back"]

        generic_menu(Mock(invoked_subcommand=None))

        list_command.assert_called_once_with()
        section_choices = select.call_args_list[0].kwargs["choices"]
        command_choices = select.call_args_list[1].kwargs["choices"]
        self.assertIn("List, initialize", choice_title(section_choices[0]))
        self.assertIn("storage destinations", choice_title(command_choices[0]))

    def test_voice_memos_menu_describes_and_prints_command(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "RESTIC_BACKUPS_CONFIG": "/tmp/config.sops.yaml",
                    "RESTIC_BACKUPS_SOPS": "1",
                },
            ),
            patch("restic_backups.voice_memos.cli.select") as select,
            patch("restic_backups.voice_memos.cli.questionary.text") as arguments,
            patch("restic_backups.voice_memos.cli.click.echo") as echo,
            patch.object(voice_memos_cli, "main") as main,
        ):
            select.return_value.unsafe_ask.side_effect = ["backup", "run", "print"]
            arguments.return_value.unsafe_ask.return_value = ""

            voice_memos_menu()

        choices = select.call_args_list[0].kwargs["choices"]
        backup_choice = next(choice for choice in choices if choice.value == "backup")
        self.assertIn("Back up recordings", choice_title(backup_choice))
        output = "\n".join(str(item.args[0]) for item in echo.call_args_list)
        self.assertIn("Usage:", output)
        self.assertIn(
            f"uv run restic-backups --config {Path('/tmp/config.sops.yaml').resolve()} "
            "--sops voice-memos backup",
            output,
        )
        main.assert_not_called()

    @patch("restic_backups.cli.generic_cli.print_typer_help")
    @patch("restic_backups.cli.select")
    def test_root_help_returns_to_root_menu(self, select, print_help) -> None:
        select.return_value.unsafe_ask.side_effect = ["help", "exit"]

        root_menu()

        print_help.assert_called_once_with(app, "restic-backups")

    def test_restic_help_does_not_load_configuration(self) -> None:
        with (
            patch(
                "restic_backups.generic.cli.restic.available_commands",
                return_value=[("list", "List objects in the repository")],
            ),
            patch(
                "restic_backups.generic.cli.restic.command_usage",
                return_value="restic list [flags] [objects]",
            ),
            patch(
                "restic_backups.generic.cli.restic.command_help",
                return_value="full restic list help",
            ) as command_help,
            patch("restic_backups.generic.cli.validated") as validated,
            patch("restic_backups.generic.cli.select") as select,
        ):
            select.return_value.unsafe_ask.side_effect = [
                "list",
                "help",
                "back",
                "back",
            ]

            restic_menu()

        command_help.assert_called_once_with("list")
        validated.assert_not_called()

    def test_voice_memos_help_does_not_prepare_configuration(self) -> None:
        before_run = Mock()
        with (
            patch("restic_backups.voice_memos.cli.select") as select,
            patch("restic_backups.voice_memos.cli.questionary.text") as arguments,
        ):
            select.return_value.unsafe_ask.side_effect = [
                "backup",
                "help",
                "back",
                "back",
            ]

            voice_memos_menu(before_run)

        before_run.assert_not_called()
        arguments.assert_not_called()

    def test_config_path_option_and_environment_variable(self) -> None:
        config = """\
storage:
  - id: b2
    type: s3
    endpoint: CHANGE_ME
    region: CHANGE_ME
    credentials:
      access-key-id: CHANGE_ME
      secret-access-key: CHANGE_ME
restic-repositories:
  - id: store
    storage-id: b2
    enabled: false
    bucket: CHANGE_ME
    key_prefix: CHANGE_ME
    password: CHANGE_ME
backups:
  - job-id: voice-memos
    restic-repository-id: store
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

    def test_init_validates_only_the_selected_repository(self) -> None:
        config = """\
storage:
  - id: aws
    type: s3
    endpoint: https://s3.example.com
    region: us-east-1
    credentials:
      access-key-id: key
      secret-access-key: secret
  - id: disk
    type: local
    path: /Volumes/unfinished
restic-repositories:
  - id: aws-ready
    storage-id: aws
    enabled: true
    bucket: backups
    key_prefix: restic
    password: password
  - id: disk-unfinished
    storage-id: disk
    enabled: true
    path: personal
    password: CHANGE_ME
backups:
  - job-id: documents
    restic-repository-id: aws-ready
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(config)
            runner = CliRunner()
            with patch(
                "restic_backups.generic.cli.restic.repository_command",
                return_value=10,
            ) as command:
                result = runner.invoke(
                    app,
                    [
                        "--config",
                        str(path),
                        "generic",
                        "repository",
                        "init",
                        "aws-ready",
                        "--dry-run",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("would initialize repository", result.output)
            command.assert_called_once()

            check = runner.invoke(app, ["--config", str(path), "check-config"])
            self.assertEqual(check.exit_code, 1, check.output)
            self.assertIn("disk-unfinished", check.output)

    def test_forget_prunes_selected_tagged_snapshot(self) -> None:
        storage: dict[str, dict[str, object]] = {"storage": {}}
        repositories = {"store": {"enabled": True}}
        backups = {"backup": {"restic-repository-id": "store", "tag": "documents"}}
        snapshot_id = "a" * 64
        with (
            patch("restic_backups.generic.cli.sys.stdin.isatty", return_value=True),
            patch(
                "restic_backups.generic.cli.validated",
                return_value=({}, storage, repositories, backups),
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
            patch("restic_backups.generic.cli.select") as select,
            patch("restic_backups.generic.cli.questionary.confirm") as confirm,
            patch(
                "restic_backups.generic.cli.restic.command", return_value=0
            ) as command,
        ):
            select.return_value.unsafe_ask.return_value = snapshot_id
            confirm.return_value.unsafe_ask.return_value = True

            forget_command("backup")
            forget_command("backup", dry_run=True)

        self.assertEqual(
            command_output.call_args_list,
            [
                call(
                    "backup",
                    ["snapshots", "--tag", "documents", "--json"],
                    storage,
                    repositories,
                    backups,
                    "store",
                ),
                call(
                    "backup",
                    ["snapshots", "--tag", "documents", "--json"],
                    storage,
                    repositories,
                    backups,
                    "store",
                ),
            ],
        )
        self.assertEqual(
            command.call_args_list,
            [
                call(
                    "backup",
                    ["forget", snapshot_id, "--prune"],
                    storage,
                    repositories,
                    backups,
                    repository_id="store",
                ),
                call(
                    "backup",
                    ["forget", snapshot_id, "--prune", "--dry-run"],
                    storage,
                    repositories,
                    backups,
                    repository_id="store",
                ),
            ],
        )
        confirm.assert_called_once_with(
            "Forget snapshot 'aaaaaaaa' and prune its unreferenced data?",
            default=False,
        )

    def test_snapshots_lists_configured_tag_as_table(self) -> None:
        storage: dict[str, dict[str, object]] = {"storage": {}}
        repositories = {"store": {"enabled": True}}
        backups = {"documents": {"restic-repository-id": "store", "tag": "files"}}
        with (
            patch(
                "restic_backups.generic.cli.validated",
                return_value=({}, storage, repositories, backups),
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
            storage,
            repositories,
            backups,
            "store",
        )

    def test_generic_backup_uses_configured_paths(self) -> None:
        storage: dict[str, dict[str, object]] = {"storage": {}}
        repositories = {"first": {"enabled": True}, "second": {"enabled": True}}
        backups = {
            "documents": {
                "restic-repository-ids": ["first", "second"],
                "paths": ["~/Documents", "/tmp/example"],
            }
        }
        with (
            patch(
                "restic_backups.generic.cli.validated",
                return_value=({}, storage, repositories, backups),
            ),
            patch(
                "restic_backups.generic.cli.restic.command", return_value=0
            ) as command,
        ):
            result = CliRunner().invoke(
                app,
                [
                    "generic",
                    "backup",
                    "run",
                    "documents",
                    "--repository",
                    "first",
                    "--repository",
                    "second",
                ],
            )
            dry_run = CliRunner().invoke(
                app,
                [
                    "generic",
                    "backup",
                    "run",
                    "documents",
                    "--repository",
                    "first",
                    "--repository",
                    "second",
                    "--dry-run",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(dry_run.exit_code, 0, dry_run.output)
        self.assertEqual(
            command.call_args_list,
            [
                call(
                    "documents",
                    ["backup", str(Path("~/Documents").expanduser()), "/tmp/example"],
                    storage,
                    repositories,
                    backups,
                    repository_id="first",
                ),
                call(
                    "documents",
                    ["backup", str(Path("~/Documents").expanduser()), "/tmp/example"],
                    storage,
                    repositories,
                    backups,
                    repository_id="second",
                ),
                call(
                    "documents",
                    [
                        "backup",
                        "--dry-run",
                        str(Path("~/Documents").expanduser()),
                        "/tmp/example",
                    ],
                    storage,
                    repositories,
                    backups,
                    repository_id="first",
                ),
                call(
                    "documents",
                    [
                        "backup",
                        "--dry-run",
                        str(Path("~/Documents").expanduser()),
                        "/tmp/example",
                    ],
                    storage,
                    repositories,
                    backups,
                    repository_id="second",
                ),
            ],
        )

    @patch("restic_backups.generic.cli.restic.repository_command", return_value=0)
    @patch("restic_backups.generic.cli.validated")
    def test_prime_cache_checks_repository_with_cache(self, validated, command) -> None:
        storage = {"id": "storage", "type": "s3"}
        restic_repository = {
            "id": "store",
            "enabled": True,
            "storage-id": "storage",
        }
        validated.return_value = (
            {},
            {"storage": storage},
            {"store": restic_repository},
            {},
        )

        result = CliRunner().invoke(
            app, ["generic", "repository", "prime-cache", "store"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("store: cache primed", result.output)
        command.assert_called_once_with(
            restic_repository, storage, ["check", "--with-cache"]
        )

    def test_repository_menu_compacts_small_packs_with_dry_run(self) -> None:
        with (
            patch("restic_backups.generic.cli.select") as select,
            patch("restic_backups.generic.cli.questionary.text") as size,
            patch("restic_backups.generic.cli.choose_dry_run", return_value=True),
            patch("restic_backups.generic.cli.prune_command") as prune,
        ):
            select.return_value.unsafe_ask.side_effect = ["prune", "small-packs"]
            size.return_value.unsafe_ask.return_value = "20M"

            repository_menu()

        prune.assert_called_once_with(None, dry_run=True, repack_smaller_than="20M")

    @patch("restic_backups.generic.cli.restic.repository_command", return_value=0)
    @patch("restic_backups.generic.cli.validated")
    def test_prune_passes_native_restic_options(self, validated, command) -> None:
        storage = {"id": "storage", "type": "s3"}
        restic_repository = {
            "id": "store",
            "enabled": True,
            "storage-id": "storage",
        }
        validated.return_value = (
            {},
            {"storage": storage},
            {"store": restic_repository},
            {},
        )

        result = CliRunner().invoke(
            app,
            [
                "generic",
                "repository",
                "prune",
                "store",
                "--max-unused",
                "unlimited",
                "--repack-cacheable-only",
                "--dry-run",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        command.assert_called_once_with(
            restic_repository,
            storage,
            [
                "prune",
                "--max-unused",
                "unlimited",
                "--repack-cacheable-only",
                "--dry-run",
            ],
        )

    def test_advanced_restic_can_print_without_running(self) -> None:
        storage: dict[str, dict[str, object]] = {"storage": {}}
        repositories = {"store": {"enabled": True}}
        backups = {"documents": {"restic-repository-id": "store"}}
        with (
            patch(
                "restic_backups.generic.cli.validated",
                return_value=({}, storage, repositories, backups),
            ),
            patch.dict(
                "os.environ",
                {
                    "RESTIC_BACKUPS_CONFIG": "/tmp/config.sops.yaml",
                    "RESTIC_BACKUPS_SOPS": "1",
                },
            ),
            patch("restic_backups.generic.cli.select") as select,
            patch("restic_backups.generic.cli.console.print") as print_line,
            patch("restic_backups.generic.cli.restic.command") as command,
        ):
            select.return_value.unsafe_ask.return_value = "print"
            run_args("documents", ["list", "snapshots"], interactive=True)

        command.assert_not_called()
        output = "\n".join(str(item.args[0]) for item in print_line.call_args_list)
        self.assertIn(
            f"uv run restic-backups --config {Path('/tmp/config.sops.yaml').resolve()} "
            "--sops generic restic run --backup documents --repository store list snapshots",
            output,
        )

    def test_advanced_restic_menu_adds_selected_dry_run(self) -> None:
        with (
            patch(
                "restic_backups.generic.cli.restic.available_commands",
                return_value=[("backup", "Create a new backup")],
            ),
            patch(
                "restic_backups.generic.cli.restic.command_usage",
                return_value="restic backup [flags] [files]",
            ),
            patch(
                "restic_backups.generic.cli.restic.supports_dry_run",
                return_value=True,
            ),
            patch(
                "restic_backups.generic.cli.choose_dry_run", return_value=True
            ) as dry_run,
            patch("restic_backups.generic.cli.select") as select,
            patch("restic_backups.generic.cli.questionary.text") as arguments,
            patch("restic_backups.generic.cli.run_args") as run,
        ):
            select.return_value.unsafe_ask.side_effect = ["backup", "run"]
            arguments.return_value.unsafe_ask.return_value = "/data"

            restic_menu()

        dry_run.assert_called_once_with()
        run.assert_called_once_with(
            None, ["backup", "--dry-run", "/data"], interactive=True
        )

    @patch("restic_backups.generic.cli.restic.repository_command")
    @patch("restic_backups.generic.cli.validated")
    def test_init_skips_existing_repositories(self, validated, command) -> None:
        storage = {
            "existing": {
                "id": "existing",
                "type": "s3",
                "endpoint": "https://existing.example.com",
            },
            "new": {
                "id": "new",
                "type": "s3",
                "endpoint": "https://new.example.com",
            },
        }
        repositories = {
            "existing": {
                "id": "existing",
                "enabled": True,
                "storage-id": "existing",
                "bucket": "bucket",
                "key_prefix": "existing",
            },
            "new": {
                "id": "new",
                "enabled": True,
                "storage-id": "new",
                "bucket": "bucket",
                "key_prefix": "new",
            },
        }
        backups = {"first": {"restic-repository-id": "existing"}}
        validated.return_value = ({}, storage, repositories, backups)
        command.side_effect = [0, 10, 0]
        runner = CliRunner()

        repository_list = runner.invoke(app, ["generic", "repository", "list"])
        self.assertEqual(repository_list.exit_code, 0, repository_list.output)
        for text in ("Repositories", "existing", "new"):
            self.assertIn(text, repository_list.output)

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
                    repositories["existing"],
                    storage["existing"],
                    ["cat", "config"],
                    quiet=True,
                ),
                call(
                    repositories["new"],
                    storage["new"],
                    ["cat", "config"],
                    quiet=True,
                ),
                call(repositories["new"], storage["new"], ["init"]),
            ],
        )

        command.reset_mock()
        command.side_effect = [0]
        result = runner.invoke(app, ["generic", "repository", "init", "existing"])
        self.assertEqual(result.exit_code, 0, result.output)
        command.assert_called_once_with(
            repositories["existing"],
            storage["existing"],
            ["cat", "config"],
            quiet=True,
        )

        command.reset_mock()
        command.side_effect = [10]
        result = runner.invoke(
            app, ["generic", "repository", "init", "new", "--dry-run"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("dry run; would initialize repository", result.output)
        command.assert_called_once_with(
            repositories["new"], storage["new"], ["cat", "config"], quiet=True
        )

    def test_destroy_dry_run_does_not_delete(self) -> None:
        restic_repository = {
            "id": "store",
            "enabled": True,
            "storage-id": "s3",
            "bucket": "bucket",
            "key_prefix": "restic",
        }
        storage = {
            "s3": {
                "id": "s3",
                "type": "s3",
                "endpoint": "https://s3.example.com",
            }
        }
        with (
            patch("restic_backups.generic.cli.sys.stdin.isatty", return_value=True),
            patch(
                "restic_backups.generic.cli.validated",
                return_value=({}, storage, {"store": restic_repository}, {}),
            ),
            patch("restic_backups.generic.cli.s3.delete_repository") as delete,
            patch("restic_backups.generic.cli.questionary.confirm") as confirm,
        ):
            destroy_command("store", dry_run=True)

        delete.assert_not_called()
        confirm.assert_not_called()

    @patch("restic_backups.generic.cli.sys.stdin.isatty", return_value=True)
    @patch("restic_backups.generic.cli.select")
    @patch("restic_backups.generic.cli.restic.repository_command", return_value=0)
    @patch("restic_backups.generic.cli.validated")
    def test_init_prompt_lists_all_first(self, validated, command, select, _) -> None:
        storage = {"id": "storage", "type": "s3"}
        restic_repository = {
            "id": "store",
            "enabled": True,
            "storage-id": "storage",
        }
        validated.return_value = (
            {},
            {"storage": storage},
            {"store": restic_repository},
            {},
        )
        select.return_value.unsafe_ask.return_value = "store"

        init_command()

        choices = select.call_args.kwargs["choices"]
        self.assertEqual(choice_title(choices[0]), "All repositories")
        command.assert_called_once_with(
            restic_repository, storage, ["cat", "config"], quiet=True
        )


if __name__ == "__main__":
    unittest.main()

"""Small command-surface regression check."""

import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner as ClickCliRunner
from restic_backups.cli import app
from restic_backups.voice_memos.cli import cli as voice_memos_cli
from typer.testing import CliRunner


class VoiceMemosCliTest(unittest.TestCase):
    def test_root_and_generic_help_expose_subcommands(self) -> None:
        runner = CliRunner()
        root = runner.invoke(app, ["--help"])
        self.assertEqual(root.exit_code, 0, root.output)
        self.assertIn("generic", root.output)
        self.assertIn("voice-memos", root.output)

        generic = runner.invoke(app, ["generic", "--help"])
        self.assertEqual(generic.exit_code, 0, generic.output)
        for command in ("list", "data-dir", "init", "run"):
            self.assertIn(command, generic.output)

    def test_help_exposes_workflows(self) -> None:
        result = ClickCliRunner().invoke(voice_memos_cli, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        for command in ("backup", "transcribe", "diarize-parallel", "restore"):
            self.assertIn(command, result.output)

    def test_voice_memos_help_does_not_require_config(self) -> None:
        result = CliRunner().invoke(app, ["voice-memos", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)

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


if __name__ == "__main__":
    unittest.main()

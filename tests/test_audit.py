import json
import stat
import subprocess
from unittest.mock import patch

from restic_backups import audit
from restic_backups import cli as root_cli
from restic_backups.generic import restic


def test_audit_appends_json_and_redacts_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(audit.AUDIT_ENV)
    monkeypatch.setattr(audit.socket, "gethostname", lambda: "backup-host")

    audit.record(
        "restic-backups",
        [
            "backup",
            "--password",
            "plain-password",
            "--token=plain-token",
            "AWS_SECRET_ACCESS_KEY=plain-key",
            "https://user:plain-url-password@example.com/repository",
            "/safe/secret-file",
        ],
    )
    audit.record("restic", ["snapshots"])

    events = [json.loads(line) for line in audit.AUDIT_LOG.read_text().splitlines()]
    assert len(events) == 2
    assert events[0] == {
        "args": [
            "backup",
            "--password",
            audit.REDACTED,
            f"--token={audit.REDACTED}",
            f"AWS_SECRET_ACCESS_KEY={audit.REDACTED}",
            f"https://user:{audit.REDACTED}@example.com/repository",
            "/safe/secret-file",
        ],
        "command": "restic-backups",
        "date-time": events[0]["date-time"],
        "hostname": "backup-host",
    }
    assert events[1]["command"] == "restic"
    assert stat.S_IMODE(audit.AUDIT_LOG.stat().st_mode) == 0o600


def test_audit_can_be_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(audit.AUDIT_ENV, "false")

    audit.record("restic-backups", ["check-config"])

    assert not audit.AUDIT_LOG.exists()


def test_installed_entrypoint_and_restic_log_exact_commands(monkeypatch) -> None:
    monkeypatch.setattr(root_cli.sys, "argv", ["restic-backups", "--help"])
    with (
        patch.object(root_cli.audit, "record") as record,
        patch.object(root_cli, "app") as app,
    ):
        root_cli.main()
    record.assert_called_once_with("restic-backups", ["--help"])
    app.assert_called_once_with()

    restic_repository = {
        "id": "store",
        "storage-id": "s3",
        "enabled": True,
        "bucket": "bucket",
        "key_prefix": "restic",
        "password": "repository-password",
    }
    storage = {
        "id": "s3",
        "type": "s3",
        "endpoint": "https://s3.example.com",
        "region": "region",
        "credentials": {
            "access-key-id": "access-key",
            "secret-access-key": "secret-key",
        },
    }
    result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with (
        patch.object(restic.audit, "record") as record,
        patch.object(restic.subprocess, "run", return_value=result),
    ):
        restic.repository_command(restic_repository, storage, ["snapshots", "--json"])

    record.assert_called_once_with(
        "restic", ["-o", "s3.region=region", "snapshots", "--json"]
    )

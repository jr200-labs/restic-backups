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

    first_id = audit.record_repository_write(
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
    audit.finish(first_id, True)

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
        "event": "started",
        "hostname": "backup-host",
        "id": first_id,
        "start-time": events[0]["start-time"],
    }
    assert events[1] == {
        "end-time": events[1]["end-time"],
        "event": "finished",
        "started-id": first_id,
        "successful": True,
    }
    assert stat.S_IMODE(audit.AUDIT_LOG.stat().st_mode) == 0o600


def test_audit_can_be_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(audit.AUDIT_ENV, "false")

    audit.finish(audit.record_repository_write("restic", ["backup", "/data"]), True)

    assert not audit.AUDIT_LOG.exists()


def test_only_mutating_restic_commands_are_audited(monkeypatch) -> None:
    monkeypatch.setattr(root_cli.sys, "argv", ["restic-backups", "--help"])
    with (
        patch.object(root_cli.audit, "finish_all") as finish_all,
        patch.object(root_cli, "app") as app,
    ):
        root_cli.main()
    finish_all.assert_called_once_with(True)
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
        patch.object(
            restic.audit, "record_repository_write", return_value="event-id"
        ) as record,
        patch.object(restic.audit, "finish") as finish,
        patch.object(restic.subprocess, "run", return_value=result),
    ):
        restic.repository_command(restic_repository, storage, ["snapshots", "--json"])
        record.assert_not_called()
        restic.repository_command(restic_repository, storage, ["backup", "/data"])

    record.assert_called_once_with(
        "restic", ["-o", "s3.region=region", "backup", "/data"]
    )
    assert finish.call_args_list[-1].args == ("event-id", True)
    assert restic.mutates_repository(["backup", "/data"])
    assert restic.mutates_repository(["key", "add"])
    assert not restic.mutates_repository(["key", "list"])
    assert not restic.mutates_repository(["prune", "--dry-run"])
    assert not restic.mutates_repository(["snapshots"])


def test_unfinished_event_identifies_an_interrupted_command(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(audit.AUDIT_ENV)
    event_id = audit.record_repository_write("restic-backups", ["job", "run", "photos"])

    event = json.loads(audit.AUDIT_LOG.read_text())
    assert event["event"] == "started"
    assert event["id"] == event_id
    audit.finish(event_id, False)

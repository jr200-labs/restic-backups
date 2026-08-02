import pytest

from restic_backups import audit


@pytest.fixture(autouse=True)
def disable_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(audit.AUDIT_ENV, "0")

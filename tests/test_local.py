from pathlib import Path

import pytest

from restic_backups.errors import BackupError
from restic_backups.generic import local


def test_local_destroy_removes_only_repository_directory(tmp_path: Path) -> None:
    storage = {"id": "disk", "type": "local", "path": str(tmp_path)}
    restic_repository = {"id": "repo", "path": "restic/repo"}
    repository_path = tmp_path / "restic/repo"
    repository_path.mkdir(parents=True)
    (repository_path / "config").write_text("restic")
    (tmp_path / "keep").write_text("keep")

    assert local.delete_repository(restic_repository, storage) == 1
    assert not repository_path.exists()
    assert (tmp_path / "keep").exists()

    restic_repository["path"] = "../outside"
    with pytest.raises(BackupError, match="safe and relative"):
        local.delete_repository(restic_repository, storage)


def test_local_destroy_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "disk"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BackupError, match="outside its storage root"):
        local.delete_repository(
            {"id": "local", "path": "linked/repository"},
            {"id": "disk", "type": "local", "path": str(root)},
        )

    assert outside.is_dir()

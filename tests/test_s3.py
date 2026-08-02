"""S3 repository deletion regression checks."""

import unittest
from unittest.mock import patch

import typer

from restic_backups.errors import BackupError
from restic_backups.generic import cli, s3


class S3DeletionTest(unittest.TestCase):
    @patch("restic_backups.generic.s3.boto3.client")
    def test_delete_repository_removes_versions_markers_and_objects(
        self, client
    ) -> None:
        s3_client = client.return_value
        s3_client.list_object_versions.side_effect = [
            {
                "Versions": [{"Key": "repo/config", "VersionId": "v1"}],
                "DeleteMarkers": [{"Key": "repo/config", "VersionId": "v2"}],
            },
            {},
        ]
        s3_client.list_objects_v2.side_effect = [
            {"Contents": [{"Key": "repo/data/pack"}]},
            {},
        ]
        s3_client.delete_objects.return_value = {}
        restic_repository = {
            "id": "repository",
            "bucket": "bucket",
            "key_prefix": "repo",
        }
        storage = {
            "endpoint": "https://s3.example.com",
            "region": "region",
            "credentials": {
                "access-key-id": "key",
                "secret-access-key": "secret",
            },
        }

        deleted = s3.delete_repository(restic_repository, storage)

        self.assertEqual(deleted, 3)
        self.assertEqual(s3_client.delete_objects.call_count, 2)

    @patch("restic_backups.generic.s3.boto3.client")
    def test_delete_repository_refuses_bucket_root(self, client) -> None:
        with self.assertRaisesRegex(BackupError, "entire S3 bucket"):
            s3.delete_repository({"key_prefix": "/"}, {})
        client.assert_not_called()

    def test_destroy_requires_exact_repository_id(self) -> None:
        restic_repository = {
            "id": "repository",
            "storage-id": "s3",
            "bucket": "bucket",
            "key_prefix": "repo",
            "password": "password",
        }
        storage = {
            "s3": {
                "id": "s3",
                "type": "s3",
                "endpoint": "https://s3.example.com",
                "region": "us-east-1",
                "credentials": {
                    "access-key-id": "key",
                    "secret-access-key": "secret",
                },
            }
        }
        with (
            patch("restic_backups.generic.cli.sys.stdin.isatty", return_value=True),
            patch(
                "restic_backups.generic.cli.validated",
                return_value=({}, storage, {"repository": restic_repository}, {}),
            ),
            patch("restic_backups.generic.cli.questionary.confirm") as confirm,
            patch("restic_backups.generic.cli.questionary.text") as text,
            patch("restic_backups.generic.cli.s3.delete_repository") as delete,
        ):
            confirm.return_value.unsafe_ask.return_value = True
            text.return_value.unsafe_ask.return_value = "wrong"

            with self.assertRaises(typer.Exit):
                cli.destroy_command("repository")

        delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()

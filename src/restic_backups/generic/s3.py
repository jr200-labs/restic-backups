"""Destructive S3 repository operations."""

from __future__ import annotations

import logging
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)

from ..errors import BackupError

logger = logging.getLogger(__name__)


def client(storage: dict[str, Any]) -> Any:
    credentials = storage["credentials"]
    return boto3.client(
        "s3",
        endpoint_url=storage["endpoint"],
        region_name=storage["region"],
        aws_access_key_id=credentials["access-key-id"],
        aws_secret_access_key=credentials["secret-access-key"],
    )


def is_initialized(restic_repository: dict[str, Any], storage: dict[str, Any]) -> bool:
    """Return whether S3 contains the repository's Restic config object."""
    key = f"{restic_repository['key_prefix'].strip('/')}/config"
    try:
        client(storage).head_object(Bucket=restic_repository["bucket"], Key=key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            return False
        raise BackupError(
            f"could not inspect repository '{restic_repository['id']}': {exc}"
        ) from exc
    except BotoCoreError as exc:
        raise BackupError(
            f"could not inspect repository '{restic_repository['id']}': {exc}"
        ) from exc
    return True


def delete_repository(
    restic_repository: dict[str, Any], storage: dict[str, Any]
) -> int:
    """Permanently delete every object and version in a repository prefix."""
    prefix = restic_repository["key_prefix"].strip("/")
    if not prefix:
        raise BackupError("refusing to delete an entire S3 bucket")
    prefix += "/"
    s3_client = client(storage)
    deleted = 0
    try:
        while True:
            page = s3_client.list_object_versions(
                Bucket=restic_repository["bucket"], Prefix=prefix, MaxKeys=1000
            )
            objects = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for section in ("Versions", "DeleteMarkers")
                for item in page.get(section, [])
            ]
            if not objects:
                break
            deleted += delete_batch(s3_client, restic_repository, objects)

        while True:
            page = s3_client.list_objects_v2(
                Bucket=restic_repository["bucket"], Prefix=prefix, MaxKeys=1000
            )
            objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if not objects:
                break
            deleted += delete_batch(s3_client, restic_repository, objects)
    except (BotoCoreError, ClientError) as exc:
        raise BackupError(
            f"could not delete repository '{restic_repository['id']}': {exc}"
        ) from exc
    return deleted


def delete_batch(
    client: Any, restic_repository: dict[str, Any], objects: list[dict[str, str]]
) -> int:
    logger.info(
        "%s: deleting %d objects or versions",
        restic_repository["id"],
        len(objects),
    )
    result = client.delete_objects(
        Bucket=restic_repository["bucket"],
        Delete={"Objects": objects, "Quiet": True},
    )
    errors = result.get("Errors", [])
    if errors:
        raise BackupError(
            f"could not fully delete repository '{restic_repository['id']}': {errors[0].get('Code', 'S3 error')}"
        )
    return len(objects)

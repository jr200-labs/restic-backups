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


def delete_repository(store: dict[str, Any], credential: dict[str, Any]) -> int:
    """Permanently delete every object and version in a repository prefix."""
    prefix = store["key_prefix"].strip("/")
    if not prefix:
        raise BackupError("refusing to delete an entire S3 bucket")
    prefix += "/"
    client = boto3.client(
        "s3",
        endpoint_url=store["endpoint"],
        region_name=store["region"],
        aws_access_key_id=credential["access-key-id"],
        aws_secret_access_key=credential["secret-access-key"],
    )
    deleted = 0
    try:
        while True:
            page = client.list_object_versions(
                Bucket=store["bucket"], Prefix=prefix, MaxKeys=1000
            )
            objects = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for section in ("Versions", "DeleteMarkers")
                for item in page.get(section, [])
            ]
            if not objects:
                break
            deleted += delete_batch(client, store, objects)

        while True:
            page = client.list_objects_v2(
                Bucket=store["bucket"], Prefix=prefix, MaxKeys=1000
            )
            objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if not objects:
                break
            deleted += delete_batch(client, store, objects)
    except (BotoCoreError, ClientError) as exc:
        raise BackupError(
            f"could not delete repository '{store['id']}': {exc}"
        ) from exc
    return deleted


def delete_batch(
    client: Any, store: dict[str, Any], objects: list[dict[str, str]]
) -> int:
    logger.info("%s: deleting %d objects or versions", store["id"], len(objects))
    result = client.delete_objects(
        Bucket=store["bucket"], Delete={"Objects": objects, "Quiet": True}
    )
    errors = result.get("Errors", [])
    if errors:
        raise BackupError(
            f"could not fully delete repository '{store['id']}': {errors[0].get('Code', 'S3 error')}"
        )
    return len(objects)

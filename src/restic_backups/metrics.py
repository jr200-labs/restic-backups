"""Optional Prometheus Pushgateway metrics for batch jobs."""

from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Mapping

from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

PUSHGATEWAY_ENV = "RESTIC_BACKUPS_PROMETHEUS_PUSHGATEWAY_URL"
logger = logging.getLogger(__name__)


def record_job(
    job_id: str,
    job_type: str,
    successful: bool,
    duration_seconds: float,
    destinations: Mapping[str, bool],
    *,
    dry_run: bool = False,
) -> None:
    """Push the latest result when a Pushgateway URL is configured."""
    gateway = os.getenv(PUSHGATEWAY_ENV)
    if not gateway:
        return

    registry = CollectorRegistry()
    labels = ("job_type", "dry_run")
    values = (job_type, str(dry_run).lower())
    Gauge(
        "restic_backups_job_last_run_timestamp_seconds",
        "Unix timestamp of the latest completed restic-backups job run.",
        labels,
        registry=registry,
    ).labels(*values).set(time.time())
    Gauge(
        "restic_backups_job_last_run_duration_seconds",
        "Duration of the latest completed restic-backups job run.",
        labels,
        registry=registry,
    ).labels(*values).set(duration_seconds)
    Gauge(
        "restic_backups_job_last_run_success",
        "Whether the latest restic-backups job run succeeded (1 or 0).",
        labels,
        registry=registry,
    ).labels(*values).set(successful)
    destination_metric = Gauge(
        "restic_backups_job_repository_success",
        "Whether the latest job run succeeded for a destination repository (1 or 0).",
        (*labels, "repository_id"),
        registry=registry,
    )
    for repository_id, result in destinations.items():
        destination_metric.labels(*values, repository_id).set(result)

    try:
        push_to_gateway(
            gateway,
            job="restic-backups",
            grouping_key={"instance": socket.gethostname(), "job_id": job_id},
            registry=registry,
            timeout=5,
        )
    except (OSError, ValueError) as exc:
        logger.warning("Could not push Prometheus metrics: %s", exc)

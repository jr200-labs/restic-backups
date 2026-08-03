from prometheus_client import generate_latest

from restic_backups import metrics


def test_record_job_pushes_latest_result(monkeypatch):
    pushed = {}
    monkeypatch.setenv(metrics.PUSHGATEWAY_ENV, "http://pushgateway:9091")
    monkeypatch.setattr(metrics.socket, "gethostname", lambda: "backup-host")

    def capture(gateway, *, job, grouping_key, registry, timeout):
        pushed.update(
            gateway=gateway,
            job=job,
            grouping_key=grouping_key,
            timeout=timeout,
            body=generate_latest(registry).decode(),
        )

    monkeypatch.setattr(metrics, "push_to_gateway", capture)
    metrics.record_job("photos", "files", True, 12.5, {"offsite": True})

    assert pushed["gateway"] == "http://pushgateway:9091"
    assert pushed["timeout"] == 5
    assert pushed["grouping_key"] == {"instance": "backup-host", "job_id": "photos"}
    assert (
        'restic_backups_job_last_run_success{dry_run="false",job_type="files"} 1.0'
        in pushed["body"]
    )
    assert 'repository_id="offsite"' in pushed["body"]


def test_record_job_is_disabled_without_gateway(monkeypatch):
    monkeypatch.delenv(metrics.PUSHGATEWAY_ENV, raising=False)
    monkeypatch.setattr(
        metrics,
        "push_to_gateway",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected push")
        ),
    )

    metrics.record_job("photos", "files", True, 1, {})

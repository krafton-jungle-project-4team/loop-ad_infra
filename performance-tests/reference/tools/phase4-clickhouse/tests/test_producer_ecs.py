from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from performance_tests_import import load_phase4_module


PREPARE = load_phase4_module("prepare_producer_ecs.py", "phase4_prepare_producer_ecs")
RUN = load_phase4_module("run_full_load_ecs.py", "phase4_run_full_load_ecs")


def test_bootstrap_command_uses_original_asset_and_digest_gate() -> None:
    command = PREPARE.bootstrap_command(
        "bucket-name",
        "producer/run_20260716_120000_phase4_clickhouse_ecs/producer-asset.tar.gz",
        "a" * 64,
    )
    assert "sha256sum -c -" in command
    assert "bootstrap.sh" in command
    assert "s3://bucket-name/producer/" in command
    assert "chmod 0755 /opt/loopad-producer/run_stage.sh" in command
    assert PREPARE.EXPECTED_POOL_SHA256 == (
        "93704c35ef7ca24c9c887a439dbea011c94a852f98e12b2d51b4bf6d4f3322b7"
    )


def test_full_load_command_freezes_exact_50k_contract() -> None:
    command = RUN.full_load_command(
        "stream-name",
        "run_20260716_120000_phase4_clickhouse_ecs",
        "bucket-name",
        "producer-evidence/run/full-load",
        "/var/lib/loopad-producer/evidence/full-load-run",
    )
    assert "50k_final 50000 60 300 8 stream-name" in command
    assert "/opt/loopad-producer/run_stage.sh" in command
    assert "artifact_transfer.py" in command


def test_full_load_start_gate_enforces_time_and_cost() -> None:
    now = datetime(2026, 7, 16, 12, 30, tzinfo=UTC)
    run = {"paidWallClockStartedAt": (now - timedelta(minutes=30)).isoformat()}
    cost = {"operationalMaximumUsd": "11.0", "maximumIncludingCleanupUsd": "14.0"}
    RUN.enforce_start_gate(run, cost, now)

    with pytest.raises(RuntimeError, match="cannot start"):
        RUN.enforce_start_gate(
            {"paidWallClockStartedAt": (now - timedelta(minutes=55)).isoformat()},
            cost,
            now,
        )
    with pytest.raises(RuntimeError, match="stop threshold"):
        RUN.enforce_start_gate(run, dict(cost, operationalMaximumUsd="17.0"), now)
    with pytest.raises(RuntimeError, match="hard cap"):
        RUN.enforce_start_gate(run, dict(cost, maximumIncludingCleanupUsd="20.000001"), now)


def test_stage_manifest_rejects_any_contract_change() -> None:
    document = {
        "stage": "50k_final",
        "targetRecordsPerSecond": 50_000,
        "workers": 8,
        "warmupSeconds": 60,
        "measurementSeconds": 300,
        "runId": "run",
        "streamName": "stream",
    }
    RUN.validate_stage_manifest(document, "run", "stream")
    with pytest.raises(RuntimeError, match="measurementSeconds"):
        RUN.validate_stage_manifest(dict(document, measurementSeconds=301), "run", "stream")


def test_clickhouse_snapshot_covers_parts_merges_and_disk() -> None:
    query = RUN.clickhouse_snapshot_query()
    assert "system.parts" in query
    assert "system.merges" in query
    assert "disk_used_percent" in query


def test_safety_monitor_stops_on_runtime_failure_but_not_capacity_signal() -> None:
    healthy = {
        "paidWallClockMinutes": 50,
        "serviceReady": True,
        "taskIdentitiesStable": True,
        "failureObjects": 0,
        "terminalFailure": 0,
        "checkpointError": 0,
        "readThrottle": 0,
        "clickHouseRestartCount": 0,
        "clickHouse": {"disk_used_percent": 10},
    }
    assert RUN.safety_violations(healthy) == []
    assert RUN.safety_violations(dict(healthy, taskCpuP95=75)) == []

    failed = dict(
        healthy,
        serviceReady=False,
        taskIdentitiesStable=False,
        failureObjects=1,
        terminalFailure=1,
        checkpointError=1,
        readThrottle=1,
        clickHouseRestartCount=1,
        paidWallClockMinutes=100,
        clickHouse={"disk_used_percent": 80},
    )
    assert set(RUN.safety_violations(failed)) == {
        "cleanup-deadline",
        "ecs-service-not-ready",
        "ecs-task-identity-changed",
        "terminal-failure-object",
        "terminal-failure-metric",
        "checkpoint-error-metric",
        "kinesis-read-throttle",
        "clickhouse-restart",
        "clickhouse-disk",
    }

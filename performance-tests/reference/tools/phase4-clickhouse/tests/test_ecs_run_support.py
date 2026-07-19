from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from performance_tests_import import load_phase4_module


SUPPORT = load_phase4_module("ecs_run_support.py", "phase4_ecs_run_support")
SMOKE = load_phase4_module("aws_correctness_smoke_ecs.py", "phase4_aws_smoke_ecs")
RECOVERY = load_phase4_module("verify_ecs_recovery.py", "phase4_verify_ecs_recovery")


def test_smoke_fixture_preserves_valid_properties_and_routes_invalid_and_late() -> None:
    fixture = SUPPORT.make_smoke_fixture("run_20260716_120000_phase4_clickhouse_ecs")
    valid = json.loads(fixture.valid_data)
    invalid = json.loads(fixture.invalid_data)
    late = json.loads(fixture.late_data)

    assert valid["properties_json"] == fixture.valid_properties_json
    assert "event_id" not in invalid
    assert invalid["run_id"] == valid["run_id"]
    assert late["event_id"] == fixture.late_event_id
    assert late["event_time"] == late["producer_sent_at"]
    assert fixture.valid_count == 1_000
    assert len(fixture.records) == 1_002
    assert base64.b64decode(base64.b64encode(fixture.invalid_data)) == fixture.invalid_data


def test_load_bundle_rejects_mismatched_outputs(tmp_path: Path) -> None:
    run_id = "run_20260716_120000_phase4_clickhouse_ecs"
    session_id = "phase4-clickhouse-ecs-20260716T120000Z"
    (tmp_path / "run.json").write_text(json.dumps({
        "runId": run_id,
        "sessionId": session_id,
        "account": "742711170910",
    }), encoding="utf-8")
    (tmp_path / "cdk-outputs.json").write_text(json.dumps({
        SUPPORT.RUNTIME_STACK_NAME: {
            "RunId": "run_20260716_120001_phase4_clickhouse_ecs",
            "SessionId": session_id,
            "ConsumerClusterName": "cluster",
            "ConsumerServiceName": "service",
            "ClickHouseInstanceId": "i-123",
            "ProducerInstanceId": "i-456",
            "StreamName": "stream",
            "LeaseTableName": "lease",
            "FailureBucketName": "failure",
            "ArchiveBucketName": "archive",
            "ConsumerImageUri": "repository@sha256:" + "a" * 64,
        },
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="do not match"):
        SUPPORT.load_bundle(tmp_path)


def test_service_ready_requires_two_distinct_instances() -> None:
    snapshot = {
        "desiredCount": 2,
        "runningCount": 2,
        "pendingCount": 0,
        "tasks": [
            {"containerInstanceArn": "one", "lastStatus": "RUNNING", "healthStatus": "HEALTHY"},
            {"containerInstanceArn": "two", "lastStatus": "RUNNING", "healthStatus": "HEALTHY"},
        ],
    }
    assert SMOKE.service_ready(snapshot) is True
    snapshot["tasks"][1]["containerInstanceArn"] = "one"
    assert SMOKE.service_ready(snapshot) is False


def test_lease_readiness_requires_exact_two_worker_60_60_assignment() -> None:
    balanced = {
        "count": 120,
        "ownedCount": 120,
        "ownerCounts": {"worker-a": 60, "worker-b": 60},
    }
    assert SMOKE.leases_balanced(balanced) is True

    first_worker_monopoly = {
        "count": 120,
        "ownedCount": 120,
        "ownerCounts": {"worker-a": 119, "worker-b": 1},
    }
    assert SMOKE.leases_balanced(first_worker_monopoly) is False

    stale_third_owner = {
        "count": 120,
        "ownedCount": 120,
        "ownerCounts": {"worker-a": 59, "worker-b": 60, "worker-old": 1},
    }
    assert SMOKE.leases_balanced(stale_third_owner) is False


def test_smoke_requires_clickhouse_schema_before_sending_records() -> None:
    class ReadyAws:
        def clickhouse_rows(self, _query):
            return [{"query_ok": 1, "schema_tables": 2}]

    class MissingAws:
        def clickhouse_rows(self, _query):
            raise RuntimeError("docker not ready")

    assert SMOKE.probe_clickhouse_readiness(ReadyAws()) == {
        "ready": True,
        "queryOk": 1,
        "schemaTables": 2,
    }
    missing = SMOKE.probe_clickhouse_readiness(MissingAws())
    assert missing["ready"] is False
    assert "docker not ready" in missing["error"]


def test_sql_literal_escapes_quote_and_backslash() -> None:
    assert SUPPORT.sql_literal("a'b\\c") == "'a\\'b\\\\c'"


def test_parse_utc_requires_offset_and_normalizes() -> None:
    assert SUPPORT.parse_utc("2026-07-16T12:00:00+09:00").isoformat() == "2026-07-16T03:00:00+00:00"
    with pytest.raises(ValueError, match="UTC offset"):
        SUPPORT.parse_utc("2026-07-16T12:00:00")


def test_fault_sender_accounts_for_all_accepted_records() -> None:
    class FakeAws:
        def put_records(self, records):
            return {"inputRecords": len(records), "failedRecords": 0}

    records = [(b"{}", str(index)) for index in range(3)]
    sender = RECOVERY.FaultInputSender(FakeAws(), records, records_per_second=3)
    sender.start()
    sender.join(timeout=2)

    assert sender.is_alive() is False
    assert sender.error is None
    assert sender.accepted == 3
    assert sender.failed == 0


def test_recovery_requires_exactly_one_task_identity_change() -> None:
    baseline = {"task-a", "task-b"}
    assert RECOVERY.exactly_one_task_replaced(
        baseline, {"task-b", "task-c"}, "task-a"
    )
    assert not RECOVERY.exactly_one_task_replaced(
        baseline, {"task-c", "task-d"}, "task-a"
    )
    assert not RECOVERY.exactly_one_task_replaced(
        baseline, {"task-a", "task-c"}, "task-a"
    )

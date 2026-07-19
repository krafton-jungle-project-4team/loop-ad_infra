#!/usr/bin/env python3
"""Run the Phase 4 ECS live correctness gate before any performance load."""

from __future__ import annotations

import argparse
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecs_run_support import (
    AwsRun,
    load_bundle,
    make_smoke_fixture,
    sql_literal,
    wait_until,
    write_private,
)


EXPECTED_LEASES = 120


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_bundle(args.run_dir)
    aws = AwsRun(bundle)
    identity = aws.assert_identity()
    service = wait_until(
        "two healthy ECS tasks",
        args.timeout_seconds,
        10,
        aws.service_snapshot,
        service_ready,
    )
    leases_before = wait_until(
        "120 KCL leases balanced 60/60 across two workers",
        args.timeout_seconds,
        10,
        aws.lease_snapshot,
        leases_balanced,
    )
    clickhouse_readiness = wait_until(
        "ClickHouse container and schema readiness",
        args.timeout_seconds,
        10,
        lambda: probe_clickhouse_readiness(aws),
        lambda value: value["ready"],
    )
    fixture = make_smoke_fixture(bundle.run_id)
    accepted = aws.put_records(fixture.records)
    query = smoke_query(bundle.run_id, fixture.valid_event_id, fixture.invalid_partition_key)
    try:
        counts = wait_until(
            "ClickHouse correctness rows",
            args.timeout_seconds,
            10,
            lambda: one_row(aws.clickhouse_rows(query)),
            lambda value: (
                int(value["events_final"]) == fixture.valid_count
                and int(value["events_unique"]) == fixture.valid_count
                and int(value["raw_events"]) == 1
            ),
        )
    except TimeoutError as error:
        write_private(args.run_dir / "smoke-failure-ecs.json", {
            "schemaVersion": 1,
            "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "runId": bundle.run_id,
            "sessionId": bundle.session_id,
            "stage": "ClickHouse correctness rows",
            "errorCategory": type(error).__name__,
            "accepted": accepted,
            "lastCounts": getattr(error, "last", None),
            "leasesBefore": leases_before,
            "leasesAtFailure": aws.lease_snapshot(),
            "failureObjects": aws.failure_object_count(),
            "pass": False,
        })
        raise
    late_count = wait_until(
        "LateEventDropped metric",
        args.timeout_seconds,
        15,
        lambda: aws.metric_sum(
            "LoopAd/Phase4",
            "LateEventDropped",
            [{"Name": "RunId", "Value": bundle.run_id}],
        ),
        lambda value: value >= 1,
    )
    iterator_age = wait_until(
        "published Kinesis iterator age",
        args.timeout_seconds,
        15,
        aws.iterator_age_max,
        lambda value: value is not None and value <= 60_000,
    )
    leases_after = aws.lease_snapshot()
    terminal_failure = aws.metric_sum(
        "LoopAd/Phase4",
        "TerminalFailure",
        [{"Name": "RunId", "Value": bundle.run_id}],
    )
    checkpoint_error = aws.metric_sum(
        "LoopAd/Phase4",
        "CheckpointError",
        [{"Name": "RunId", "Value": bundle.run_id}],
    )
    failure_objects = aws.failure_object_count()
    expected_raw_base64 = base64.b64encode(fixture.invalid_data).decode("ascii")
    checks = {
        "serviceReady": service_ready(service),
        "leaseCount": leases_after["count"] == EXPECTED_LEASES,
        "leaseOwnersBalanced": leases_balanced(leases_after),
        "allShardCheckpointsNumeric": (
            leases_after["numericCheckpointCount"] == EXPECTED_LEASES
        ),
        "inputCoveredEveryShard": len(accepted["shardIds"]) == EXPECTED_LEASES,
        "inputInvariant": (
            int(counts["events_final"]) + int(counts["raw_events"]) + int(late_count)
            == accepted["inputRecords"]
        ),
        "eventUnique": (
            int(counts["events_final"]) == fixture.valid_count
            and int(counts["events_unique"]) == fixture.valid_count
            and int(counts["events_physical"]) == fixture.valid_count
        ),
        "propertiesJsonExact": counts["properties_json"] == fixture.valid_properties_json,
        "rawPayloadExact": counts["raw_payload_base64"] == expected_raw_base64,
        "lateMetricExact": int(late_count) == 1,
        "terminalFailureZero": terminal_failure == 0,
        "checkpointErrorZero": checkpoint_error == 0,
        "failureObjectsZero": failure_objects == 0,
        "iteratorAgeBounded": iterator_age is not None and iterator_age <= 60_000,
    }
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "runId": bundle.run_id,
        "sessionId": bundle.session_id,
        "identity": identity,
        "accepted": accepted,
        "fixture": {
            "validEventId": fixture.valid_event_id,
            "validRecords": fixture.valid_count,
            "invalidPartitionKey": fixture.invalid_partition_key,
            "lateEventId": fixture.late_event_id,
        },
        "counts": {
            **counts,
            "lateEventDropped": late_count,
            "terminalFailure": terminal_failure,
            "checkpointError": checkpoint_error,
            "failureObjects": failure_objects,
            "iteratorAgeMaximumMilliseconds": iterator_age,
        },
        "service": service,
        "clickHouseReadiness": clickhouse_readiness,
        "leasesBefore": leases_before,
        "leasesAfter": leases_after,
        "checks": checks,
        "pass": all(checks.values()),
    }
    write_private(args.run_dir / "smoke-ecs.json", result)
    print(json.dumps({"checks": checks, "pass": result["pass"]}, indent=2))
    return 0 if result["pass"] else 2


def service_ready(snapshot: dict[str, Any]) -> bool:
    tasks = snapshot.get("tasks", [])
    return (
        snapshot.get("desiredCount") == 2
        and snapshot.get("runningCount") == 2
        and snapshot.get("pendingCount") == 0
        and len(tasks) == 2
        and len({task.get("containerInstanceArn") for task in tasks}) == 2
        and all(task.get("lastStatus") == "RUNNING" for task in tasks)
        and all(task.get("healthStatus") in {"HEALTHY", "UNKNOWN"} for task in tasks)
    )


def leases_balanced(snapshot: dict[str, Any]) -> bool:
    owner_counts = snapshot.get("ownerCounts", {})
    return (
        snapshot.get("count") == EXPECTED_LEASES
        and snapshot.get("ownedCount") == EXPECTED_LEASES
        and len(owner_counts) == 2
        and sorted(owner_counts.values()) == [60, 60]
    )


def probe_clickhouse_readiness(aws: AwsRun) -> dict[str, Any]:
    try:
        rows = aws.clickhouse_rows("""
SELECT
    1 AS query_ok,
    countIf(database = 'loopad' AND name IN ('events', 'raw_events')) AS schema_tables
FROM system.tables
""".strip())
        if len(rows) != 1:
            return {"ready": False, "error": f"expected one readiness row, got {len(rows)}"}
        observed = rows[0]
        return {
            "ready": int(observed["query_ok"]) == 1 and int(observed["schema_tables"]) == 2,
            "queryOk": int(observed["query_ok"]),
            "schemaTables": int(observed["schema_tables"]),
        }
    except Exception as error:  # readiness polling must not send records on transient bootstrap errors
        return {
            "ready": False,
            "error": f"{type(error).__name__}: {str(error)[:512]}",
        }


def smoke_query(run_id: str, event_id: str, invalid_partition_key: str) -> str:
    return f"""
SELECT
    (SELECT count() FROM loopad.events FINAL
      WHERE run_id = {sql_literal(run_id)}) AS events_final,
    (SELECT uniqExact(event_id) FROM loopad.events FINAL
      WHERE run_id = {sql_literal(run_id)}) AS events_unique,
    (SELECT count() FROM loopad.events
      WHERE run_id = {sql_literal(run_id)}) AS events_physical,
    (SELECT any(properties_json) FROM loopad.events FINAL
      WHERE run_id = {sql_literal(run_id)} AND event_id = {sql_literal(event_id)}) AS properties_json,
    (SELECT count() FROM loopad.raw_events
      WHERE run_id = {sql_literal(run_id)} AND partition_key = {sql_literal(invalid_partition_key)}) AS raw_events,
    (SELECT any(raw_payload_base64) FROM loopad.raw_events
      WHERE run_id = {sql_literal(run_id)} AND partition_key = {sql_literal(invalid_partition_key)}) AS raw_payload_base64
""".strip()


def one_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) != 1:
        raise RuntimeError(f"expected one ClickHouse row, got {len(rows)}")
    return rows[0]


if __name__ == "__main__":
    raise SystemExit(main())

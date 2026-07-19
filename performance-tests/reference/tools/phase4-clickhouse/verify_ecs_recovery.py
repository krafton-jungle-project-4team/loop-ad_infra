#!/usr/bin/env python3
"""Verify ECS task replacement under continuous input and KCL checkpoint recovery."""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aws_correctness_smoke_ecs import leases_balanced, service_ready
from ecs_run_support import (
    AwsRun,
    load_bundle,
    make_valid_records,
    sql_literal,
    wait_until,
    write_private,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_bundle(args.run_dir)
    smoke = json.loads((args.run_dir / "smoke-ecs.json").read_text(encoding="utf-8"))
    if not smoke.get("pass"):
        raise RuntimeError("smoke-ecs.json is not a passing baseline")
    baseline_tasks = {task["taskArn"] for task in smoke["service"]["tasks"]}
    baseline_lease_hash = str(smoke["leasesAfter"]["sha256"])

    aws = AwsRun(bundle)
    identity = aws.assert_identity()
    service_before = aws.service_snapshot()
    if not service_ready(service_before) or task_arns(service_before) != baseline_tasks:
        raise RuntimeError("ECS service changed after smoke and before the deliberate fault")

    fault_run_id = f"{bundle.run_id}-fault-smoke"
    source_records = make_valid_records(fault_run_id, 900)
    sender = FaultInputSender(aws, source_records, records_per_second=15)
    sender.start()
    if not sender.first_batch.wait(timeout=30):
        sender.join(timeout=5)
        raise TimeoutError("fault input did not accept its first batch")
    if sender.error is not None:
        raise sender.error
    stopped_task = sorted(baseline_tasks)[0]
    stop_response = aws.client("ecs").stop_task(
        cluster=bundle.outputs["ConsumerClusterName"],
        task=stopped_task,
        reason=f"Phase 4 deliberate task replacement fault for {bundle.run_id}",
    )
    sender.join(timeout=120)
    if sender.is_alive():
        raise TimeoutError("fault input sender did not finish")
    if sender.error is not None:
        raise sender.error
    if sender.accepted != len(source_records):
        raise RuntimeError(
            f"fault input accepted {sender.accepted}, expected {len(source_records)}"
        )

    service = wait_until(
        "replacement task and stable desired count",
        args.timeout_seconds,
        10,
        aws.service_snapshot,
        lambda value: service_ready(value) and task_arns(value) != baseline_tasks,
    )
    counts = wait_until(
        "all fault input in ClickHouse FINAL",
        args.timeout_seconds,
        10,
        lambda: fault_counts(aws, fault_run_id),
        lambda value: (
            value["final"] == sender.accepted
            and value["unique"] == sender.accepted
            and value["physical"] >= sender.accepted
        ),
    )
    leases = wait_until(
        "post-replacement checkpoint progress",
        args.timeout_seconds,
        10,
        aws.lease_snapshot,
        lambda value: (
            leases_balanced(value)
            and value["numericCheckpointCount"] == 120
            and value["sha256"] != baseline_lease_hash
        ),
    )
    iterator_age = wait_until(
        "post-replacement iterator age",
        args.timeout_seconds,
        15,
        aws.iterator_age_latest,
        lambda value: value is not None and value <= 1_000,
    )
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
    current_tasks = task_arns(service)
    task_identity_union = baseline_tasks | current_tasks
    checks = {
        "serviceRecovered": service_ready(service),
        "taskReplaced": current_tasks != baseline_tasks and len(current_tasks) == 2,
        "exactlyOneTaskIdentityReplaced": exactly_one_task_replaced(
            baseline_tasks, current_tasks, stopped_task
        ),
        "continuousInputAccepted": sender.accepted == 900 and sender.failed == 0,
        "faultInputMissingZero": counts["final"] == sender.accepted,
        "faultInputUnique": counts["unique"] == sender.accepted,
        "physicalAtLeastLogical": counts["physical"] >= sender.accepted,
        "leasesStable": leases_balanced(leases),
        "allShardCheckpointsNumeric": leases["numericCheckpointCount"] == 120,
        "checkpointProgressed": leases["sha256"] != baseline_lease_hash,
        "iteratorAgeReturnedToZeroBucket": iterator_age is not None and iterator_age <= 1_000,
        "terminalFailureZero": terminal_failure == 0,
        "checkpointErrorZero": checkpoint_error == 0,
        "failureObjectsZero": failure_objects == 0,
    }
    result = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "runId": bundle.run_id,
        "sessionId": bundle.session_id,
        "identity": identity,
        "baselineTaskArns": sorted(baseline_tasks),
        "taskIdentityUnion": sorted(task_identity_union),
        "serviceBefore": service_before,
        "stoppedTask": {
            "taskArn": stopped_task,
            "lastStatus": stop_response["task"].get("lastStatus"),
            "stopCode": stop_response["task"].get("stopCode"),
        },
        "recoveredService": service,
        "faultInput": {
            "payloadRunId": fault_run_id,
            "targetRecordsPerSecond": 15,
            "durationSeconds": 60,
            "offeredRecords": len(source_records),
            "acceptedRecords": sender.accepted,
            "failedRecords": sender.failed,
            "clickHouse": counts,
        },
        "leases": leases,
        "metrics": {
            "iteratorAgeLatestMaximumMilliseconds": iterator_age,
            "terminalFailure": terminal_failure,
            "checkpointError": checkpoint_error,
            "failureObjects": failure_objects,
        },
        "checks": checks,
        "pass": all(checks.values()),
    }
    write_private(args.run_dir / "recovery-ecs.json", result)
    print(json.dumps({"checks": checks, "pass": result["pass"]}, indent=2))
    return 0 if result["pass"] else 2


def task_arns(snapshot: dict[str, Any]) -> set[str]:
    return {str(task["taskArn"]) for task in snapshot.get("tasks", [])}


def exactly_one_task_replaced(
    baseline_tasks: set[str],
    current_tasks: set[str],
    deliberately_stopped_task: str,
) -> bool:
    return (
        len(baseline_tasks) == 2
        and len(current_tasks) == 2
        and len(baseline_tasks | current_tasks) == 3
        and deliberately_stopped_task in baseline_tasks
        and deliberately_stopped_task not in current_tasks
        and len(baseline_tasks & current_tasks) == 1
    )


def fault_counts(aws: AwsRun, run_id: str) -> dict[str, int]:
    rows = aws.clickhouse_rows(f"""
SELECT
    (SELECT count() FROM loopad.events FINAL WHERE run_id = {sql_literal(run_id)}) AS final,
    (SELECT uniqExact(event_id) FROM loopad.events FINAL WHERE run_id = {sql_literal(run_id)}) AS unique,
    (SELECT count() FROM loopad.events WHERE run_id = {sql_literal(run_id)}) AS physical
""".strip())
    if len(rows) != 1:
        raise RuntimeError(f"expected one fault count row, got {len(rows)}")
    return {key: int(rows[0][key]) for key in ["final", "unique", "physical"]}


class FaultInputSender(threading.Thread):
    def __init__(
        self,
        aws: AwsRun,
        records: list[tuple[bytes, str]],
        records_per_second: int,
    ) -> None:
        super().__init__(name="phase4-fault-input", daemon=True)
        self.aws = aws
        self.records = records
        self.records_per_second = records_per_second
        self.first_batch = threading.Event()
        self.accepted = 0
        self.failed = 0
        self.error: BaseException | None = None

    def run(self) -> None:
        started = time.monotonic()
        try:
            for offset in range(0, len(self.records), self.records_per_second):
                batch = self.records[offset:offset + self.records_per_second]
                result = self.aws.put_records([
                    {"Data": data, "PartitionKey": event_id}
                    for data, event_id in batch
                ])
                self.accepted += int(result["inputRecords"])
                self.failed += int(result["failedRecords"])
                self.first_batch.set()
                target = started + ((offset // self.records_per_second) + 1)
                remaining = target - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
        except BaseException as error:
            self.error = error
            self.first_batch.set()


if __name__ == "__main__":
    raise SystemExit(main())

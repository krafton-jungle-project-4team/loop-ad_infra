#!/usr/bin/env python3
"""Run the exact qualified 50k/s stage on the run-owned producer and retrieve evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from aws_correctness_smoke_ecs import leases_balanced, service_ready
from ecs_run_support import AwsRun, load_bundle, parse_utc, write_private


TARGET_RPS = 50_000
WARMUP_SECONDS = 60
MEASUREMENT_SECONDS = 300
WORKERS = 8
EXPECTED_RECORDS = TARGET_RPS * MEASUREMENT_SECONDS
FULL_LOAD_START_DEADLINE_MINUTES = 55
INSTALL_DIR = "/opt/loopad-producer"
SAFETY_POLL_SECONDS = 15
PAID_CLEANUP_DEADLINE_MINUTES = 100
NEW_LOAD_STOP_USD = 17.0
HARD_CAP_USD = 20.0


class FullLoadSafetyAbort(RuntimeError):
    def __init__(self, command_id: str, samples: list[dict[str, Any]]) -> None:
        super().__init__("full-load safety monitor stopped the producer")
        self.command_id = command_id
        self.samples = samples


class FullLoadCommandError(RuntimeError):
    def __init__(
        self,
        command_id: str,
        samples: list[dict[str, Any]],
        status: str,
        standard_error: str,
    ) -> None:
        super().__init__(f"producer SSM command ended with status {status}")
        self.command_id = command_id
        self.samples = samples
        self.status = status
        self.standard_error = standard_error[:512]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_bundle(args.run_dir)
    bootstrap = json.loads((args.run_dir / "producer-bootstrap-ecs.json").read_text(encoding="utf-8"))
    if not bootstrap.get("pass"):
        raise RuntimeError("producer bootstrap is not a passing gate")
    recovery = json.loads((args.run_dir / "recovery-ecs.json").read_text(encoding="utf-8"))
    if not recovery.get("pass") or recovery.get("checks", {}).get(
        "exactlyOneTaskIdentityReplaced"
    ) is not True:
        raise RuntimeError("recovery-ecs.json is not an exact-one-replacement passing gate")
    expected_task_arns = {
        str(task["taskArn"])
        for task in recovery.get("recoveredService", {}).get("tasks", [])
    }
    if len(expected_task_arns) != 2:
        raise RuntimeError("recovery evidence must contain exactly two current tasks")
    run_document = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
    cost_model = json.loads((args.run_dir / "cost-model-ecs.json").read_text(encoding="utf-8"))
    enforce_start_gate(run_document, cost_model, datetime.now(UTC))

    aws = AwsRun(bundle)
    identity = aws.assert_identity()
    service = aws.service_snapshot()
    leases = aws.lease_snapshot()
    iterator_age = aws.iterator_age_latest()
    clickhouse_ready = aws.clickhouse_rows("SELECT 1 AS ready")
    clickhouse_before = one_row(aws.clickhouse_rows(clickhouse_snapshot_query()))
    if (
        not service_ready(service)
        or not leases_balanced(leases)
        or leases["numericCheckpointCount"] != 120
        or service_task_arns(service) != expected_task_arns
        or iterator_age is None
        or iterator_age > 1_000
        or clickhouse_ready != [{"ready": 1}]
        or aws.failure_object_count() != 0
    ):
        raise RuntimeError("full-load readiness gate failed")

    instance_id = bundle.outputs["ProducerInstanceId"]
    remote_output = f"/var/lib/loopad-producer/evidence/full-load-{bundle.run_id}"
    evidence_prefix = f"producer-evidence/{bundle.run_id}/full-load"
    command = full_load_command(
        stream_name=bundle.outputs["StreamName"],
        run_id=bundle.run_id,
        bucket=bundle.outputs["ArchiveBucketName"],
        evidence_prefix=evidence_prefix,
        remote_output=remote_output,
    )
    started_at = datetime.now(UTC)
    safety_samples: list[dict[str, Any]] = []
    command_id: str | None = None
    try:
        stdout, command_id, safety_samples = run_monitored_full_load(
            aws,
            command,
            instance_id,
            run_document,
            expected_task_arns,
            args.timeout_seconds,
        )
        completed_at = datetime.now(UTC)
        local_evidence = args.run_dir / "producer-full-load"
        if local_evidence.exists():
            raise FileExistsError("local producer-full-load evidence already exists")
        downloaded = download_evidence(
            aws,
            bundle.outputs["ArchiveBucketName"],
            evidence_prefix,
            local_evidence,
        )
        stage_manifest = json.loads(
            (local_evidence / "stage-manifest.json").read_text(encoding="utf-8")
        )
        validate_stage_manifest(stage_manifest, bundle.run_id, bundle.outputs["StreamName"])
    except FullLoadSafetyAbort as error:
        stop_producer(aws, instance_id)
        result = {
            "schemaVersion": 1,
            "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "runId": bundle.run_id,
            "sessionId": bundle.session_id,
            "identity": identity,
            "startedAt": started_at.isoformat().replace("+00:00", "Z"),
            "ssmCommandId": error.command_id,
            "safetySamples": error.samples,
            "violations": error.samples[-1].get("violations", []) if error.samples else [],
            "producerInstanceStopped": True,
            "verdict": "aborted",
            "pass": False,
        }
        write_private(args.run_dir / "producer-full-load-abort-ecs.json", result)
        print(json.dumps({
            "violations": result["violations"],
            "producerInstanceStopped": True,
            "pass": False,
        }, indent=2))
        return 2
    except FullLoadCommandError as error:
        stop_producer(aws, instance_id)
        result = {
            "schemaVersion": 1,
            "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "runId": bundle.run_id,
            "sessionId": bundle.session_id,
            "identity": identity,
            "startedAt": started_at.isoformat().replace("+00:00", "Z"),
            "ssmCommandId": error.command_id,
            "safetySamples": error.samples,
            "errorCategory": type(error).__name__,
            "errorMessage": str(error),
            "ssmStatus": error.status,
            "ssmStandardError": error.standard_error,
            "producerInstanceStopped": True,
            "verdict": "aborted",
            "pass": False,
        }
        write_private(args.run_dir / "producer-full-load-error-ecs.json", result)
        print(json.dumps({
            "errorCategory": result["errorCategory"],
            "ssmCommandId": result["ssmCommandId"],
            "ssmStatus": result["ssmStatus"],
            "producerInstanceStopped": True,
            "pass": False,
        }, indent=2))
        return 2
    except Exception as error:
        stop_producer(aws, instance_id)
        result = {
            "schemaVersion": 1,
            "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "runId": bundle.run_id,
            "sessionId": bundle.session_id,
            "identity": identity,
            "startedAt": started_at.isoformat().replace("+00:00", "Z"),
            "ssmCommandId": command_id,
            "safetySamples": safety_samples,
            "errorCategory": type(error).__name__,
            "errorMessage": str(error)[:512],
            "producerInstanceStopped": True,
            "verdict": "aborted",
            "pass": False,
        }
        write_private(args.run_dir / "producer-full-load-error-ecs.json", result)
        print(json.dumps({
            "errorCategory": result["errorCategory"],
            "producerInstanceStopped": True,
            "pass": False,
        }, indent=2))
        return 2
    finally:
        stop_producer(aws, instance_id)
    result = {
        "schemaVersion": 1,
        "generatedAt": completed_at.isoformat().replace("+00:00", "Z"),
        "runId": bundle.run_id,
        "sessionId": bundle.session_id,
        "identity": identity,
        "startedAt": started_at.isoformat().replace("+00:00", "Z"),
        "completedAt": completed_at.isoformat().replace("+00:00", "Z"),
        "targetRecordsPerSecond": TARGET_RPS,
        "measurementSeconds": MEASUREMENT_SECONDS,
        "expectedRecords": EXPECTED_RECORDS,
        "workers": WORKERS,
        "readiness": {
            "service": service,
            "leases": {key: leases[key] for key in ["count", "ownedCount", "sha256"]},
            "iteratorAgeLatestMaximumMilliseconds": iterator_age,
            "clickHouse": clickhouse_ready,
            "clickHouseBefore": clickhouse_before,
        },
        "remoteStdout": stdout,
        "ssmCommandId": command_id,
        "safetySamples": safety_samples,
        "evidence": downloaded,
        "stageManifest": stage_manifest,
        "producerInstanceStopped": True,
        "pass": True,
    }
    write_private(args.run_dir / "producer-full-load-ecs.json", result)
    print(json.dumps({
        "expectedRecords": EXPECTED_RECORDS,
        "evidenceFiles": len(downloaded),
        "producerInstanceStopped": True,
        "pass": True,
    }, indent=2))
    return 0


def enforce_start_gate(run_document: dict[str, Any], cost_model: dict[str, Any], now: datetime) -> None:
    started_value = run_document.get("paidWallClockStartedAt")
    if not isinstance(started_value, str):
        raise ValueError("run.json must contain paidWallClockStartedAt")
    elapsed_minutes = (now - parse_utc(started_value)).total_seconds() / 60
    if elapsed_minutes < 0 or elapsed_minutes >= FULL_LOAD_START_DEADLINE_MINUTES:
        raise RuntimeError(
            f"full load cannot start at paid wall-clock minute {elapsed_minutes:.3f}"
        )
    if float(cost_model["operationalMaximumUsd"]) >= NEW_LOAD_STOP_USD:
        raise RuntimeError("modeled operational cost has reached the new-load stop threshold")
    if float(cost_model["maximumIncludingCleanupUsd"]) > HARD_CAP_USD:
        raise RuntimeError("modeled maximum exceeds the hard cap")


def full_load_command(
    stream_name: str,
    run_id: str,
    bucket: str,
    evidence_prefix: str,
    remote_output: str,
) -> str:
    values = [stream_name, run_id, bucket, evidence_prefix, remote_output]
    if any(not value or "\n" in value or "\r" in value for value in values):
        raise ValueError("invalid full-load command input")
    run_stage = [
        f"{INSTALL_DIR}/run_stage.sh",
        "50k_final",
        str(TARGET_RPS),
        str(WARMUP_SECONDS),
        str(MEASUREMENT_SECONDS),
        str(WORKERS),
        stream_name,
        run_id,
        f"{INSTALL_DIR}/payloads.ndjson",
        remote_output,
    ]
    transfer = [
        f"{INSTALL_DIR}/.venv/bin/python",
        f"{INSTALL_DIR}/artifact_transfer.py",
        "--region", "ap-northeast-2",
        "upload-tree",
        "--bucket", bucket,
        "--prefix", evidence_prefix,
        "--source", remote_output,
    ]
    return "\n".join([
        "set -euo pipefail",
        "test ! -e " + shlex.quote(remote_output),
        "runuser -u ec2-user -- " + " ".join(shlex.quote(value) for value in run_stage),
        "runuser -u ec2-user -- " + " ".join(shlex.quote(value) for value in transfer) + " >/tmp/phase4-producer-transfer.json",
        "echo phase4-producer-evidence-uploaded",
    ])


def download_evidence(
    aws: AwsRun,
    bucket: str,
    prefix: str,
    destination: Path,
) -> list[dict[str, Any]]:
    s3 = aws.client("s3")
    objects = [
        item
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket,
            Prefix=f"{prefix.rstrip('/')}/",
        )
        for item in page.get("Contents", [])
    ]
    if not objects:
        raise RuntimeError("producer evidence prefix is empty")
    downloaded: list[dict[str, Any]] = []
    for item in objects:
        relative = PurePosixPath(item["Key"]).relative_to(PurePosixPath(prefix))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("unsafe producer evidence key")
        local = destination.joinpath(*relative.parts)
        local.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, item["Key"], str(local))
        digest = hashlib.sha256(local.read_bytes()).hexdigest()
        head = s3.head_object(Bucket=bucket, Key=item["Key"])
        expected_digest = head.get("Metadata", {}).get("sha256")
        if expected_digest != digest:
            raise RuntimeError(f"producer evidence digest mismatch: {item['Key']}")
        downloaded.append({
            "key": item["Key"],
            "bytes": int(item["Size"]),
            "sha256": digest,
            "local": str(local),
        })
    return downloaded


def run_monitored_full_load(
    aws: AwsRun,
    command: str,
    instance_id: str,
    run_document: dict[str, Any],
    expected_task_arns: set[str],
    timeout_seconds: int,
) -> tuple[str, str, list[dict[str, Any]]]:
    ssm = aws.client("ssm")
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        TimeoutSeconds=timeout_seconds,
        Parameters={"commands": [command], "executionTimeout": [str(timeout_seconds)]},
        Comment=f"Exact Phase 4 qualified full load for {aws.bundle.run_id}",
    )
    command_id = str(response["Command"]["CommandId"])
    deadline = time.monotonic() + timeout_seconds
    next_safety_poll = 0.0
    samples: list[dict[str, Any]] = []
    while True:
        now_monotonic = time.monotonic()
        if now_monotonic >= next_safety_poll:
            try:
                sample = live_safety_snapshot(aws, run_document, expected_task_arns)
            except Exception as error:
                sample = {
                    "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "monitorErrorCategory": type(error).__name__,
                    "violations": [f"monitor-error:{type(error).__name__}"],
                }
            samples.append(sample)
            if sample.get("violations"):
                cancel_ssm_command(ssm, command_id, instance_id)
                stop_producer(aws, instance_id)
                raise FullLoadSafetyAbort(command_id, samples)
            next_safety_poll = now_monotonic + SAFETY_POLL_SECONDS

        try:
            invocation = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except ssm.exceptions.InvocationDoesNotExist:
            invocation = {"Status": "Pending"}
        status = str(invocation.get("Status", "Pending"))
        if status == "Success":
            return str(invocation.get("StandardOutputContent", "")), command_id, samples
        if status in {"Cancelled", "Failed", "TimedOut", "Undeliverable", "Terminated"}:
            raise FullLoadCommandError(
                command_id,
                samples,
                status,
                str(invocation.get("StandardErrorContent", "")),
            )
        if time.monotonic() >= deadline:
            cancel_ssm_command(ssm, command_id, instance_id)
            raise TimeoutError("producer SSM command exceeded the bounded timeout")
        time.sleep(3)


def live_safety_snapshot(
    aws: AwsRun,
    run_document: dict[str, Any],
    expected_task_arns: set[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = now or datetime.now(UTC)
    paid_started = parse_utc(str(run_document["paidWallClockStartedAt"]))
    paid_minutes = (observed_at - paid_started).total_seconds() / 60
    service = aws.service_snapshot()
    clickhouse = one_row(aws.clickhouse_rows(clickhouse_snapshot_query()))
    restart_text = aws.run_ssm([
        "docker inspect --format '{{.RestartCount}}' phase4-clickhouse",
    ]).strip()
    if not restart_text.isdigit():
        raise RuntimeError("ClickHouse restart count is not numeric")
    snapshot = {
        "generatedAt": observed_at.isoformat().replace("+00:00", "Z"),
        "paidWallClockMinutes": round(paid_minutes, 3),
        "serviceReady": service_ready(service),
        "taskIdentitiesStable": (
            expected_task_arns is None
            or service_task_arns(service) == expected_task_arns
        ),
        "currentTaskArns": sorted(service_task_arns(service)),
        "service": service,
        "failureObjects": aws.failure_object_count(),
        "terminalFailure": aws.metric_sum(
            "LoopAd/Phase4",
            "TerminalFailure",
            [{"Name": "RunId", "Value": aws.bundle.run_id}],
        ),
        "checkpointError": aws.metric_sum(
            "LoopAd/Phase4",
            "CheckpointError",
            [{"Name": "RunId", "Value": aws.bundle.run_id}],
        ),
        "readThrottle": aws.metric_sum(
            "AWS/Kinesis",
            "ReadProvisionedThroughputExceeded",
            [{"Name": "StreamName", "Value": aws.bundle.outputs["StreamName"]}],
        ),
        "iteratorAgeLatestMaximumMilliseconds": aws.iterator_age_latest(),
        "clickHouse": clickhouse,
        "clickHouseRestartCount": int(restart_text),
    }
    snapshot["violations"] = safety_violations(snapshot)
    return snapshot


def safety_violations(snapshot: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    if float(snapshot["paidWallClockMinutes"]) >= PAID_CLEANUP_DEADLINE_MINUTES:
        violations.append("cleanup-deadline")
    if snapshot.get("serviceReady") is not True:
        violations.append("ecs-service-not-ready")
    if snapshot.get("taskIdentitiesStable") is not True:
        violations.append("ecs-task-identity-changed")
    if int(snapshot.get("failureObjects", 0)) != 0:
        violations.append("terminal-failure-object")
    if float(snapshot.get("terminalFailure", 0)) != 0:
        violations.append("terminal-failure-metric")
    if float(snapshot.get("checkpointError", 0)) != 0:
        violations.append("checkpoint-error-metric")
    if float(snapshot.get("readThrottle", 0)) != 0:
        violations.append("kinesis-read-throttle")
    if int(snapshot.get("clickHouseRestartCount", 0)) != 0:
        violations.append("clickhouse-restart")
    clickhouse = snapshot.get("clickHouse")
    if not isinstance(clickhouse, dict) or float(clickhouse.get("disk_used_percent", 100)) >= 80:
        violations.append("clickhouse-disk")
    return violations


def cancel_ssm_command(ssm: Any, command_id: str, instance_id: str) -> None:
    try:
        ssm.cancel_command(CommandId=command_id, InstanceIds=[instance_id])
    except Exception:
        # The EC2 stop immediately following this call is the authoritative load stop.
        pass


def stop_producer(aws: AwsRun, instance_id: str) -> None:
    response = aws.client("ec2").describe_instances(InstanceIds=[instance_id])
    state = response["Reservations"][0]["Instances"][0]["State"]["Name"]
    if state not in {"stopping", "stopped", "shutting-down", "terminated"}:
        aws.client("ec2").stop_instances(InstanceIds=[instance_id])
    if state not in {"shutting-down", "terminated"}:
        aws.client("ec2").get_waiter("instance_stopped").wait(
            InstanceIds=[instance_id],
            WaiterConfig={"Delay": 10, "MaxAttempts": 60},
        )


def validate_stage_manifest(document: dict[str, Any], run_id: str, stream_name: str) -> None:
    expected = {
        "stage": "50k_final",
        "targetRecordsPerSecond": TARGET_RPS,
        "workers": WORKERS,
        "warmupSeconds": WARMUP_SECONDS,
        "measurementSeconds": MEASUREMENT_SECONDS,
        "runId": run_id,
        "streamName": stream_name,
    }
    mismatches = {
        key: {"observed": document.get(key), "expected": value}
        for key, value in expected.items()
        if document.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"producer stage manifest mismatch: {mismatches}")


def clickhouse_snapshot_query() -> str:
    return """
SELECT
    (SELECT count() FROM system.parts
      WHERE active AND database = 'loopad' AND table = 'events') AS active_parts,
    (SELECT count() FROM system.merges
      WHERE database = 'loopad' AND table = 'events') AS active_merges,
    round(100 * (1 - free_space / total_space), 6) AS disk_used_percent
FROM system.disks
WHERE name = 'default'
""".strip()


def one_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) != 1:
        raise RuntimeError(f"expected one ClickHouse row, got {len(rows)}")
    return rows[0]


def service_task_arns(snapshot: dict[str, Any]) -> set[str]:
    return {str(task["taskArn"]) for task in snapshot.get("tasks", [])}


if __name__ == "__main__":
    raise SystemExit(main())

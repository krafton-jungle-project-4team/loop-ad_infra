#!/usr/bin/env python3
"""Shared, run-scoped AWS inspection helpers for the Phase 4 ECS experiment."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.config import Config


ROOT = Path(__file__).resolve().parents[2]
QUALIFIED_IMPLEMENTATION = (
    ROOT
    / "performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation"
)
sys.path.insert(0, str(QUALIFIED_IMPLEMENTATION))

from payload_contract import EXPECTED_POOL_SHA256, PayloadFactory, compact_json  # noqa: E402


REGION = "ap-northeast-2"
PAYLOAD_PATH = ROOT / "performance-tests/phase1-kinesis/payloads/sdk-compatible-event-bodies.ndjson"
RUNTIME_STACK_NAME = "LoopAdPerfPhase4ClickHouseEcsStack"
RUN_ID_PATTERN = re.compile(r"^run_[0-9]{8}_[0-9]{6}_phase4_clickhouse_ecs$")
SESSION_ID_PATTERN = re.compile(r"^phase4-clickhouse-ecs-[0-9]{8}T[0-9]{6}Z$")
SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "total_max_attempts": 5},
    user_agent_appid="loopad-phase4-ecs-run/1",
)


class WaitTimeoutError(TimeoutError):
    def __init__(self, description: str, last: Any) -> None:
        super().__init__(f"timed out waiting for {description}; last={last!r}")
        self.description = description
        self.last = last


@dataclass(frozen=True)
class RunBundle:
    run_dir: Path
    run_id: str
    session_id: str
    account: str
    outputs: dict[str, str]


@dataclass(frozen=True)
class SmokeFixture:
    valid_records: list[tuple[bytes, str]]
    valid_data: bytes
    valid_event_id: str
    valid_properties_json: str
    invalid_data: bytes
    invalid_partition_key: str
    late_data: bytes
    late_event_id: str

    @property
    def records(self) -> list[dict[str, Any]]:
        return [
            *[
                {"Data": data, "PartitionKey": event_id}
                for data, event_id in self.valid_records
            ],
            {"Data": self.invalid_data, "PartitionKey": self.invalid_partition_key},
            {"Data": self.late_data, "PartitionKey": self.late_event_id},
        ]

    @property
    def valid_count(self) -> int:
        return len(self.valid_records)


class AwsRun:
    def __init__(self, bundle: RunBundle) -> None:
        self.bundle = bundle
        self.session = boto3.Session(region_name=REGION)
        self._clients: dict[str, Any] = {}

    def client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = self.session.client(
                service,
                region_name=REGION,
                config=SDK_CONFIG,
            )
        return self._clients[service]

    def assert_identity(self) -> dict[str, Any]:
        identity = self.client("sts").get_caller_identity()
        if str(identity["Account"]) != self.bundle.account:
            raise RuntimeError(
                f"account mismatch: expected {self.bundle.account}, got {identity['Account']}"
            )
        return {"account": str(identity["Account"]), "arn": str(identity["Arn"])}

    def service_snapshot(self) -> dict[str, Any]:
        cluster = self.bundle.outputs["ConsumerClusterName"]
        service_name = self.bundle.outputs["ConsumerServiceName"]
        ecs = self.client("ecs")
        services = ecs.describe_services(cluster=cluster, services=[service_name])["services"]
        if len(services) != 1:
            raise RuntimeError("expected exactly one ECS service")
        service = services[0]
        task_arns = sorted(
            task_arn
            for page in ecs.get_paginator("list_tasks").paginate(
                cluster=cluster,
                serviceName=service_name,
                desiredStatus="RUNNING",
            )
            for task_arn in page.get("taskArns", [])
        )
        tasks: list[dict[str, Any]] = []
        for offset in range(0, len(task_arns), 100):
            tasks.extend(ecs.describe_tasks(
                cluster=cluster,
                tasks=task_arns[offset:offset + 100],
            )["tasks"])
        container_arns = sorted({
            str(task["containerInstanceArn"])
            for task in tasks
            if task.get("containerInstanceArn")
        })
        container_instances: list[dict[str, Any]] = []
        for offset in range(0, len(container_arns), 100):
            container_instances.extend(ecs.describe_container_instances(
                cluster=cluster,
                containerInstances=container_arns[offset:offset + 100],
            )["containerInstances"])
        container_to_ec2 = {
            item["containerInstanceArn"]: item["ec2InstanceId"]
            for item in container_instances
        }
        task_documents = sorted([
            {
                "taskArn": task["taskArn"],
                "taskDefinitionArn": task["taskDefinitionArn"],
                "containerInstanceArn": task.get("containerInstanceArn"),
                "ec2InstanceId": container_to_ec2.get(task.get("containerInstanceArn", "")),
                "healthStatus": task.get("healthStatus"),
                "lastStatus": task.get("lastStatus"),
                "startedAt": iso_value(task.get("startedAt")),
            }
            for task in tasks
        ], key=lambda item: item["taskArn"])
        return {
            "cluster": cluster,
            "service": service_name,
            "desiredCount": int(service.get("desiredCount", 0)),
            "runningCount": int(service.get("runningCount", 0)),
            "pendingCount": int(service.get("pendingCount", 0)),
            "deployments": [
                {
                    "status": deployment.get("status"),
                    "rolloutState": deployment.get("rolloutState"),
                    "desiredCount": deployment.get("desiredCount"),
                    "runningCount": deployment.get("runningCount"),
                    "pendingCount": deployment.get("pendingCount"),
                }
                for deployment in service.get("deployments", [])
            ],
            "tasks": task_documents,
        }

    def lease_snapshot(self) -> dict[str, Any]:
        resource = self.session.resource(
            "dynamodb",
            region_name=REGION,
            config=SDK_CONFIG,
        )
        client = resource.meta.client
        items: list[dict[str, Any]] = []
        for page in client.get_paginator("scan").paginate(
            TableName=self.bundle.outputs["LeaseTableName"],
            ConsistentRead=True,
        ):
            for item in page.get("Items", []):
                items.append({
                    "leaseKey": str(item.get("leaseKey", "")),
                    "checkpoint": str(item.get("checkpoint", "")),
                    "checkpointSubSequenceNumber": str(item.get("checkpointSubSequenceNumber", "")),
                    "leaseOwner": str(item.get("leaseOwner", "")),
                    "leaseCounter": str(item.get("leaseCounter", "")),
                    "ownerSwitchesSinceCheckpoint": str(item.get("ownerSwitchesSinceCheckpoint", "")),
                })
        items.sort(key=lambda item: item["leaseKey"])
        encoded = json.dumps(items, sort_keys=True, separators=(",", ":")).encode("utf-8")
        owner_counts: dict[str, int] = {}
        for item in items:
            owner = item["leaseOwner"]
            if owner:
                owner_counts[owner] = owner_counts.get(owner, 0) + 1
        return {
            "count": len(items),
            "ownedCount": sum(bool(item["leaseOwner"]) for item in items),
            "checkpointedCount": sum(bool(item["checkpoint"]) for item in items),
            "numericCheckpointCount": sum(
                bool(re.fullmatch(r"[0-9]+", item["checkpoint"])) for item in items
            ),
            "ownerCounts": dict(sorted(owner_counts.items())),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "items": items,
        }

    def put_records(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        if not records:
            raise ValueError("at least one Kinesis record is required")
        entries: list[dict[str, Any]] = []
        request_count = 0
        for offset in range(0, len(records), 500):
            chunk = records[offset:offset + 500]
            response = self.client("kinesis").put_records(
                StreamName=self.bundle.outputs["StreamName"],
                Records=chunk,
            )
            chunk_entries = response.get("Records", [])
            errors = [entry for entry in chunk_entries if entry.get("ErrorCode")]
            if (
                int(response.get("FailedRecordCount", 0)) != 0
                or errors
                or len(chunk_entries) != len(chunk)
            ):
                raise RuntimeError("Kinesis smoke PutRecords was not fully accepted")
            entries.extend(chunk_entries)
            request_count += 1
        return {
            "inputRecords": len(records),
            "failedRecords": 0,
            "putRecordsRequests": request_count,
            "shardIds": sorted({str(entry["ShardId"]) for entry in entries}),
            "sequenceNumbers": [str(entry["SequenceNumber"]) for entry in entries],
        }

    def clickhouse_rows(self, query: str, timeout_seconds: int = 180) -> list[dict[str, Any]]:
        if not query.lstrip().upper().startswith("SELECT"):
            raise ValueError("run validation allows SELECT queries only")
        query_payload = base64.b64encode(query.encode("utf-8")).decode("ascii")
        command = (
            f"printf '%s' '{query_payload}' | base64 -d | "
            "docker exec -i phase4-clickhouse clickhouse-client --format JSONEachRow"
        )
        output = self.run_ssm([command], timeout_seconds=timeout_seconds)
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    def run_ssm(
        self,
        commands: list[str],
        timeout_seconds: int = 180,
        instance_id: str | None = None,
    ) -> str:
        target_instance_id = instance_id or self.bundle.outputs["ClickHouseInstanceId"]
        ssm = self.client("ssm")
        response = ssm.send_command(
            InstanceIds=[target_instance_id],
            DocumentName="AWS-RunShellScript",
            TimeoutSeconds=timeout_seconds,
            Parameters={"commands": commands, "executionTimeout": [str(timeout_seconds)]},
            Comment=f"Phase 4 run-scoped validation for {self.bundle.run_id}",
        )
        command_id = response["Command"]["CommandId"]
        ssm.get_waiter("command_executed").wait(
            CommandId=command_id,
            InstanceId=target_instance_id,
            WaiterConfig={"Delay": 3, "MaxAttempts": max(1, timeout_seconds // 3)},
        )
        invocation = ssm.get_command_invocation(
            CommandId=command_id,
            InstanceId=target_instance_id,
        )
        if invocation["Status"] != "Success":
            raise RuntimeError(
                f"SSM query failed with status {invocation['Status']}: "
                f"{invocation.get('StandardErrorContent', '')[:512]}"
            )
        return str(invocation.get("StandardOutputContent", ""))

    def failure_object_count(self) -> int:
        s3 = self.client("s3")
        return sum(
            len(page.get("Contents", []))
            for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.bundle.outputs["FailureBucketName"],
                Prefix=f"failures/{self.bundle.run_id}/",
            )
        )

    def metric_sum(
        self,
        namespace: str,
        metric_name: str,
        dimensions: list[dict[str, str]],
        minutes: int = 15,
    ) -> float:
        end = datetime.now(UTC)
        response = self.client("cloudwatch").get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=end - timedelta(minutes=minutes),
            EndTime=end,
            Period=60,
            Statistics=["Sum"],
        )
        return sum(float(point.get("Sum", 0.0)) for point in response.get("Datapoints", []))

    def iterator_age_max(self, minutes: int = 10) -> float | None:
        end = datetime.now(UTC)
        response = self.client("cloudwatch").get_metric_statistics(
            Namespace="AWS/Kinesis",
            MetricName="GetRecords.IteratorAgeMilliseconds",
            Dimensions=[{"Name": "StreamName", "Value": self.bundle.outputs["StreamName"]}],
            StartTime=end - timedelta(minutes=minutes),
            EndTime=end,
            Period=60,
            Statistics=["Maximum"],
        )
        values = [float(point["Maximum"]) for point in response.get("Datapoints", []) if "Maximum" in point]
        return max(values) if values else None

    def iterator_age_latest(self, minutes: int = 10) -> float | None:
        end = datetime.now(UTC)
        response = self.client("cloudwatch").get_metric_statistics(
            Namespace="AWS/Kinesis",
            MetricName="GetRecords.IteratorAgeMilliseconds",
            Dimensions=[{"Name": "StreamName", "Value": self.bundle.outputs["StreamName"]}],
            StartTime=end - timedelta(minutes=minutes),
            EndTime=end,
            Period=60,
            Statistics=["Maximum"],
        )
        points = sorted(response.get("Datapoints", []), key=lambda point: point["Timestamp"])
        if not points or "Maximum" not in points[-1]:
            return None
        return float(points[-1]["Maximum"])


def load_bundle(run_dir: Path) -> RunBundle:
    run_document = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_id = str(run_document.get("runId", ""))
    session_id = str(run_document.get("sessionId", ""))
    account = str(run_document.get("account") or run_document.get("expectedAccount") or "")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run.json has an invalid Phase 4 ECS runId")
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("run.json has an invalid Phase 4 ECS sessionId")
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("run.json has an invalid AWS account")
    output_document = json.loads((run_dir / "cdk-outputs.json").read_text(encoding="utf-8"))
    stack_outputs = output_document.get(RUNTIME_STACK_NAME)
    if not isinstance(stack_outputs, dict):
        raise ValueError(f"cdk-outputs.json has no {RUNTIME_STACK_NAME} object")
    outputs = {str(key): str(value) for key, value in stack_outputs.items()}
    required = {
        "RunId", "SessionId", "ConsumerClusterName", "ConsumerServiceName",
        "ClickHouseInstanceId", "ProducerInstanceId", "StreamName", "LeaseTableName",
        "FailureBucketName", "ArchiveBucketName", "ConsumerImageUri",
    }
    missing = sorted(required.difference(outputs))
    if missing:
        raise ValueError(f"cdk outputs are missing: {', '.join(missing)}")
    if outputs["RunId"] != run_id or outputs["SessionId"] != session_id:
        raise ValueError("cdk outputs do not match run.json ownership")
    return RunBundle(run_dir, run_id, session_id, account, outputs)


def make_smoke_fixture(run_id: str, valid_count: int = 1_000) -> SmokeFixture:
    if valid_count < 1:
        raise ValueError("valid_count must be positive")
    factory = PayloadFactory(PAYLOAD_PATH, run_id, expected_sha256=EXPECTED_POOL_SHA256)
    valid_records = factory.create_batch(min(500, valid_count))
    while len(valid_records) < valid_count:
        valid_records.extend(factory.create_batch(min(500, valid_count - len(valid_records))))
    valid_records = valid_records[:valid_count]
    valid = valid_records[0]
    valid_body = json.loads(valid.data)
    invalid_body = json.loads(factory.create_record().data)
    invalid_body.pop("event_id", None)
    invalid_data = compact_json(invalid_body)
    late = factory.create_record()
    late_body = json.loads(late.data)
    late_at = datetime.now(UTC) - timedelta(days=8)
    late_text = late_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    late_body["event_time"] = late_text
    late_body["producer_sent_at"] = late_text
    return SmokeFixture(
        valid_records=[(record.data, record.event_id) for record in valid_records],
        valid_data=valid.data,
        valid_event_id=valid.event_id,
        valid_properties_json=str(valid_body["properties_json"]),
        invalid_data=invalid_data,
        invalid_partition_key="phase4-smoke-invalid-required-field",
        late_data=compact_json(late_body),
        late_event_id=late.event_id,
    )


def make_valid_record(run_id: str) -> tuple[bytes, str]:
    record = PayloadFactory(PAYLOAD_PATH, run_id, expected_sha256=EXPECTED_POOL_SHA256).create_record()
    return record.data, record.event_id


def make_valid_records(run_id: str, count: int) -> list[tuple[bytes, str]]:
    if count < 1:
        raise ValueError("count must be positive")
    factory = PayloadFactory(PAYLOAD_PATH, run_id, expected_sha256=EXPECTED_POOL_SHA256)
    records: list[tuple[bytes, str]] = []
    while len(records) < count:
        batch = factory.create_batch(min(500, count - len(records)))
        records.extend((record.data, record.event_id) for record in batch)
    return records


def wait_until(
    description: str,
    timeout_seconds: int,
    interval_seconds: int,
    probe: Callable[[], Any],
    accept: Callable[[Any], bool],
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last: Any = None
    while True:
        last = probe()
        if accept(last):
            return last
        if time.monotonic() >= deadline:
            raise WaitTimeoutError(description, last)
        time.sleep(interval_seconds)


def sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def iso_value(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return None


def write_private(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")

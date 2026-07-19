#!/usr/bin/env python3
"""Run frozen Phase 7 AWS runtime stages without reading secret values."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from common import (
    EXPECTED_ACCOUNT,
    EXPECTED_OPERATOR_ARN,
    EXPECTED_REGION,
    expected_tags,
    file_sha256,
    parse_utc,
    read_json,
    tag_map,
    tags_match,
    write_json,
)


ROOT = Path(__file__).resolve().parents[3]
PHASE6 = ROOT / "performance-tests/phase6-archive"
sys.path.insert(0, str(PHASE6))
from seed_partition import (  # noqa: E402
    DEFAULT_SEED,
    FULL_SCALE_ROWS,
    GENERATOR_VERSION,
    GeneratorContract,
    seed_insert_sql,
    utc_source_partition,
)
from runtime_observability import (  # noqa: E402
    capture_haproxy,
    collect_observability,
    finish_score_capture,
    metric_value,
    parse_prometheus,
    start_score_capture,
)

RUNTIME_STACK = "LoopAdPerfPhase7IntegrationStack"
EXPECTED_RUNTIME_OUTPUT_KEYS = frozenset({
    "ProtocolEndpoint",
    "ProtocolConnectDnsName",
    "LoadGeneratorAutoScalingGroupName",
    "LoadGeneratorRoleArn",
    "LoadGeneratorCount",
    "OhaImage",
    "CollectorClusterName",
    "CollectorServiceName",
    "CollectorEndpoint",
    "HaproxyClusterName",
    "HaproxyServiceName",
    "HaproxyStatsPort",
    "HaproxyConfigSha256",
    "CollectorCloudMapServiceName",
    "StreamName",
    "ConsumerClusterName",
    "ConsumerServiceName",
    "ClickHouseClusterName",
    "ClickHouseServiceName",
    "ClickHouseEndpoint",
    "ArchiveBucketName",
    "FailureBucketName",
    "LeaseTableName",
    "WorkerMetricsTableName",
    "CoordinatorStateTableName",
    "ArchiveTaskDefinitionArn",
    "ArchiveClusterName",
    "ArchiveCapacityProviderName",
    "ArchiveSubnetIds",
    "ArchiveSecurityGroupId",
    "ClickHouseSecretArn",
    "CollectorAutoScalingGroupName",
    "HaproxyAutoScalingGroupName",
    "ConsumerAutoScalingGroupName",
    "ClickHouseAutoScalingGroupName",
})
RUN_ID_PATTERN = re.compile(r"^run_[0-9]{8}_[0-9]{6}_phase7_integration$")
SESSION_ID_PATTERN = re.compile(r"^phase7-integration-[0-9]{8}T[0-9]{6}Z$")
SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "total_max_attempts": 5},
    user_agent_appid="loopad-phase7-runtime/1",
)
TOPOLOGY_CONTRACT_PATH = ROOT / "performance-tests/phase7-integration/topology-contract.json"
TOPOLOGY = read_json(TOPOLOGY_CONTRACT_PATH)
VISIBILITY_BUCKETS_MS = (100, 250, 500, 1_000, 2_000, 5_000, 10_000, 30_000, 60_000)
EXPECTED_COUNTS = {
    "Collector": int(TOPOLOGY["collectorHosts"]),
    "Haproxy": int(TOPOLOGY["haproxyHosts"]),
    "Consumer": int(TOPOLOGY["consumerHosts"]),
    "ClickHouse": int(TOPOLOGY["clickHouseHosts"]),
}


@dataclass(frozen=True)
class Bundle:
    run_dir: Path
    run_id: str
    session_id: str
    outputs: dict[str, str]


class AwsRuntime:
    def __init__(self, bundle: Bundle) -> None:
        self.bundle = bundle
        self.session = boto3.Session(region_name=EXPECTED_REGION)
        self._clients: dict[str, Any] = {}

    def client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = self.session.client(
                service, region_name=EXPECTED_REGION, config=SDK_CONFIG
            )
        return self._clients[service]

    def assert_identity(self) -> dict[str, str]:
        identity = self.client("sts").get_caller_identity()
        if identity.get("Account") != EXPECTED_ACCOUNT or identity.get("Arn") != EXPECTED_OPERATOR_ARN:
            raise RuntimeError("exact user-approved AWS identity is required")
        return {"account": str(identity["Account"]), "arn": str(identity["Arn"])}

    def asg_instances(self, output_key: str, expected: int) -> list[str]:
        name = self.bundle.outputs[output_key]
        groups = self.client("autoscaling").describe_auto_scaling_groups(
            AutoScalingGroupNames=[name]
        )["AutoScalingGroups"]
        if len(groups) != 1:
            raise RuntimeError(f"expected one ASG for {output_key}")
        group = groups[0]
        instances = sorted(
            item["InstanceId"]
            for item in group.get("Instances", [])
            if item.get("LifecycleState") == "InService"
        )
        if (
            int(group.get("DesiredCapacity", -1)) != expected
            or int(group.get("MinSize", -1)) != expected
            or int(group.get("MaxSize", -1)) != expected
            or len(instances) != expected
        ):
            raise RuntimeError(f"{output_key} does not have the exact fixed capacity {expected}")
        return instances

    def service_snapshot(self, role: str) -> dict[str, Any]:
        cluster = self.bundle.outputs[f"{role}ClusterName"]
        service_name = self.bundle.outputs[f"{role}ServiceName"]
        ecs = self.client("ecs")
        services = ecs.describe_services(cluster=cluster, services=[service_name])["services"]
        if len(services) != 1:
            raise RuntimeError(f"expected one {role} service")
        service = services[0]
        task_arns = sorted(
            arn
            for page in ecs.get_paginator("list_tasks").paginate(
                cluster=cluster, serviceName=service_name, desiredStatus="RUNNING"
            )
            for arn in page.get("taskArns", [])
        )
        tasks = ecs.describe_tasks(cluster=cluster, tasks=task_arns).get("tasks", []) if task_arns else []
        documents = [
            {
                "taskArn": task["taskArn"],
                "taskDefinitionArn": task["taskDefinitionArn"],
                "containerInstanceArn": task.get("containerInstanceArn"),
                "healthStatus": task.get("healthStatus"),
                "lastStatus": task.get("lastStatus"),
                "startedAt": iso(task.get("startedAt")),
            }
            for task in tasks
        ]
        return {
            "cluster": cluster,
            "service": service_name,
            "desiredCount": int(service.get("desiredCount", 0)),
            "runningCount": int(service.get("runningCount", 0)),
            "pendingCount": int(service.get("pendingCount", 0)),
            "taskDefinition": service.get("taskDefinition"),
            "capacityProviderStrategy": service.get("capacityProviderStrategy", []),
            "launchType": service.get("launchType"),
            "deploymentCount": len(service.get("deployments", [])),
            "tasks": documents,
        }

    def wait_service(self, role: str, timeout: int = 900) -> dict[str, Any]:
        expected = EXPECTED_COUNTS[role]
        return wait_until(
            f"{role} service ready",
            timeout,
            10,
            lambda: self.service_snapshot(role),
            lambda value: (
                value["desiredCount"] == expected
                and value["runningCount"] == expected
                and value["pendingCount"] == 0
                and len(value["tasks"]) == expected
                and all(task["lastStatus"] == "RUNNING" for task in value["tasks"])
                and all(task["healthStatus"] in {"HEALTHY", "UNKNOWN"} for task in value["tasks"])
            ),
        )

    def run_ssm(self, instance_id: str, commands: list[str], timeout: int = 180) -> str:
        ssm = self.client("ssm")
        sent = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            TimeoutSeconds=timeout,
            Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
            Comment=f"Phase 7 run-scoped validation for {self.bundle.run_id}",
        )
        command_id = sent["Command"]["CommandId"]
        deadline = time.monotonic() + timeout + 30
        invocation: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            time.sleep(3)
            try:
                invocation = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") == "InvocationDoesNotExist":
                    continue
                raise
            if invocation.get("Status") not in {"Pending", "InProgress", "Delayed"}:
                break
        if not invocation or invocation.get("Status") != "Success" or invocation.get("ResponseCode") != 0:
            detail = "missing invocation" if not invocation else str(invocation.get("StandardErrorContent", ""))[:512]
            raise RuntimeError(f"SSM command failed without secret output: {detail}")
        return str(invocation.get("StandardOutputContent", ""))

    def clickhouse_instance(self) -> str:
        return self.asg_instances("ClickHouseAutoScalingGroupName", 1)[0]

    def clickhouse(self, query: str, *, select: bool = True, timeout: int = 600) -> list[dict[str, Any]]:
        if select and not query.lstrip().upper().startswith(("SELECT", "DESCRIBE")):
            raise ValueError("read-only ClickHouse helper accepts SELECT or DESCRIBE")
        encoded = base64.b64encode(query.encode()).decode()
        format_arg = " --format JSONEachRow" if select else ""
        command = (
            "container=$(docker ps --filter label=com.amazonaws.ecs.container-name=clickhouse "
            "--format '{{.ID}}' | head -1); test -n \"$container\"; "
            f"printf '%s' '{encoded}' | base64 -d | docker exec -i \"$container\" sh -lc "
            f"'clickhouse-client --user \"$CLICKHOUSE_USER\" --password \"$CLICKHOUSE_PASSWORD\"{format_arg}'"
        )
        output = self.run_ssm(self.clickhouse_instance(), [command], timeout=timeout)
        return [json.loads(line) for line in output.splitlines() if line.strip()] if select else []

    def lease_snapshot(self) -> dict[str, Any]:
        client = self.client("dynamodb")
        items = [
            item
            for page in client.get_paginator("scan").paginate(
                TableName=self.bundle.outputs["LeaseTableName"], ConsistentRead=True
            )
            for item in page.get("Items", [])
        ]
        owners: dict[str, int] = {}
        checkpoints = 0
        for item in items:
            owner = item.get("leaseOwner", {}).get("S", "")
            if owner:
                owners[owner] = owners.get(owner, 0) + 1
            checkpoint = item.get("checkpoint", {}).get("S", "")
            if re.fullmatch(r"[0-9]+", checkpoint):
                checkpoints += 1
        canonical = json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
        return {
            "count": len(items),
            "ownerCounts": dict(sorted(owners.items())),
            "numericCheckpointCount": checkpoints,
            "sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def put_records(self, records: list[tuple[bytes, str]]) -> dict[str, Any]:
        accepted = 0
        shard_ids: set[str] = set()
        client = self.client("kinesis")
        for offset in range(0, len(records), 500):
            chunk = records[offset : offset + 500]
            response = client.put_records(
                StreamName=self.bundle.outputs["StreamName"],
                Records=[{"Data": data, "PartitionKey": key} for data, key in chunk],
            )
            if response.get("FailedRecordCount") or len(response.get("Records", [])) != len(chunk):
                raise RuntimeError("Kinesis PutRecords was not fully accepted")
            if any(value.get("ErrorCode") for value in response["Records"]):
                raise RuntimeError("Kinesis PutRecords contained an entry failure")
            accepted += len(chunk)
            shard_ids.update(value["ShardId"] for value in response["Records"])
        return {"accepted": accepted, "failed": 0, "shardIds": sorted(shard_ids)}

    def metric_sum(self, namespace: str, metric: str, dimensions: list[dict[str, str]], minutes: int = 15) -> float:
        end = datetime.now(UTC)
        response = self.client("cloudwatch").get_metric_statistics(
            Namespace=namespace,
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=end - timedelta(minutes=minutes),
            EndTime=end,
            Period=60,
            Statistics=["Sum"],
        )
        return sum(float(point.get("Sum", 0)) for point in response.get("Datapoints", []))

    def iterator_age(self, minutes: int = 10) -> float | None:
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
        points = sorted(response.get("Datapoints", []), key=lambda item: item["Timestamp"])
        return float(points[-1]["Maximum"]) if points and "Maximum" in points[-1] else None

    def iterator_age_samples(self, since_epoch: int) -> list[dict[str, Any]]:
        end = datetime.now(UTC)
        response = self.client("cloudwatch").get_metric_statistics(
            Namespace="AWS/Kinesis",
            MetricName="GetRecords.IteratorAgeMilliseconds",
            Dimensions=[{"Name": "StreamName", "Value": self.bundle.outputs["StreamName"]}],
            StartTime=datetime.fromtimestamp(since_epoch, UTC),
            EndTime=end,
            Period=60,
            Statistics=["Maximum"],
        )
        return [
            {
                "timestamp": iso(point["Timestamp"]),
                "epoch": int(point["Timestamp"].timestamp()),
                "maximumMs": float(point["Maximum"]),
            }
            for point in sorted(response.get("Datapoints", []), key=lambda item: item["Timestamp"])
            if "Maximum" in point and int(point["Timestamp"].timestamp()) >= since_epoch
        ]

    def kinesis_incoming_records(self, start_epoch: int, end_epoch: int) -> int:
        response = self.client("cloudwatch").get_metric_statistics(
            Namespace="AWS/Kinesis",
            MetricName="IncomingRecords",
            Dimensions=[{"Name": "StreamName", "Value": self.bundle.outputs["StreamName"]}],
            StartTime=datetime.fromtimestamp(start_epoch, UTC),
            EndTime=datetime.fromtimestamp(end_epoch, UTC),
            Period=60,
            Statistics=["Sum"],
        )
        return int(round(sum(float(point.get("Sum", 0)) for point in response.get("Datapoints", []))))

    def successful_consumer_input(self, start_epoch: int, end_epoch: int) -> dict[str, int]:
        logs = self.client("logs")
        response = logs.start_query(
            logGroupName=f"/loopad/perf/phase7/{self.bundle.run_id}/ConsumerLogs",
            startTime=start_epoch,
            endTime=end_epoch,
            queryString=(
                "fields @message | filter event = 'phase4_batch_success' "
                "| stats sum(inputRecords) as processed, count(*) as batches, max(attempts) as maxAttempts"
            ),
            limit=10,
        )
        query_id = response["queryId"]
        deadline = time.monotonic() + 300
        result: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            result = logs.get_query_results(queryId=query_id)
            if result.get("status") in {"Complete", "Failed", "Cancelled", "Timeout", "Unknown"}:
                break
            time.sleep(3)
        if not result or result.get("status") != "Complete" or len(result.get("results", [])) != 1:
            raise RuntimeError("consumer CloudWatch Logs Insights accounting did not complete")
        fields = {item["field"]: item["value"] for item in result["results"][0]}
        return {
            "processed": int(float(fields.get("processed", "0"))),
            "batches": int(float(fields.get("batches", "0"))),
            "maxAttempts": int(float(fields.get("maxAttempts", "0"))),
        }

    def visibility_histogram(self, start_epoch: int, end_epoch: int) -> dict[str, int]:
        fields = ["observedRecords", *[f"latencyLe{bound}Ms" for bound in VISIBILITY_BUCKETS_MS], "latencyGt60000Ms"]
        query = (
            "fields @message | filter event = 'phase7_visibility_histogram' | stats "
            + ", ".join(f"sum({field}) as {field}" for field in fields)
        )
        logs = self.client("logs")
        response = logs.start_query(
            logGroupName=f"/loopad/perf/phase7/{self.bundle.run_id}/ConsumerLogs",
            startTime=start_epoch,
            endTime=end_epoch,
            queryString=query,
            limit=10,
        )
        query_id = response["queryId"]
        deadline = time.monotonic() + 300
        result: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            result = logs.get_query_results(queryId=query_id)
            if result.get("status") in {"Complete", "Failed", "Cancelled", "Timeout", "Unknown"}:
                break
            time.sleep(3)
        if not result or result.get("status") != "Complete" or len(result.get("results", [])) != 1:
            raise RuntimeError("consumer visibility histogram query did not complete")
        values = {item["field"]: item["value"] for item in result["results"][0]}
        return {field: int(float(values.get(field, "0"))) for field in fields}


def load_bundle(run_dir: Path) -> Bundle:
    run = read_json(run_dir / "run.json")
    run_id = str(run.get("runId", ""))
    session_id = str(run.get("sessionId", ""))
    if not RUN_ID_PATTERN.fullmatch(run_id) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("runtime run identifiers are invalid")
    output_document = read_json(run_dir / "cdk-outputs.json")
    raw_outputs = output_document.get(RUNTIME_STACK)
    if not isinstance(raw_outputs, dict):
        raise ValueError("runtime CDK outputs are missing")
    if any(
        not isinstance(key, str) or not key or not isinstance(value, str) or not value
        for key, value in raw_outputs.items()
    ):
        raise ValueError("runtime CDK outputs must contain nonempty string keys and values")
    outputs = dict(raw_outputs)
    missing = sorted(EXPECTED_RUNTIME_OUTPUT_KEYS.difference(outputs))
    unexpected = sorted(set(outputs).difference(EXPECTED_RUNTIME_OUTPUT_KEYS))
    if missing or unexpected:
        raise ValueError(
            "runtime CDK outputs do not have the exact expected key set: "
            f"missing={missing}, unexpected={unexpected}"
        )
    return Bundle(run_dir.resolve(), run_id, session_id, outputs)


def verify_cloudformation_outputs(
    stack: dict[str, Any],
    expected_outputs: dict[str, str],
) -> dict[str, str]:
    if (
        set(expected_outputs) != EXPECTED_RUNTIME_OUTPUT_KEYS
        or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in expected_outputs.items()
        )
    ):
        raise RuntimeError("local runtime outputs do not have the exact expected contract")
    rows = stack.get("Outputs")
    if not isinstance(rows, list):
        raise RuntimeError("CloudFormation stack outputs are missing")
    actual: dict[str, str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RuntimeError(f"CloudFormation output row {index} is malformed")
        key = row.get("OutputKey")
        value = row.get("OutputValue")
        if not isinstance(key, str) or not key or not isinstance(value, str) or not value:
            raise RuntimeError(f"CloudFormation output row {index} is malformed")
        if key in actual:
            raise RuntimeError(f"CloudFormation output key is duplicated: {key}")
        actual[key] = value
    missing = sorted(EXPECTED_RUNTIME_OUTPUT_KEYS.difference(actual))
    unexpected = sorted(set(actual).difference(EXPECTED_RUNTIME_OUTPUT_KEYS))
    mismatched = sorted(
        key for key in EXPECTED_RUNTIME_OUTPUT_KEYS.intersection(actual)
        if actual[key] != expected_outputs[key]
    )
    if missing or unexpected or mismatched:
        raise RuntimeError(
            "CloudFormation outputs do not exactly match the local deployment outputs: "
            f"missing={missing}, unexpected={unexpected}, mismatched={mismatched}"
        )
    return actual


def deployment_contract(bundle: Bundle) -> dict[str, Any]:
    preflight = read_json(bundle.run_dir / "inputs" / "preflight.json")
    image_manifest = read_json(bundle.run_dir / "inputs" / "image-manifest.json")
    for document in (preflight, image_manifest):
        if document.get("runId") != bundle.run_id or document.get("sessionId") != bundle.session_id:
            raise RuntimeError("deployment input belongs to another run")
    if preflight.get("passed") is not True or image_manifest.get("runtimeDeployed") is not False:
        raise RuntimeError("deployment inputs do not authorize one fresh runtime deployment")
    image_by_role = {
        str(item.get("role")): item
        for item in image_manifest.get("images", [])
        if isinstance(item, dict)
    }
    if set(image_by_role) != {"collector", "consumer", "archive"}:
        raise RuntimeError("deployment image manifest must contain the exact three roles")
    for role, item in image_by_role.items():
        digest = str(item.get("digest", ""))
        exact = str(item.get("exactImage", ""))
        repository = str(item.get("repository", ""))
        if (
            re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            or not exact.endswith(f"@{digest}")
            or repository != f"loop-ad/perf-phase7/{bundle.run_id}/{role}"
        ):
            raise RuntimeError(f"deployment image contract is invalid for {role}")
    snapshot = preflight.get("snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError("preflight snapshot is missing")
    return {"preflight": preflight, "snapshot": snapshot, "images": image_by_role}


def verify_owned_role(aws: AwsRuntime, role_arn: str) -> dict[str, str]:
    if re.fullmatch(rf"arn:aws:iam::{EXPECTED_ACCOUNT}:role/.+", role_arn) is None:
        raise RuntimeError("runtime IAM role ARN is outside the approved account")
    role_name = role_arn.rsplit("/", 1)[-1]
    tags = tag_map(aws.client("iam").list_role_tags(RoleName=role_name).get("Tags", []))
    if not tags_match(tags, aws.bundle.run_id, aws.bundle.session_id):
        raise RuntimeError(f"runtime IAM role is not exactly run-owned: {role_name}")
    return {"arn": role_arn, "name": role_name}


def verify_instance_contract(
    aws: AwsRuntime,
    asgs: dict[str, list[str]],
    contract: dict[str, Any],
) -> dict[str, Any]:
    ec2 = aws.client("ec2")
    expected_ids = {instance_id for values in asgs.values() for instance_id in values}
    reservations = ec2.describe_instances(InstanceIds=sorted(expected_ids)).get("Reservations", [])
    instances = [instance for reservation in reservations for instance in reservation.get("Instances", [])]
    by_id = {str(instance.get("InstanceId")): instance for instance in instances}
    if set(by_id) != expected_ids:
        raise RuntimeError("EC2 instance inventory differs from the exact ASG inventory")
    snapshot = contract["snapshot"]
    expected_amis = {
        "loadGenerator": snapshot["amis"]["x86"]["imageId"],
        "collector": snapshot["amis"]["x86"]["imageId"],
        "haproxy": snapshot["amis"]["x86"]["imageId"],
        "consumer": snapshot["amis"]["arm"]["imageId"],
        "clickHouse": snapshot["amis"]["arm"]["imageId"],
    }
    expected_types = {key: str(value) for key, value in TOPOLOGY["hostInstanceTypes"].items()}
    observed: dict[str, list[dict[str, Any]]] = {}
    role_security_groups: dict[str, str] = {}
    vpc_ids: set[str] = set()
    subnet_ids: set[str] = set()
    profile_arns: set[str] = set()
    for role, instance_ids in asgs.items():
        role_rows = []
        security_groups: set[str] = set()
        for instance_id in instance_ids:
            instance = by_id[instance_id]
            tags = tag_map(instance.get("Tags", []))
            metadata = instance.get("MetadataOptions", {})
            groups = {str(group["GroupId"]) for group in instance.get("SecurityGroups", [])}
            profile = str(instance.get("IamInstanceProfile", {}).get("Arn", ""))
            if (
                instance.get("State", {}).get("Name") != "running"
                or instance.get("ImageId") != expected_amis[role]
                or instance.get("InstanceType") != expected_types[role]
                or metadata.get("HttpTokens") != "required"
                or metadata.get("HttpEndpoint") != "enabled"
                or metadata.get("State") != "applied"
                or instance.get("PublicIpAddress") is not None
                or len(groups) != 1
                or not profile
                or not tags_match(tags, aws.bundle.run_id, aws.bundle.session_id)
            ):
                raise RuntimeError(f"EC2 runtime contract mismatch for {role}/{instance_id}")
            security_groups.update(groups)
            vpc_ids.add(str(instance["VpcId"]))
            subnet_ids.add(str(instance["SubnetId"]))
            profile_arns.add(profile)
            role_rows.append({
                "instanceId": instance_id,
                "imageId": instance["ImageId"],
                "instanceType": instance["InstanceType"],
                "subnetId": instance["SubnetId"],
                "securityGroupId": next(iter(groups)),
                "metadataTokens": metadata["HttpTokens"],
                "publicIp": False,
            })
        if len(security_groups) != 1:
            raise RuntimeError(f"{role} hosts do not use one exact security group")
        role_security_groups[role] = next(iter(security_groups))
        observed[role] = role_rows
    if len(vpc_ids) != 1:
        raise RuntimeError("Phase 7 hosts span more than one VPC")
    vpc_id = next(iter(vpc_ids))

    subnets = {
        item["SubnetId"]: item
        for item in ec2.describe_subnets(SubnetIds=sorted(subnet_ids)).get("Subnets", [])
    }
    if set(subnets) != subnet_ids or any(
        item.get("VpcId") != vpc_id or item.get("MapPublicIpOnLaunch") is not False
        for item in subnets.values()
    ):
        raise RuntimeError("runtime hosts are not confined to exact private subnets")
    nat_gateway_ids: set[str] = set()
    for subnet_id in sorted(subnet_ids):
        tables = ec2.describe_route_tables(Filters=[{
            "Name": "association.subnet-id", "Values": [subnet_id]
        }]).get("RouteTables", [])
        routes = [route for table in tables for route in table.get("Routes", [])]
        default_routes = [
            route for route in routes
            if route.get("DestinationCidrBlock") == "0.0.0.0/0"
            and route.get("State") == "active" and route.get("NatGatewayId")
        ]
        if len(default_routes) != 1:
            raise RuntimeError(f"private subnet lacks one active NAT route: {subnet_id}")
        nat_gateway_ids.add(str(default_routes[0]["NatGatewayId"]))
    gateways = ec2.describe_nat_gateways(NatGatewayIds=sorted(nat_gateway_ids)).get("NatGateways", [])
    if {item.get("NatGatewayId") for item in gateways} != nat_gateway_ids or any(
        item.get("State") != "available" or item.get("VpcId") != vpc_id for item in gateways
    ):
        raise RuntimeError("runtime NAT gateway is not available in the run VPC")
    endpoints = ec2.describe_vpc_endpoints(Filters=[{
        "Name": "vpc-id", "Values": [vpc_id]
    }]).get("VpcEndpoints", [])
    available_gateway_services = {
        item.get("ServiceName") for item in endpoints
        if item.get("VpcEndpointType") == "Gateway" and item.get("State") == "available"
    }
    required_gateway_services = {
        f"com.amazonaws.{EXPECTED_REGION}.s3",
        f"com.amazonaws.{EXPECTED_REGION}.dynamodb",
    }
    if not required_gateway_services.issubset(available_gateway_services):
        raise RuntimeError("required S3 and DynamoDB gateway endpoints are not available")

    instance_profile_roles = []
    iam = aws.client("iam")
    for profile_arn in sorted(profile_arns):
        profile_name = profile_arn.rsplit("/", 1)[-1]
        profile = iam.get_instance_profile(InstanceProfileName=profile_name)["InstanceProfile"]
        roles = profile.get("Roles", [])
        if len(roles) != 1:
            raise RuntimeError("runtime instance profile must contain exactly one role")
        role = verify_owned_role(aws, str(roles[0]["Arn"]))
        instance_profile_roles.append({"profileArn": profile_arn, "role": role})

    clickhouse = by_id[asgs["clickHouse"][0]]
    clickhouse_mappings = clickhouse.get("BlockDeviceMappings", [])
    if len(clickhouse_mappings) != 1 or clickhouse_mappings[0].get("Ebs", {}).get("DeleteOnTermination") is not True:
        raise RuntimeError("ClickHouse must have one delete-on-termination EBS data/root volume")
    volume_id = clickhouse_mappings[0]["Ebs"]["VolumeId"]
    volumes = ec2.describe_volumes(VolumeIds=[volume_id]).get("Volumes", [])
    if len(volumes) != 1:
        raise RuntimeError("ClickHouse EBS volume is missing")
    volume = volumes[0]
    if any((
        volume.get("Encrypted") is not True,
        volume.get("VolumeType") != "gp3",
        int(volume.get("Size", 0)) != 500,
        int(volume.get("Iops", 0)) != 3_000,
        int(volume.get("Throughput", 0)) != 500,
        volume.get("State") != "in-use",
    )):
        raise RuntimeError("ClickHouse EBS volume differs from the exact encrypted gp3 contract")
    return {
        "vpcId": vpc_id,
        "subnetIds": sorted(subnet_ids),
        "natGatewayIds": sorted(nat_gateway_ids),
        "gatewayEndpoints": sorted(required_gateway_services),
        "roleSecurityGroups": role_security_groups,
        "instances": observed,
        "instanceProfileRoles": instance_profile_roles,
        "clickHouseVolume": {
            "volumeId": volume_id, "encrypted": True, "type": "gp3",
            "sizeGiB": 500, "iops": 3_000, "throughput": 500,
        },
    }


def validate_task_definition(
    aws: AwsRuntime,
    response: dict[str, Any],
    role: str,
    expected_architecture: str,
    expected_containers: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    task = response.get("taskDefinition", {})
    tags = tag_map(response.get("tags", []))
    if (
        task.get("networkMode") != "awsvpc"
        or task.get("runtimePlatform", {}).get("cpuArchitecture") != expected_architecture
        or task.get("runtimePlatform", {}).get("operatingSystemFamily") != "LINUX"
        or not tags_match(tags, aws.bundle.run_id, aws.bundle.session_id)
    ):
        raise RuntimeError(f"{role} task definition platform, network, or ownership mismatch")
    task_role = verify_owned_role(aws, str(task.get("taskRoleArn", "")))
    execution_role = verify_owned_role(aws, str(task.get("executionRoleArn", "")))
    if task_role["arn"] == execution_role["arn"]:
        raise RuntimeError(f"{role} task and execution roles must be distinct")
    containers = {
        str(item.get("name")): item for item in task.get("containerDefinitions", [])
    }
    if set(containers) != set(expected_containers):
        raise RuntimeError(f"{role} task definition has an unexpected container set")
    summaries = []
    for name, expected in expected_containers.items():
        container = containers[name]
        environment_rows = container.get("environment", [])
        environment = {
            str(item.get("name")): str(item.get("value"))
            for item in environment_rows
        }
        if len(environment) != len(environment_rows):
            raise RuntimeError(f"{role}/{name} container has duplicate environment names")
        health = container.get("healthCheck")
        if health is not None and not 1 <= int(health.get("retries", 0)) <= 10:
            raise RuntimeError(f"{role}/{name} health retries are outside the ECS 1..10 range")
        if (
            container.get("image") != expected["image"]
            or int(container.get("cpu", -1)) != expected["cpu"]
            or int(container.get("memory", -1)) != expected["memory"]
            or bool(container.get("essential", True)) is not True
            or container.get("logConfiguration", {}).get("logDriver") != "awslogs"
            or any(environment.get(key) != value for key, value in expected.get("environment", {}).items())
        ):
            raise RuntimeError(f"{role}/{name} container contract mismatch")
        summaries.append({
            "name": name,
            "image": container["image"],
            "cpu": container["cpu"],
            "memoryMiB": container["memory"],
            "healthRetries": None if health is None else int(health["retries"]),
            "verifiedEnvironment": {
                key: environment[key]
                for key in sorted(expected.get("environment", {}))
            },
        })
    return {
        "taskDefinitionArn": task["taskDefinitionArn"],
        "architecture": expected_architecture,
        "taskRole": task_role,
        "executionRole": execution_role,
        "containers": summaries,
    }


def expected_ecs_task_definitions(
    bundle: Bundle,
    images: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, dict[str, dict[str, Any]]]]:
    outputs = bundle.outputs
    clickhouse_endpoint = outputs["ClickHouseEndpoint"]
    stream_name = outputs["StreamName"]
    return {
        "Collector": ("X86_64", {
            "collector": {
                "image": images["collector"]["exactImage"],
                "cpu": 3_584,
                "memory": 3_584,
                "environment": {
                    "LOOPAD_ENV": "phase7-integration",
                    "AWS_REGION": EXPECTED_REGION,
                    "LOOPAD_KINESIS_STREAM_NAME": stream_name,
                },
            },
        }),
        "Haproxy": ("X86_64", {
            "haproxy": {
                "image": TOPOLOGY["fixedImages"]["haproxy"],
                "cpu": 3_584,
                "memory": 3_584,
            },
        }),
        "Consumer": ("ARM64", {
            "consumer": {
                "image": images["consumer"]["exactImage"],
                "cpu": 1_024,
                "memory": 2_048,
                "environment": {
                    "AWS_REGION": EXPECTED_REGION,
                    "RUN_ID": bundle.run_id,
                    "KINESIS_STREAM_NAME": stream_name,
                    "KINESIS_STREAM_ARN": (
                        f"arn:aws:kinesis:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:stream/{stream_name}"
                    ),
                    "KCL_APPLICATION_NAME": f"{bundle.run_id}-consumer",
                    "KCL_LEASE_TABLE_NAME": outputs["LeaseTableName"],
                    "KCL_WORKER_METRICS_TABLE_NAME": outputs["WorkerMetricsTableName"],
                    "KCL_COORDINATOR_STATE_TABLE_NAME": outputs["CoordinatorStateTableName"],
                    "CLICKHOUSE_HTTP_URL": clickhouse_endpoint,
                    "CLICKHOUSE_SECRET_ARN": outputs["ClickHouseSecretArn"],
                    "FAILURE_BUCKET": outputs["FailureBucketName"],
                    "FAILURE_PREFIX": f"failures/{bundle.run_id}/",
                    "PHASE7_LOCAL_MODE": "false",
                },
            },
        }),
        "ClickHouse": ("ARM64", {
            "clickhouse": {
                "image": TOPOLOGY["fixedImages"]["clickHouse"],
                "cpu": 4_096,
                "memory": 8_192,
            },
            "schema-guard": {
                "image": images["archive"]["exactImage"],
                "cpu": 128,
                "memory": 256,
                "environment": {"CLICKHOUSE_HTTP_URL": "http://127.0.0.1:8123"},
            },
        }),
        "Archive": ("ARM64", {
            "archive": {
                "image": images["archive"]["exactImage"],
                "cpu": 1_024,
                "memory": 2_048,
                "environment": {
                    "AWS_REGION": EXPECTED_REGION,
                    "AWS_ACCOUNT_ID": EXPECTED_ACCOUNT,
                    "RUN_ID": bundle.run_id,
                    "CLICKHOUSE_HTTP_URL": clickhouse_endpoint,
                    "CLICKHOUSE_IMAGE": TOPOLOGY["fixedImages"]["clickHouse"],
                    "ARCHIVE_IMAGE_DIGEST": images["archive"]["digest"],
                    "ARCHIVE_BUCKET": outputs["ArchiveBucketName"],
                    "ARCHIVE_EXPECTED_ROWS": "15000000",
                    "ARCHIVE_ROWS_PER_PART": "5000000",
                    "ARCHIVE_PART_COUNT": "3",
                    "ARCHIVE_PARTITION": "OVERRIDE_REQUIRED",
                    "ARCHIVE_TODAY": "OVERRIDE_REQUIRED",
                },
            },
        }),
    }


def verify_ecs_contract(
    aws: AwsRuntime,
    services: dict[str, dict[str, Any]],
    asgs: dict[str, list[str]],
    contract: dict[str, Any],
) -> dict[str, Any]:
    ecs = aws.client("ecs")
    images = contract["images"]
    expected = expected_ecs_task_definitions(aws.bundle, images)
    role_to_asg = {
        "Collector": "collector", "Haproxy": "haproxy",
        "Consumer": "consumer", "ClickHouse": "clickHouse",
    }
    result = {}
    for role, service in services.items():
        asg_role = role_to_asg[role]
        strategies = service.get("capacityProviderStrategy", [])
        if (
            len(strategies) != 1
            or not strategies[0].get("capacityProvider")
            or int(strategies[0].get("weight", 0)) != 1
            or int(strategies[0].get("base", 0)) != 0
            or service.get("launchType") not in {None, ""}
            or service.get("deploymentCount") != 1
        ):
            raise RuntimeError(f"{role} service capacity provider or deployment contract mismatch")
        provider_name = str(strategies[0]["capacityProvider"])
        providers = ecs.describe_capacity_providers(capacityProviders=[provider_name]).get("capacityProviders", [])
        if len(providers) != 1:
            raise RuntimeError(f"{role} capacity provider is missing")
        provider = providers[0]
        provider_asg = provider.get("autoScalingGroupProvider", {})
        expected_asg_name = aws.bundle.outputs[f"{role}AutoScalingGroupName"]
        if (
            provider.get("status") != "ACTIVE"
            or not str(provider_asg.get("autoScalingGroupArn", "")).endswith(f"/{expected_asg_name}")
            or provider_asg.get("managedScaling", {}).get("status") != "DISABLED"
            or provider_asg.get("managedTerminationProtection") != "DISABLED"
            or provider_asg.get("managedDraining", "DISABLED") != "DISABLED"
        ):
            raise RuntimeError(f"{role} capacity provider differs from the fixed-capacity contract")
        cluster = service["cluster"]
        container_instance_arns = sorted({
            str(task.get("containerInstanceArn")) for task in service["tasks"]
        })
        container_instances = ecs.describe_container_instances(
            cluster=cluster, containerInstances=container_instance_arns
        ).get("containerInstances", [])
        placed_ids = {str(item.get("ec2InstanceId")) for item in container_instances}
        if (
            len(container_instances) != len(asgs[asg_role])
            or placed_ids != set(asgs[asg_role])
            or any(item.get("status") != "ACTIVE" or item.get("agentConnected") is not True for item in container_instances)
        ):
            raise RuntimeError(f"{role} tasks are not placed one-per-active run-owned host")
        task_definitions = {str(task.get("taskDefinitionArn")) for task in service["tasks"]}
        if task_definitions != {service.get("taskDefinition")}:
            raise RuntimeError(f"{role} running tasks do not use one exact service task definition")
        response = ecs.describe_task_definition(
            taskDefinition=str(service["taskDefinition"]), include=["TAGS"]
        )
        task_summary = validate_task_definition(aws, response, role, *expected[role])
        result[role] = {
            "service": service,
            "capacityProvider": provider_name,
            "placedEc2InstanceIds": sorted(placed_ids),
            "taskDefinition": task_summary,
        }

    archive_response = ecs.describe_task_definition(
        taskDefinition=aws.bundle.outputs["ArchiveTaskDefinitionArn"], include=["TAGS"]
    )
    archive = validate_task_definition(aws, archive_response, "Archive", *expected["Archive"])
    archive_provider_name = aws.bundle.outputs["ArchiveCapacityProviderName"]
    archive_providers = ecs.describe_capacity_providers(
        capacityProviders=[archive_provider_name]
    ).get("capacityProviders", [])
    if len(archive_providers) != 1 or not str(
        archive_providers[0].get("autoScalingGroupProvider", {}).get("autoScalingGroupArn", "")
    ).endswith(f"/{aws.bundle.outputs['ClickHouseAutoScalingGroupName']}"):
        raise RuntimeError("archive task is not pinned to the ClickHouse run-owned capacity provider")
    archive_family = archive_response["taskDefinition"]["family"]
    archive_tasks = {
        status: ecs.list_tasks(
            cluster=aws.bundle.outputs["ArchiveClusterName"],
            family=archive_family,
            desiredStatus=status,
        ).get("taskArns", [])
        for status in ("RUNNING", "STOPPED")
    }
    if any(archive_tasks.values()):
        raise RuntimeError("archive task already ran or started before the one permitted score overlap")
    result["Archive"] = {
        "taskDefinition": archive,
        "capacityProvider": archive_provider_name,
        "priorTaskInventory": {"running": 0, "stopped": 0},
    }
    return result


def verify_protocol_path(
    aws: AwsRuntime,
    instances: dict[str, Any],
    contract: dict[str, Any],
    ca_certificate: Path,
) -> dict[str, Any]:
    connect_dns = aws.bundle.outputs["ProtocolConnectDnsName"]
    endpoint = aws.bundle.outputs["ProtocolEndpoint"]
    certificate = contract["snapshot"]["certificate"]
    if endpoint != f"https://{certificate['domainName']}":
        raise RuntimeError("protocol endpoint differs from the preflight certificate domain")
    elbv2 = aws.client("elbv2")
    load_balancers = [
        item
        for page in elbv2.get_paginator("describe_load_balancers").paginate()
        for item in page.get("LoadBalancers", [])
        if item.get("DNSName") == connect_dns
    ]
    if len(load_balancers) != 1:
        raise RuntimeError("protocol NLB DNS name does not resolve to one load balancer inventory record")
    load_balancer = load_balancers[0]
    tags = tag_map(elbv2.describe_tags(ResourceArns=[load_balancer["LoadBalancerArn"]])["TagDescriptions"][0].get("Tags", []))
    protocol_sg = instances["roleSecurityGroups"]
    if (
        load_balancer.get("LoadBalancerName")
        != expected_protocol_load_balancer_name(aws.bundle.session_id)
        or load_balancer.get("Scheme") != "internal"
        or load_balancer.get("Type") != "network"
        or load_balancer.get("State", {}).get("Code") != "active"
        or load_balancer.get("VpcId") != instances["vpcId"]
        or set(load_balancer.get("SecurityGroups", [])) != {protocol_sg["protocolLoadBalancer"]}
        or not tags_match(tags, aws.bundle.run_id, aws.bundle.session_id)
    ):
        raise RuntimeError("protocol NLB state, ownership, VPC, or security group mismatch")
    listeners = elbv2.describe_listeners(LoadBalancerArn=load_balancer["LoadBalancerArn"]).get("Listeners", [])
    if len(listeners) != 1:
        raise RuntimeError("protocol NLB must have exactly one listener")
    listener = listeners[0]
    certificates = elbv2.describe_listener_certificates(ListenerArn=listener["ListenerArn"]).get("Certificates", [])
    if (
        listener.get("Protocol") != "TLS" or int(listener.get("Port", 0)) != 443
        or listener.get("AlpnPolicy") != ["HTTP2Preferred"]
        or {item.get("CertificateArn") for item in certificates} != {certificate["arn"]}
    ):
        raise RuntimeError("protocol listener does not enforce the exact TLS/HTTP2/certificate contract")
    target_groups = elbv2.describe_target_groups(LoadBalancerArn=load_balancer["LoadBalancerArn"]).get("TargetGroups", [])
    if len(target_groups) != 1:
        raise RuntimeError("protocol NLB must have exactly one target group")
    target_group = target_groups[0]
    health = elbv2.describe_target_health(TargetGroupArn=target_group["TargetGroupArn"]).get("TargetHealthDescriptions", [])
    if (
        target_group.get("Protocol") != "TCP" or int(target_group.get("Port", 0)) != 8080
        or target_group.get("TargetType") != "ip" or len(health) != int(TOPOLOGY["haproxyHosts"])
        or any(item.get("TargetHealth", {}).get("State") != "healthy" for item in health)
    ):
        raise RuntimeError("protocol NLB target group is not exactly healthy on both HAProxy tasks")

    ec2 = aws.client("ec2")
    groups = ec2.describe_security_groups(GroupIds=[protocol_sg["protocolLoadBalancer"]]).get("SecurityGroups", [])
    if len(groups) != 1:
        raise RuntimeError("protocol NLB security group is missing")
    group = groups[0]
    if not exact_group_edge(group.get("IpPermissions", []), protocol_sg["loadGenerator"], 443):
        raise RuntimeError("protocol NLB ingress is not restricted to load generators on TCP/443")
    if not exact_group_edge(group.get("IpPermissionsEgress", []), protocol_sg["haproxy"], 8080):
        raise RuntimeError("protocol NLB egress is not restricted to HAProxy on TCP/8080")

    ca_bytes = ca_certificate.resolve().read_bytes()
    if not ca_bytes or len(ca_bytes) > 32_768:
        raise RuntimeError("protocol CA evidence must be a nonempty bundle no larger than 32 KiB")
    ca_base64 = base64.b64encode(ca_bytes).decode()
    worker_sha = file_sha256(ROOT / "performance-tests/phase1-kinesis/run-ec2-oha-worker.sh")
    oha_image = str(TOPOLOGY["fixedImages"]["oha"])
    readiness_command = (
        "set -euo pipefail; test \"$(systemctl is-active docker)\" = active; "
        "test -x /opt/loop-ad-phase1/run-ec2-oha-worker.sh; "
        f"test \"$(sha256sum /opt/loop-ad-phase1/run-ec2-oha-worker.sh | awk '{{print $1}}')\" = '{worker_sha}'; "
        f"docker image inspect '{oha_image}' >/dev/null; "
        f"printf '%s' '{ca_base64}' | base64 -d > /tmp/phase7-ca.pem; "
        "trap 'rm -f /tmp/phase7-ca.pem' EXIT; "
        f"curl --silent --show-error --fail --http2 --connect-timeout 5 --max-time 15 "
        f"--cacert /tmp/phase7-ca.pem --connect-to '{certificate['domainName']}:443:{connect_dns}:443' "
        f"'{endpoint}/health' >/dev/null; printf 'ready {worker_sha}'"
    )
    generator_readiness = {}
    for instance_id in sorted(instances["instances"]["loadGenerator"], key=lambda item: item["instanceId"]):
        identifier = instance_id["instanceId"]
        output = aws.run_ssm(identifier, [readiness_command], timeout=60).strip()
        if output != f"ready {worker_sha}":
            raise RuntimeError(f"load generator readiness failed: {identifier}")
        generator_readiness[identifier] = {"workerSha256": worker_sha, "ohaImage": oha_image, "tlsHttp2Health": True}

    haproxy_ids = [item["instanceId"] for item in instances["instances"]["haproxy"]]
    haproxy = capture_haproxy(aws, sorted(haproxy_ids))
    expected_config_sha = aws.bundle.outputs["HaproxyConfigSha256"]
    active_backends = []
    for identifier in sorted(haproxy):
        item = haproxy[identifier]
        if item["configSha256"] != expected_config_sha:
            raise RuntimeError("HAProxy runtime config hash differs from the CDK output")
        samples = parse_prometheus(item["metrics"])
        active_backends.append(int(metric_value(
            samples, "haproxy_backend_agg_server_status", proxy="collectors", state="UP"
        )))
    expected_active = int(TOPOLOGY["activeCollectorBackendsPerProxy"])
    if active_backends != [expected_active] * int(TOPOLOGY["haproxyHosts"]):
        raise RuntimeError("HAProxy does not expose the exact active collector backend topology")
    return {
        "dnsName": connect_dns,
        "loadBalancerName": load_balancer["LoadBalancerName"],
        "scheme": "internal",
        "listener": {"protocol": "TLS", "port": 443, "alpnPolicy": "HTTP2Preferred"},
        "targetHealth": [item["TargetHealth"]["State"] for item in health],
        "generatorReadiness": generator_readiness,
        "haproxyConfigSha256": expected_config_sha,
        "activeBackendsPerProxy": active_backends,
    }


def expected_protocol_load_balancer_name(session_id: str) -> str:
    if SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ValueError("invalid Phase 7 session ID")
    digits = "".join(character for character in session_id if character.isdigit())
    return f"perf-p1-conn-proxy-{digits[-11:]}"


def exact_group_edge(permissions: list[dict[str, Any]], peer_group_id: str, port: int) -> bool:
    if len(permissions) != 1:
        return False
    permission = permissions[0]
    pairs = permission.get("UserIdGroupPairs", [])
    return (
        permission.get("IpProtocol") == "tcp"
        and int(permission.get("FromPort", -1)) == port
        and int(permission.get("ToPort", -1)) == port
        and len(pairs) == 1
        and pairs[0].get("GroupId") == peer_group_id
        and not permission.get("IpRanges")
        and not permission.get("Ipv6Ranges")
        and not permission.get("PrefixListIds")
    )


def verify_deployment(aws: AwsRuntime, ca_certificate: Path) -> dict[str, Any]:
    identity = aws.assert_identity()
    contract = deployment_contract(aws.bundle)
    stacks = aws.client("cloudformation").describe_stacks(StackName=RUNTIME_STACK).get("Stacks", [])
    if len(stacks) != 1:
        raise RuntimeError("expected exactly one runtime CloudFormation stack")
    stack = stacks[0]
    tags = {item["Key"]: item["Value"] for item in stack.get("Tags", [])}
    if (
        stack["StackStatus"] != "CREATE_COMPLETE"
        or tags.get("RunId") != aws.bundle.run_id
        or tags.get("SessionId") != aws.bundle.session_id
        or tags.get("ResourceScope") != "run"
    ):
        raise RuntimeError("runtime stack is not exactly owned and CREATE_COMPLETE")
    stack_outputs = verify_cloudformation_outputs(stack, aws.bundle.outputs)
    services = {role: aws.wait_service(role) for role in EXPECTED_COUNTS}
    asgs = {
        "loadGenerator": aws.asg_instances("LoadGeneratorAutoScalingGroupName", int(TOPOLOGY["loadGeneratorHosts"])),
        "collector": aws.asg_instances("CollectorAutoScalingGroupName", int(TOPOLOGY["collectorHosts"])),
        "haproxy": aws.asg_instances("HaproxyAutoScalingGroupName", int(TOPOLOGY["haproxyHosts"])),
        "consumer": aws.asg_instances("ConsumerAutoScalingGroupName", int(TOPOLOGY["consumerHosts"])),
        "clickHouse": aws.asg_instances("ClickHouseAutoScalingGroupName", int(TOPOLOGY["clickHouseHosts"])),
    }
    instance_contract = verify_instance_contract(aws, asgs, contract)
    # The protocol NLB security group is attached to the load balancer, not to an
    # instance. Add it to the same role map before validating the exact edge.
    protocol_dns = aws.bundle.outputs["ProtocolConnectDnsName"]
    protocol_load_balancers = [
        item
        for page in aws.client("elbv2").get_paginator("describe_load_balancers").paginate()
        for item in page.get("LoadBalancers", [])
        if item.get("DNSName") == protocol_dns
    ]
    if len(protocol_load_balancers) != 1 or len(protocol_load_balancers[0].get("SecurityGroups", [])) != 1:
        raise RuntimeError("protocol NLB security group inventory is not exact")
    instance_contract["roleSecurityGroups"]["protocolLoadBalancer"] = protocol_load_balancers[0]["SecurityGroups"][0]
    managed = aws.client("ssm").describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [item for values in asgs.values() for item in values]}],
        MaxResults=50,
    )["InstanceInformationList"]
    online = {item["InstanceId"] for item in managed if item.get("PingStatus") == "Online"}
    expected_online = {item for values in asgs.values() for item in values}
    if online != expected_online:
        raise RuntimeError("not every exact run-owned host is SSM Online")
    ecs_contract = verify_ecs_contract(aws, services, asgs, contract)
    protocol_path = verify_protocol_path(aws, instance_contract, contract, ca_certificate)
    stream = aws.client("kinesis").describe_stream_summary(StreamName=aws.bundle.outputs["StreamName"])["StreamDescriptionSummary"]
    if stream.get("StreamStatus") != "ACTIVE" or int(stream.get("OpenShardCount", 0)) != 120:
        raise RuntimeError("Phase 7 Kinesis stream is not ACTIVE with 120 shards")
    clickhouse = aws.clickhouse("SELECT 1 AS query_ok, countIf(database='loopad' AND name IN ('events','raw_events')) AS schema_tables FROM system.tables")
    if len(clickhouse) != 1 or int(clickhouse[0]["query_ok"]) != 1 or int(clickhouse[0]["schema_tables"]) != 2:
        raise RuntimeError("ClickHouse schema readiness failed")
    result = {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "verifiedAt": now(),
        "identity": identity,
        "stackStatus": stack["StackStatus"],
        "stackTags": tags,
        "stackOutputs": stack_outputs,
        "services": services,
        "instances": asgs,
        "instanceContract": instance_contract,
        "ecsContract": ecs_contract,
        "protocolPath": protocol_path,
        "ssmOnline": sorted(online),
        "stream": {"name": stream["StreamName"], "status": stream["StreamStatus"], "openShardCount": stream["OpenShardCount"]},
        "clickHouse": clickhouse[0],
        "passed": True,
    }
    write_json(aws.bundle.run_dir / "deployment-verification.json", result)
    return result


def correctness_and_replacement(aws: AwsRuntime) -> dict[str, Any]:
    aws.assert_identity()
    generator = aws.asg_instances("LoadGeneratorAutoScalingGroupName", 8)[0]
    prefix = f"phase7-{aws.bundle.run_id.removeprefix('run_').replace('_', '-')}-correctness-"
    bodies = balanced_event_documents(prefix, 1_000, datetime.now(UTC))
    pool = b"\n".join(json.dumps(body, separators=(",", ":")).encode() for body in bodies) + b"\n"
    encoded = base64.b64encode(gzip.compress(pool, compresslevel=9)).decode()
    if len(encoded) > 20 * 1024:
        raise RuntimeError("correctness payload exceeds the verified SSM transfer size")
    host = aws.bundle.outputs["ProtocolEndpoint"].removeprefix("https://")
    destination = aws.bundle.outputs["ProtocolConnectDnsName"]
    remote = correctness_curl_command(encoded, host, destination)
    raw_http_result = aws.run_ssm(generator, [remote], timeout=600)
    http_receipt = {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "generatedAt": now(),
        "generatorInstanceId": generator,
        "payloadDocumentCount": len(bodies),
        "payloadPoolSha256": hashlib.sha256(pool).hexdigest(),
        "remoteCommandSha256": hashlib.sha256(remote.encode()).hexdigest(),
        "standardOutputContent": raw_http_result,
    }
    write_json(aws.bundle.run_dir / "correctness-http-ssm-receipt.json", http_receipt)
    http_result = parse_correctness_http_result(raw_http_result)
    http_receipt["parsedOutput"] = http_result
    http_receipt["passed"] = http_result == {"http202": 1000, "non202": 0, "total": 1000}
    write_json(aws.bundle.run_dir / "correctness-http-ssm-receipt.json", http_receipt)
    if http_result != {"http202": 1000, "non202": 0, "total": 1000}:
        raise RuntimeError(f"HTTP correctness mismatch: {http_result}")

    invalid_key = f"{aws.bundle.run_id}-invalid-json"
    late = event_document(f"phase7-{aws.bundle.run_id}-late-", 0, datetime.now(UTC) - timedelta(days=8))
    late["run_id"] = f"{aws.bundle.run_id}-correctness-late"
    direct = aws.put_records([
        (b'{"run_id":"phase7-invalid",', invalid_key),
        (json.dumps(late, separators=(",", ":")).encode(), late["event_id"]),
    ])
    count_query = f"""
SELECT
  (SELECT count() FROM loopad.events FINAL WHERE startsWith(event_id, '{prefix}')) AS final,
  (SELECT uniqExact(event_id) FROM loopad.events FINAL WHERE startsWith(event_id, '{prefix}')) AS unique,
  (SELECT count() FROM loopad.events WHERE startsWith(event_id, '{prefix}')) AS physical,
  (SELECT count() FROM loopad.raw_events WHERE partition_key = '{invalid_key}') AS raw
""".strip()
    counts = wait_until(
        "correctness rows",
        600,
        10,
        lambda: one(aws.clickhouse(count_query)),
        lambda value: all(int(value[name]) == expected for name, expected in {"final": 1000, "unique": 1000, "physical": 1000, "raw": 1}.items()),
    )
    late_metric = wait_until(
        "LateEventDropped metric",
        600,
        15,
        lambda: aws.metric_sum("LoopAd/Phase7", "LateEventDropped", [{"Name": "RunId", "Value": aws.bundle.run_id}]),
        lambda value: value >= 1,
    )

    service_before = aws.wait_service("Consumer")
    baseline = {task["taskArn"] for task in service_before["tasks"]}
    leases_before = wait_until("balanced KCL leases", 600, 10, aws.lease_snapshot, leases_balanced)
    replacement_run_id = f"{aws.bundle.run_id}-replacement"
    replacement_records = [
        (json.dumps({**event_document(f"phase7-{aws.bundle.run_id}-replacement-", index, datetime.now(UTC)), "run_id": replacement_run_id}, separators=(",", ":")).encode(), f"phase7-{aws.bundle.run_id}-replacement-{index:06d}")
        for index in range(900)
    ]
    first = aws.put_records(replacement_records[:15])
    stopped = sorted(baseline)[0]
    aws.client("ecs").stop_task(
        cluster=aws.bundle.outputs["ConsumerClusterName"],
        task=stopped,
        reason=f"Phase 7 deliberate replacement for {aws.bundle.run_id}",
    )
    accepted = first["accepted"]
    started = time.monotonic()
    for offset in range(15, 900, 15):
        accepted += aws.put_records(replacement_records[offset : offset + 15])["accepted"]
        deadline = started + offset // 15
        if deadline > time.monotonic():
            time.sleep(deadline - time.monotonic())
    service_after = wait_until(
        "consumer replacement",
        900,
        10,
        lambda: aws.service_snapshot("Consumer"),
        lambda value: service_ready(value, 2) and {task["taskArn"] for task in value["tasks"]} != baseline,
    )
    replacement_query = f"SELECT count() AS final, uniqExact(event_id) AS unique, (SELECT count() FROM loopad.events WHERE run_id='{replacement_run_id}') AS physical FROM loopad.events FINAL WHERE run_id='{replacement_run_id}'"
    replacement_counts = wait_until(
        "replacement rows",
        900,
        10,
        lambda: one(aws.clickhouse(replacement_query)),
        lambda value: int(value["final"]) == 900 and int(value["unique"]) == 900 and int(value["physical"]) >= 900,
    )
    leases_after = wait_until(
        "post-replacement leases",
        600,
        10,
        aws.lease_snapshot,
        lambda value: leases_balanced(value) and value["sha256"] != leases_before["sha256"],
    )
    current = {task["taskArn"] for task in service_after["tasks"]}
    replacement_ok = len(baseline) == 2 and len(current) == 2 and len(baseline | current) == 3 and stopped not in current
    result = {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "generatedAt": now(),
        "correctness": {
            "http": http_result,
            "directKinesis": direct,
            "counts": counts,
            "lateEventDropped": late_metric,
            "inputRecords": 1002,
            "passed": int(counts["final"]) + int(counts["raw"]) + int(late_metric) == 1002,
        },
        "replacement": {
            "offered": 900,
            "accepted": accepted,
            "stoppedTask": stopped,
            "baselineTasks": sorted(baseline),
            "currentTasks": sorted(current),
            "counts": replacement_counts,
            "leasesBefore": leases_before,
            "leasesAfter": leases_after,
            "passed": accepted == 900 and replacement_ok,
        },
    }
    result["passed"] = result["correctness"]["passed"] and result["replacement"]["passed"]
    write_json(aws.bundle.run_dir / "correctness-summary.json", result)
    if not result["passed"]:
        raise RuntimeError("correctness or replacement gate failed")
    return result


def seed_partition(aws: AwsRuntime) -> dict[str, Any]:
    aws.assert_identity()
    today = datetime.now(UTC).date()
    partition = utc_source_partition(today).isoformat()
    contract = GeneratorContract(
        version=GENERATOR_VERSION,
        seed=DEFAULT_SEED,
        partition=partition,
        rows=FULL_SCALE_ROWS,
        # The archive task receives the main Run ID from CDK. The deterministic
        # post-DROP reference must use the exact same generator contract.
        run_id=aws.bundle.run_id,
    )
    started = time.monotonic()
    aws.clickhouse(seed_insert_sql(contract), select=False, timeout=900)
    query = f"SELECT count() AS rows, uniqExact(event_id) AS uniqueEvents, toString(sum(cityHash64(project_id,event_id,toString(event_time),properties_json))) AS checksum FROM loopad.events FINAL WHERE event_date=toDate('{partition}')"
    first = one(aws.clickhouse(query, timeout=900))
    if int(first["rows"]) != FULL_SCALE_ROWS or int(first["uniqueEvents"]) != FULL_SCALE_ROWS:
        raise RuntimeError("closed partition seed count mismatch")
    time.sleep(10)
    second = one(aws.clickhouse(query, timeout=900))
    if first != second:
        raise RuntimeError("closed partition fingerprint did not quiesce")
    result = {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "seededAt": now(),
        "partition": partition,
        "today": today.isoformat(),
        "rows": FULL_SCALE_ROWS,
        "generatorContract": {
            "version": contract.version,
            "seed": contract.seed,
            "partition": contract.partition,
            "rows": contract.rows,
            "runId": contract.run_id,
        },
        "fingerprintSamples": [first, second],
        "stable": True,
        "durationSeconds": round(time.monotonic() - started, 6),
    }
    write_json(aws.bundle.run_dir / "seed-summary.json", result)
    return result


def run_load_stage(aws: AwsRuntime, stage: str, ca_certificate: Path) -> dict[str, Any]:
    aws.assert_identity()
    if stage not in {"warmup", "score"}:
        raise ValueError("load stage must be warmup or score")
    evidence = aws.bundle.run_dir / "evidence" / stage
    payload_dir = aws.bundle.run_dir / "inputs" / f"{stage}-payload"
    payload_dir.mkdir(parents=True, exist_ok=False)
    payload = payload_dir / "payload.ndjson"
    manifest = payload_dir / "manifest.json"
    generator = ROOT / "performance-tests/phase7-integration/aws/diagnostic_payload_pool.mjs"
    subprocess.run([
        "node", str(generator), "--run-id", aws.bundle.run_id, "--stage", stage,
        "--event-date", datetime.now(UTC).date().isoformat(), "--output", str(payload),
        "--manifest", str(manifest),
    ], cwd=ROOT, check=True)
    command = [
        "node", str(ROOT / "performance-tests/phase7-integration/aws/run_diagnostic_oha.mjs"),
        "--run-id", aws.bundle.run_id,
        "--stage", stage,
        "--instance-ids", ",".join(aws.asg_instances("LoadGeneratorAutoScalingGroupName", int(TOPOLOGY["loadGeneratorHosts"]))),
        "--target-base-url", aws.bundle.outputs["ProtocolEndpoint"],
        "--target-connect-dns-name", aws.bundle.outputs["ProtocolConnectDnsName"],
        "--ca-certificate", str(ca_certificate.resolve()),
        "--evidence-bucket", aws.bundle.outputs["ArchiveBucketName"],
        "--payload-pool", str(payload),
        "--payload-manifest", str(manifest),
        "--output-dir", str(evidence),
        "--duration-seconds", "180" if stage == "warmup" else "300",
    ]
    hard_stop_config = payload_dir / "hard-stop-config.json"
    write_json(hard_stop_config, {
        "runId": aws.bundle.run_id,
        "consumerCluster": aws.bundle.outputs["ConsumerClusterName"],
        "consumerService": aws.bundle.outputs["ConsumerServiceName"],
        "clickHouseCluster": aws.bundle.outputs["ClickHouseClusterName"],
        "clickHouseService": aws.bundle.outputs["ClickHouseServiceName"],
        "failureBucket": aws.bundle.outputs["FailureBucketName"],
        "consumerLogGroup": f"/loopad/perf/phase7/{aws.bundle.run_id}/ConsumerLogs",
    })
    command.extend(["--hard-stop-config", str(hard_stop_config)])
    if stage == "score":
        seed = read_json(aws.bundle.run_dir / "seed-summary.json")
        archive_config = payload_dir / "archive-config.json"
        write_json(archive_config, {
            "cluster": aws.bundle.outputs["ArchiveClusterName"],
            "taskDefinition": aws.bundle.outputs["ArchiveTaskDefinitionArn"],
            "capacityProvider": aws.bundle.outputs["ArchiveCapacityProviderName"],
            "subnetIds": aws.bundle.outputs["ArchiveSubnetIds"].split(","),
            "securityGroupId": aws.bundle.outputs["ArchiveSecurityGroupId"],
            "partition": seed["partition"],
            "today": seed["today"],
        })
        command.extend(["--archive-config", str(archive_config)])
    capture_context = None
    capture_dir = aws.bundle.run_dir / "evidence" / "score-observability"
    if stage == "score":
        capture_context = start_score_capture(aws, capture_dir)
    try:
        completed = subprocess.run(command, cwd=ROOT, check=False)
    finally:
        if capture_context is not None:
            finish_score_capture(aws, capture_dir, capture_context)
    if completed.returncode != 0:
        raise RuntimeError(f"{stage} orchestration failed")
    summary = read_json(evidence / "stage-summary.json")
    identity_contract = read_json(aws.bundle.run_dir / "inputs" / "identity-contract.json")
    if (
        identity_contract.get("runId") != aws.bundle.run_id
        or identity_contract.get("sessionId") != aws.bundle.session_id
        or summary.get("identityMode") != identity_contract.get("identityMode")
    ):
        raise RuntimeError("load summary does not match the predeclared identity contract")
    summary["sessionId"] = aws.bundle.session_id
    summary["identityContract"] = {
        key: identity_contract[key]
        for key in (
            "predeclaredBeforeDeploy", "userApproved", "selectionWithReplacement",
            "warmupScorePoolsSeparated", "balancedShardCount", "fixturePoolRows",
        )
    }
    write_json(evidence / "stage-summary.json", summary)
    if not summary.get("diagnosticContinuationAllowed"):
        raise RuntimeError(f"{stage} hit a hard stop and cannot continue to evidence collection")
    if stage == "warmup":
        aggregate = summary["aggregate"]
        stage_start = int(aggregate["startEpoch"])
        stage_end = aggregate_end_epoch(aggregate)
        accounting_end = int((stage_end // 60 + 2) * 60)
        expected = int(aggregate["http202"])
        kinesis = wait_until(
            "exact warmup Kinesis accounting",
            600,
            15,
            lambda: aws.kinesis_incoming_records(stage_start, accounting_end),
            lambda value: exact_or_raise(value, expected, "warmup Kinesis IncomingRecords"),
        )
        consumer = wait_until(
            "exact warmup consumer accounting",
            600,
            15,
            lambda: aws.successful_consumer_input(stage_start, int(datetime.now(UTC).timestamp()) + 60),
            lambda value: exact_or_raise(value["processed"], expected, "warmup consumer processed"),
        )
        age_samples = wait_until(
            "fresh warmup Kinesis drain samples",
            900,
            15,
            lambda: aws.iterator_age_samples(stage_end),
            wall_clock_iterator_acceptor(time.time()),
        )
        summary["accounting"] = {
            "http202": expected,
            "kinesisAccepted": kinesis,
            "kclProcessed": consumer["processed"],
            "clickHouseInserted": consumer["processed"],
        }
        summary["drain"] = {
            "basis": "fresh post-warmup CloudWatch datapoints",
            "samples": age_samples,
            "progressed": iterator_age_progressed(age_samples),
        }
        write_json(evidence / "stage-summary.json", summary)
    return summary


def drain_validate(aws: AwsRuntime) -> dict[str, Any]:
    aws.assert_identity()
    started = time.monotonic()
    score = read_json(aws.bundle.run_dir / "evidence" / "score" / "stage-summary.json")
    aggregate = score["aggregate"]
    score_start = int(aggregate["startEpoch"])
    observed_start = aggregate_start_epoch(aggregate)
    observed_end = aggregate_end_epoch(aggregate)
    run_state = read_json(aws.bundle.run_dir / "run.json")
    try:
        cleanup_start_epoch = int(
            parse_utc(str(run_state["cleanupStartDeadline"])).timestamp()
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("cleanup-start deadline is missing from run state") from error
    drain_deadline_epoch = effective_drain_deadline_epoch(
        observed_end, cleanup_start_epoch
    )
    bounded_timeout = lambda ceiling: min(ceiling, remaining_drain_seconds(drain_deadline_epoch))
    iterator_observation_started = time.time()
    age_samples = wait_until(
        "fresh score Kinesis drain samples",
        bounded_timeout(2700),
        15,
        lambda: aws.iterator_age_samples(observed_end),
        wall_clock_iterator_acceptor(iterator_observation_started),
    )
    for role in EXPECTED_COUNTS:
        aws.wait_service(role, timeout=bounded_timeout(300))
    accounting_end = int((observed_end // 60 + 2) * 60)
    prefix = read_json(aws.bundle.run_dir / "inputs" / "score-payload" / "manifest.json")["eventIdPrefix"]
    http202 = int(aggregate["http202"])
    kinesis_accepted = wait_until(
        "exact score Kinesis IncomingRecords metric",
        bounded_timeout(600),
        15,
        lambda: aws.kinesis_incoming_records(score_start, accounting_end),
        lambda value: exact_or_raise(value, http202, "score Kinesis IncomingRecords"),
    )
    consumer_accounting = wait_until(
        "exact score consumer success-log accounting",
        bounded_timeout(600),
        15,
        lambda: aws.successful_consumer_input(score_start, int(datetime.now(UTC).timestamp()) + 60),
        lambda value: exact_or_raise(value["processed"], http202, "score consumer processed"),
    )
    visibility = wait_until(
        "score visibility histogram accounting",
        bounded_timeout(600),
        15,
        lambda: aws.visibility_histogram(score_start, int(datetime.now(UTC).timestamp()) + 60),
        lambda value: exact_or_raise(value["observedRecords"], http202, "score visibility records"),
    )
    visibility_percentiles = histogram_percentiles(visibility)
    rows = one(aws.clickhouse(
        f"SELECT count() AS physical, (SELECT count() FROM loopad.events FINAL WHERE startsWith(event_id,'{prefix}')) AS final, uniqExact(event_id) AS uniqueEvents FROM loopad.events WHERE startsWith(event_id,'{prefix}')",
        timeout=bounded_timeout(900),
    ))
    accounting = {
        "http202": http202,
        "collectorFinalAck": http202,
        "kinesisAccepted": kinesis_accepted,
        "kclProcessed": consumer_accounting["processed"],
        "clickHouseInserted": consumer_accounting["processed"],
        "clickHouseLiveUnique": int(rows["uniqueEvents"]),
        "fixturePoolRows": 480,
        "physicalRowsRemainingAfterMerges": int(rows["physical"]),
        "derivation": {
            "collectorFinalAck": "HTTP 202 is emitted only after the pinned collector's final Kinesis ACK",
            "kinesisAccepted": "AWS/Kinesis IncomingRecords sum over the minute-aligned score window",
            "kclProcessed": "CloudWatch Logs Insights sum(inputRecords) for phase4_batch_success after score start",
            "clickHouseInserted": "phase4_batch_success is logged only after the ClickHouse insert call returns",
        },
        "consumerBatches": consumer_accounting["batches"],
        "consumerMaximumInsertAttempts": consumer_accounting["maxAttempts"],
    }
    terminal = aws.metric_sum("LoopAd/Phase7", "TerminalFailure", [{"Name": "RunId", "Value": aws.bundle.run_id}], minutes=60)
    checkpoint = aws.metric_sum("LoopAd/Phase7", "CheckpointError", [{"Name": "RunId", "Value": aws.bundle.run_id}], minutes=60)
    archive_result = archive_evidence(aws, clickhouse_timeout=bounded_timeout(600))
    archive_result["liveRowsAfterDrop"] = int(rows["uniqueEvents"])
    archive_task = score.get("archive") or {}
    archive_result["overlappedScoreWindow"] = (
        int(archive_task.get("startedEpoch", -1)) >= observed_start
        and int(archive_task.get("startedEpoch", -1)) < observed_end
    )
    try:
        archive_result["cycleSeconds"] = (
            datetime.fromisoformat(str(archive_task["stoppedAt"]).replace("Z", "+00:00"))
            - datetime.fromisoformat(str(archive_task["startedAt"]).replace("Z", "+00:00"))
        ).total_seconds()
    except (KeyError, TypeError, ValueError):
        archive_result["cycleSeconds"] = 10**9
    archive_result["schemaVersion"] = 1
    archive_result["runId"] = aws.bundle.run_id
    archive_result["sessionId"] = aws.bundle.session_id
    score_end_to_validated_seconds = max(0.0, datetime.now(UTC).timestamp() - observed_end)
    if score_end_to_validated_seconds > 2700:
        raise RuntimeError("score drain/accounting exceeded the absolute 45-minute post-score deadline")
    result = {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "generatedAt": now(),
        "drain": {
            "seconds": round(score_end_to_validated_seconds, 6),
            "validatorRuntimeSeconds": round(time.monotonic() - started, 6),
            "basis": "actual aggregate score end epoch through completed archive/drain/accounting validation",
            "iteratorAgeLatestMs": age_samples[-1]["maximumMs"],
            "iteratorAgeProgressed": iterator_age_progressed(age_samples),
            "iteratorAgeSamples": age_samples,
            "visibilityP50Ms": visibility_percentiles["p50Ms"],
            "visibilityP95Ms": visibility_percentiles["p95Ms"],
            "visibilityP99Ms": visibility_percentiles["p99Ms"],
            "visibilityBasis": "Kinesis approximate arrival to successful ClickHouse insert completion",
            "visibilityHistogram": visibility,
            "visibilityCaveat": "fixed-bucket upper bounds exclude collector-to-Kinesis acceptance latency",
        },
        "counts": accounting,
        "failures": {"terminalFailure": terminal, "checkpointError": checkpoint},
        "archive": archive_result,
    }
    write_json(aws.bundle.run_dir / "drain-accounting.json", result)
    write_json(aws.bundle.run_dir / "archive-validation.json", archive_result)
    return result


def archive_evidence(
    aws: AwsRuntime,
    *,
    clickhouse_timeout: int = 600,
    retain_source_after_commit: bool = False,
) -> dict[str, Any]:
    if type(retain_source_after_commit) is not bool:
        raise ValueError("retain_source_after_commit must be a boolean")
    bucket = aws.bundle.outputs["ArchiveBucketName"]
    s3 = aws.client("s3")
    commits = s3.list_objects_v2(Bucket=bucket, Prefix="commits/v1/table=events/").get("Contents", [])
    commit_keys = [item["Key"] for item in commits if item["Key"].endswith("/COMMITTED")]
    if len(commit_keys) != 1:
        raise RuntimeError("expected exactly one immutable archive COMMITTED object")
    commit_bytes = s3.get_object(Bucket=bucket, Key=commit_keys[0])["Body"].read()
    commit = json.loads(commit_bytes)
    manifest_bytes = s3.get_object(Bucket=bucket, Key=commit["manifestKey"])["Body"].read()
    if hashlib.sha256(manifest_bytes).hexdigest() != commit["manifestSha256"]:
        raise RuntimeError("committed archive manifest hash mismatch")
    manifest = json.loads(manifest_bytes)
    parts = manifest.get("parts", [])
    if not isinstance(parts, list) or len(parts) != 3:
        raise RuntimeError("committed archive manifest must contain exactly three parts")
    result_key = (
        f"attempts/v1/table=events/event_date={manifest['partition']}/"
        f"phase7-result-{aws.bundle.run_id}.json"
    )
    result_bytes = s3.get_object(Bucket=bucket, Key=result_key)["Body"].read()
    worker = json.loads(result_bytes)
    if (
        worker.get("status") != "passed"
        or worker.get("runId") != aws.bundle.run_id
        or worker.get("partition") != manifest.get("partition")
        or worker.get("archiveId") != manifest.get("archiveId")
        or worker.get("manifestKey") != commit.get("manifestKey")
        or worker.get("manifestSha256") != commit.get("manifestSha256")
    ):
        raise RuntimeError("archive worker result does not match the immutable commit")
    if retain_source_after_commit:
        if (
            worker.get("diagnosticSourceRetention") is not True
            or worker.get("dropExecuted") is not False
            or worker.get("postDrop") is not None
        ):
            raise RuntimeError("retain-source archive worker result is not fail-closed")
    elif worker.get("diagnosticSourceRetention") is True or worker.get("dropExecuted") is False:
        raise RuntimeError("strict archive worker unexpectedly retained the source partition")
    worker_parts = worker.get("parts")
    if not isinstance(worker_parts, list) or [part_identity(item) for item in worker_parts] != [part_identity(item) for item in parts]:
        raise RuntimeError("archive worker result parts do not match the committed manifest")
    object_heads = []
    for part in parts:
        head = s3.head_object(Bucket=bucket, Key=str(part["key"]))
        metadata = head.get("Metadata", {})
        if (
            metadata.get("sha256") != part.get("sha256")
            or int(head.get("ContentLength", -1)) != int(part.get("bytes", -2))
        ):
            raise RuntimeError(f"archive part HEAD evidence mismatch: {part.get('key')}")
        object_heads.append({
            "key": part["key"],
            "contentLength": int(head["ContentLength"]),
            "sha256": metadata["sha256"],
        })
    pre = exact_difference_totals(worker.get("preDrop"), "pre-DROP")
    committed_pre = exact_difference_totals(worker.get("committedPreDrop"), "committed-pre-DROP")
    post = (
        None
        if retain_source_after_commit
        else exact_difference_totals(worker.get("postDrop"), "post-DROP")
    )
    source_rows = one(aws.clickhouse(
        f"SELECT count() AS rows FROM loopad.events WHERE event_date=toDate('{manifest['partition']}')",
        timeout=clickhouse_timeout,
    ))["rows"]
    if retain_source_after_commit and int(source_rows) != FULL_SCALE_ROWS:
        raise RuntimeError("retain-source archive did not preserve the exact 15M source rows")
    if not retain_source_after_commit and int(source_rows) != 0:
        raise RuntimeError("strict archive source partition was not dropped")
    commit_reread = s3.get_object(Bucket=bucket, Key=commit_keys[0])["Body"].read()
    manifest_reread = s3.get_object(Bucket=bucket, Key=commit["manifestKey"])["Body"].read()
    result_reread = s3.get_object(Bucket=bucket, Key=result_key)["Body"].read()
    if (
        commit_reread != commit_bytes
        or manifest_reread != manifest_bytes
        or result_reread != result_bytes
    ):
        raise RuntimeError("archive commit, manifest or result changed across exact re-read")
    result = {
        "rows": int(manifest.get("archive", {}).get("rows", -1)),
        "objects": len(parts),
        "objectRows": [int(part.get("rows", -1)) for part in parts],
        "partKeys": [part.get("key") for part in parts],
        "partSha256": [part.get("sha256") for part in parts],
        "commitKey": commit_keys[0],
        "commitReRead": True,
        "manifestSha256": commit["manifestSha256"],
        "workerResultKey": result_key,
        "workerResultSha256": hashlib.sha256(result_bytes).hexdigest(),
        "objectHeads": object_heads,
        "sourceRowsAfterDrop": int(source_rows) if not retain_source_after_commit else None,
        "sourceRowsAfterArchive": int(source_rows),
        "preDropSourceMinusArchive": pre["leftMinusRight"],
        "preDropArchiveMinusSource": pre["rightMinusLeft"],
        "committedSourceMinusArchive": committed_pre["leftMinusRight"],
        "committedArchiveMinusSource": committed_pre["rightMinusLeft"],
        "postDropReferenceMinusArchive": post["leftMinusRight"] if post else None,
        "postDropArchiveMinusReference": post["rightMinusLeft"] if post else None,
        "committedReRead": True,
        "diagnosticSourceRetention": retain_source_after_commit,
        "dropExecuted": not retain_source_after_commit,
        "manifest": manifest,
        "workerResult": worker,
    }
    return result


def part_identity(value: Any) -> tuple[int, str, int, int, str]:
    if not isinstance(value, dict):
        raise RuntimeError("archive part evidence must be an object")
    return (
        int(value.get("index", -1)),
        str(value.get("key", "")),
        int(value.get("rows", -1)),
        int(value.get("bytes", -1)),
        str(value.get("sha256", "")),
    )


def exact_difference_totals(value: Any, expected_stage: str) -> dict[str, int]:
    if not isinstance(value, dict) or value.get("passed") is not True:
        raise RuntimeError(f"archive worker did not pass {expected_stage} equivalence")
    stage = str(value.get("stage", ""))
    if stage != expected_stage:
        raise RuntimeError(f"archive worker {expected_stage} stage label mismatch: {stage}")
    differences = value.get("twoWayDifferences")
    if not isinstance(differences, list) or len(differences) != 3:
        raise RuntimeError(f"archive worker {expected_stage} must report three part differences")
    if sorted(int(item.get("part", -1)) for item in differences if isinstance(item, dict)) != [0, 1, 2]:
        raise RuntimeError(f"archive worker {expected_stage} part indexes are invalid")
    totals = {
        "leftMinusRight": sum(int(item.get("leftMinusRight", -1)) for item in differences),
        "rightMinusLeft": sum(int(item.get("rightMinusLeft", -1)) for item in differences),
    }
    if totals != {"leftMinusRight": 0, "rightMinusLeft": 0}:
        raise RuntimeError(f"archive worker {expected_stage} exact differences are nonzero")
    return totals


def event_document(prefix: str, sequence: int, event_time: datetime) -> dict[str, Any]:
    stamp = event_time.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "project_id": "phase7-correctness",
        "write_key": "perf_public_write_key",
        "schema_version": "hotel_rec_promo.v1",
        "event_id": f"{prefix}{sequence:06d}",
        "event_name": "phase7_correctness",
        "event_time": stamp,
        "source": "browser_sdk",
        "user_id": f"user-{sequence % 1000}",
        "session_id": f"session-{sequence % 100}",
        "properties_json": json.dumps({"sequence": sequence}, separators=(",", ":")),
    }


def balanced_event_documents(prefix: str, count: int, event_time: datetime) -> list[dict[str, Any]]:
    if count < 120:
        raise ValueError("balanced correctness input requires at least one record per shard")
    targets = [count // 120 + (1 if index < count % 120 else 0) for index in range(120)]
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(120)]
    candidate = 0
    accepted = 0
    hash_space = 1 << 128
    while accepted < count:
        candidate += 1
        event_id = f"{prefix}{candidate:06d}"
        shard = int.from_bytes(hashlib.md5(event_id.encode(), usedforsecurity=False).digest(), "big") * 120 // hash_space
        if len(buckets[shard]) >= targets[shard]:
            continue
        document = event_document(prefix, candidate, event_time)
        buckets[shard].append(document)
        accepted += 1
    return [document for slot in range(max(targets)) for bucket in buckets for document in bucket[slot : slot + 1]]


def correctness_curl_command(encoded_pool: str, host: str, destination: str) -> str:
    if not re.fullmatch(r"[a-z0-9.-]+", host) or not re.fullmatch(r"[a-z0-9.-]+", destination):
        raise ValueError("invalid correctness TLS destination")
    return f"""set -euo pipefail
root=/tmp/phase7-correctness
rm -rf "$root"; mkdir -p "$root"
printf '%s' '{encoded_pool}' | base64 -d | gzip -d > "$root/payloads.ndjson"
: > "$root/status.txt"
send_one() {{ curl --silent --show-error --http2 --connect-timeout 5 --max-time 15 --connect-to '{host}:443:{destination}:443' -o /dev/null -w '%{{http_code}}\\n' -H 'Content-Type: application/json' --data-binary "$1" 'https://{host}/events'; }}
export -f send_one
while IFS= read -r body; do printf '%s\\0' "$body"; done < "$root/payloads.ndjson" | xargs -0 -n1 -P32 bash -c 'send_one "$1"' _ >> "$root/status.txt"
jq -cn --argjson http202 "$(grep -c '^202$' "$root/status.txt")" --argjson total "$(wc -l < "$root/status.txt")" '{{http202:$http202,non202:($total-$http202),total:$total}}'
"""


def parse_correctness_http_result(output: str) -> dict[str, int]:
    try:
        value = json.loads(output.strip())
    except json.JSONDecodeError as error:
        raise RuntimeError("correctness SSM stdout is not one JSON document") from error
    if not isinstance(value, dict) or set(value) != {"http202", "non202", "total"}:
        raise RuntimeError("correctness SSM result has an unexpected shape")
    if any(isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0 for key in value):
        raise RuntimeError("correctness SSM counts must be nonnegative integers")
    if value["http202"] + value["non202"] != value["total"]:
        raise RuntimeError("correctness SSM counts do not add up")
    return value


def leases_balanced(value: dict[str, Any]) -> bool:
    return (
        value.get("count") == 120
        and value.get("numericCheckpointCount") == 120
        and sorted(value.get("ownerCounts", {}).values()) == [60, 60]
    )


def service_ready(value: dict[str, Any], expected: int) -> bool:
    return (
        value.get("desiredCount") == expected
        and value.get("runningCount") == expected
        and value.get("pendingCount") == 0
        and len(value.get("tasks", [])) == expected
        and all(task.get("lastStatus") == "RUNNING" for task in value.get("tasks", []))
        and all(task.get("healthStatus") in {"HEALTHY", "UNKNOWN"} for task in value.get("tasks", []))
    )


def aggregate_end_epoch(aggregate: dict[str, Any]) -> int:
    nodes = aggregate.get("nodes", [])
    if not isinstance(nodes, list) or not nodes:
        raise RuntimeError("load aggregate has no node completion timestamps")
    try:
        return int(max(
            datetime.fromisoformat(str(node["endedAt"]).replace("Z", "+00:00")).timestamp()
            for node in nodes
        ))
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("load aggregate node completion timestamps are invalid") from error


def aggregate_start_epoch(aggregate: dict[str, Any]) -> int:
    nodes = aggregate.get("nodes", [])
    if not isinstance(nodes, list) or not nodes:
        raise RuntimeError("load aggregate has no node start timestamps")
    try:
        return int(min(
            datetime.fromisoformat(str(node["startedAt"]).replace("Z", "+00:00")).timestamp()
            for node in nodes
        ))
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("load aggregate node start timestamps are invalid") from error


def effective_drain_deadline_epoch(
    score_end_epoch: int, cleanup_start_epoch: int
) -> int:
    return min(int(score_end_epoch) + 2700, int(cleanup_start_epoch))


def remaining_drain_seconds(deadline_epoch: int, now_epoch: float | None = None) -> int:
    remaining = float(deadline_epoch) - (time.time() if now_epoch is None else float(now_epoch))
    seconds = int(remaining)
    if seconds < 1:
        raise RuntimeError("score drain/accounting cannot finish inside the absolute 45-minute deadline")
    return seconds


def exact_or_raise(actual: int, expected: int, description: str) -> bool:
    if actual > expected:
        raise RuntimeError(f"{description} exceeded the immutable HTTP 202 count")
    return actual == expected


def iterator_age_progressed(samples: list[dict[str, Any]]) -> bool:
    if len(samples) < 2:
        return False
    values = [float(sample["maximumMs"]) for sample in samples]
    return values[-1] <= 1_000 and (max(values[:-1]) > values[-1] or all(value <= 1_000 for value in values))


def wall_clock_iterator_acceptor(
    observation_started_epoch: float,
) -> Callable[[list[dict[str, Any]]], bool]:
    return lambda samples: iterator_drain_complete(
        samples,
        now_epoch=time.time(),
        observation_started_epoch=observation_started_epoch,
    )


def iterator_drain_complete(
    samples: list[dict[str, Any]],
    *,
    now_epoch: float | None = None,
    observation_started_epoch: float | None = None,
) -> bool:
    try:
        ordered = sorted(
            ((int(sample["epoch"]), float(sample["maximumMs"])) for sample in samples),
            key=lambda item: item[0],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("iterator-age evidence is malformed") from error
    if len({epoch for epoch, _ in ordered}) != len(ordered):
        raise RuntimeError("iterator-age evidence has duplicate timestamps")
    fresh_for_acceptance = (
        observation_started_epoch is None
        or (
            bool(ordered)
            and ordered[-1][0] >= float(observation_started_epoch)
        )
    )
    if fresh_for_acceptance and iterator_age_progressed(samples):
        return True
    last_progress_epoch = (
        float(observation_started_epoch)
        if observation_started_epoch is not None
        else (float(ordered[0][0]) if ordered else None)
    )
    for (previous_epoch, previous), (current_epoch, current) in zip(ordered, ordered[1:]):
        if current_epoch <= previous_epoch:
            raise RuntimeError("iterator-age evidence timestamps are not increasing")
        if current < previous:
            last_progress_epoch = max(
                float(current_epoch),
                float(observation_started_epoch or current_epoch),
            )
    comparison_epoch = (
        float(now_epoch)
        if now_epoch is not None
        else (float(ordered[-1][0]) if ordered else None)
    )
    if (
        comparison_epoch is not None
        and last_progress_epoch is not None
        and comparison_epoch - last_progress_epoch >= 600
    ):
        raise RuntimeError("Kinesis iterator age did not decrease for 10 consecutive minutes")
    return False


def histogram_percentiles(histogram: dict[str, int]) -> dict[str, int]:
    observed = int(histogram.get("observedRecords", -1))
    if observed <= 0:
        raise RuntimeError("visibility histogram contains no observed records")
    buckets = [
        (bound, int(histogram.get(f"latencyLe{bound}Ms", -1)))
        for bound in VISIBILITY_BUCKETS_MS
    ]
    buckets.append((60_001, int(histogram.get("latencyGt60000Ms", -1))))
    if any(count < 0 for _, count in buckets) or sum(count for _, count in buckets) != observed:
        raise RuntimeError("visibility histogram buckets do not equal observed records")

    def percentile(quantile: float) -> int:
        threshold = max(1, int(observed * quantile + 0.999999999))
        cumulative = 0
        for upper_bound, count in buckets:
            cumulative += count
            if cumulative >= threshold:
                return upper_bound
        raise RuntimeError("visibility histogram percentile is not reachable")

    return {"p50Ms": percentile(0.50), "p95Ms": percentile(0.95), "p99Ms": percentile(0.99)}


def wait_until(description: str, timeout: int, interval: int, probe: Callable[[], Any], accept: Callable[[Any], bool]) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        last = probe()
        if accept(last):
            return last
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {description}; last={last!r}")
        time.sleep(interval)


def one(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) != 1:
        raise RuntimeError(f"expected one ClickHouse row, got {len(rows)}")
    return rows[0]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso(value: Any) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["verify", "correctness", "seed", "warmup", "score_archive", "drain_validate", "collect"])
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--ca-certificate", type=Path)
    args = parser.parse_args()
    if args.action in {"verify", "warmup", "score_archive"} and not args.ca_certificate:
        parser.error("verification and load stages require --ca-certificate")
    return args


def main() -> int:
    args = parse_args()
    bundle = load_bundle(args.run_dir.resolve())
    aws = AwsRuntime(bundle)
    if args.action == "verify":
        result = verify_deployment(aws, args.ca_certificate)
    elif args.action == "correctness":
        result = correctness_and_replacement(aws)
    elif args.action == "seed":
        result = seed_partition(aws)
    elif args.action == "warmup":
        result = run_load_stage(aws, "warmup", args.ca_certificate)
    elif args.action == "score_archive":
        result = run_load_stage(aws, "score", args.ca_certificate)
    elif args.action == "collect":
        result = collect_observability(aws)
    else:
        result = drain_validate(aws)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

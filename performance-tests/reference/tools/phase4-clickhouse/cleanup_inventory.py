#!/usr/bin/env python3
"""Read-only service inventory used before and after Phase 4 cleanup."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


REGION = "ap-northeast-2"
STACK_NAME = "LoopAdPerfPhase4ClickHouseStack"
SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "max_attempts": 5},
    user_agent_extra="loopad-phase4-cleanup-inventory/1",
)


class Inventory:
    def __init__(self, run_id: str, session_id: str, expected_account: str) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.expected_account = expected_account
        self.session = boto3.Session(region_name=REGION)

    def client(self, service: str) -> Any:
        return self.session.client(service, region_name=REGION, config=SDK_CONFIG)

    def collect(self) -> dict[str, Any]:
        identity = self.client("sts").get_caller_identity()
        if identity["Account"] != self.expected_account:
            raise RuntimeError(
                f"account mismatch: expected {self.expected_account}, got {identity['Account']}"
            )

        resources = {
            "cloudFormationStacks": self._cloudformation(),
            "taggingApiResources": self._tagging_api(),
            "ec2Instances": self._ec2("describe_instances"),
            "vpcs": self._ec2("describe_vpcs"),
            "subnets": self._ec2("describe_subnets"),
            "securityGroups": self._ec2("describe_security_groups"),
            "networkInterfaces": self._ec2("describe_network_interfaces"),
            "vpcEndpoints": self._ec2("describe_vpc_endpoints"),
            "elasticIps": self._ec2("describe_addresses"),
            "kinesisStreams": self._kinesis(),
            "lambdaFunctions": self._lambda(),
            "eventSourceMappings": self._event_source_mappings(),
            "s3Buckets": self._s3(),
            "logGroups": self._logs(),
            "secrets": self._secrets(),
            "cloudWatchAlarms": self._alarms(),
        }
        counts = {name: len(items) for name, items in resources.items()}
        return {
            "schemaVersion": 1,
            "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "readOnly": True,
            "account": identity["Account"],
            "region": REGION,
            "identity": {"arn": identity["Arn"]},
            "ownership": {"runId": self.run_id, "sessionId": self.session_id},
            "counts": counts,
            "resources": resources,
            "allZero": all(count == 0 for count in counts.values()),
        }

    def _cloudformation(self) -> list[dict[str, str]]:
        try:
            stacks = self.client("cloudformation").describe_stacks(StackName=STACK_NAME)["Stacks"]
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ValidationError":
                return []
            raise
        result = []
        for stack in stacks:
            tags = {item["Key"]: item["Value"] for item in stack.get("Tags", [])}
            if self._owned(tags):
                result.append({"stackName": stack["StackName"], "status": stack["StackStatus"]})
        return result

    def _tagging_api(self) -> list[str]:
        paginator = self.client("resourcegroupstaggingapi").get_paginator("get_resources")
        arns: list[str] = []
        for page in paginator.paginate(TagFilters=self._tag_filters()):
            arns.extend(item["ResourceARN"] for item in page["ResourceTagMappingList"])
        return sorted(set(arns))

    def _ec2(self, operation: str) -> list[str]:
        client = self.client("ec2")
        method = getattr(client, operation)
        response = method(Filters=[
            {"Name": "tag:RunId", "Values": [self.run_id]},
            {"Name": "tag:SessionId", "Values": [self.session_id]},
        ])
        response_key = {
            "describe_instances": "Reservations",
            "describe_vpcs": "Vpcs",
            "describe_subnets": "Subnets",
            "describe_security_groups": "SecurityGroups",
            "describe_network_interfaces": "NetworkInterfaces",
            "describe_vpc_endpoints": "VpcEndpoints",
            "describe_addresses": "Addresses",
        }[operation]
        if operation == "describe_instances":
            return sorted(
                instance["InstanceId"]
                for reservation in response[response_key]
                for instance in reservation["Instances"]
                if instance["State"]["Name"] != "terminated"
            )
        id_key = {
            "describe_vpcs": "VpcId",
            "describe_subnets": "SubnetId",
            "describe_security_groups": "GroupId",
            "describe_network_interfaces": "NetworkInterfaceId",
            "describe_vpc_endpoints": "VpcEndpointId",
            "describe_addresses": "AllocationId",
        }[operation]
        return sorted(item.get(id_key, item.get("PublicIp", "unknown")) for item in response[response_key])

    def _kinesis(self) -> list[str]:
        client = self.client("kinesis")
        names: list[str] = []
        paginator = client.get_paginator("list_streams")
        for page in paginator.paginate():
            for name in page["StreamNames"]:
                tags = {
                    item["Key"]: item["Value"]
                    for item in client.list_tags_for_stream(StreamName=name)["Tags"]
                }
                if self._owned(tags):
                    names.append(name)
        return sorted(names)

    def _lambda(self) -> list[str]:
        client = self.client("lambda")
        names: list[str] = []
        for page in client.get_paginator("list_functions").paginate():
            for function in page["Functions"]:
                tags = client.list_tags(Resource=function["FunctionArn"])["Tags"]
                if self._owned(tags):
                    names.append(function["FunctionName"])
        return sorted(names)

    def _event_source_mappings(self) -> list[str]:
        client = self.client("lambda")
        uuids: list[str] = []
        for function_name in self._lambda():
            for page in client.get_paginator("list_event_source_mappings").paginate(
                FunctionName=function_name,
            ):
                uuids.extend(item["UUID"] for item in page["EventSourceMappings"])
        return sorted(uuids)

    def _s3(self) -> list[str]:
        client = self.client("s3")
        names: list[str] = []
        for bucket in client.list_buckets()["Buckets"]:
            name = bucket["Name"]
            try:
                tags = {
                    item["Key"]: item["Value"]
                    for item in client.get_bucket_tagging(Bucket=name)["TagSet"]
                }
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {
                    "NoSuchTagSet", "NoSuchBucket", "AccessDenied",
                }:
                    continue
                raise
            if self._owned(tags):
                names.append(name)
        return sorted(names)

    def _logs(self) -> list[str]:
        client = self.client("logs")
        names: list[str] = []
        for page in client.get_paginator("describe_log_groups").paginate():
            for group in page["logGroups"]:
                arn = group.get("logGroupArn") or group.get("arn", "").removesuffix(":*")
                if not arn:
                    continue
                tags = client.list_tags_for_resource(resourceArn=arn)["tags"]
                if self._owned(tags):
                    names.append(group["logGroupName"])
        return sorted(names)

    def _secrets(self) -> list[str]:
        client = self.client("secretsmanager")
        names: list[str] = []
        for page in client.get_paginator("list_secrets").paginate(
            Filters=[
                {"Key": "tag-key", "Values": ["RunId"]},
                {"Key": "tag-value", "Values": [self.run_id]},
            ],
            IncludePlannedDeletion=True,
        ):
            for secret in page["SecretList"]:
                tags = {item["Key"]: item["Value"] for item in secret.get("Tags", [])}
                if self._owned(tags):
                    names.append(secret["Name"])
        return sorted(names)

    def _alarms(self) -> list[str]:
        client = self.client("cloudwatch")
        names: list[str] = []
        for page in client.get_paginator("describe_alarms").paginate():
            for alarm in [*page.get("MetricAlarms", []), *page.get("CompositeAlarms", [])]:
                tags = client.list_tags_for_resource(ResourceARN=alarm["AlarmArn"])["Tags"]
                tag_map = {item["Key"]: item["Value"] for item in tags}
                if self._owned(tag_map):
                    names.append(alarm["AlarmName"])
        return sorted(names)

    def _tag_filters(self) -> list[dict[str, Any]]:
        return [
            {"Key": "RunId", "Values": [self.run_id]},
            {"Key": "SessionId", "Values": [self.session_id]},
        ]

    def _owned(self, tags: dict[str, str]) -> bool:
        return tags.get("RunId") == self.run_id and tags.get("SessionId") == self.session_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def write_private(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")


def main() -> int:
    args = parse_args()
    result = Inventory(args.run_id, args.session_id, args.expected_account).collect()
    write_private(args.output, result)
    print(json.dumps({"counts": result["counts"], "allZero": result["allZero"]}, indent=2))
    return 0 if result["allZero"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

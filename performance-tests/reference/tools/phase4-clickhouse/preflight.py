#!/usr/bin/env python3
"""Read-only AWS and ownership preflight for the Phase 4 experiment."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


EXPECTED_REGION = "ap-northeast-2"
EXPECTED_AZ = "ap-northeast-2a"
STACK_NAME = "LoopAdPerfPhase4ClickHouseStack"
PHASE_TAG = "phase4-clickhouse-lambda"
REQUIRED_KINESIS_SHARDS = 120
REQUIRED_STANDARD_VCPUS = 16
REQUIRED_LAMBDA_RESERVED_CONCURRENCY = 120
MINIMUM_UNRESERVED_LAMBDA_CONCURRENCY = 100
PLANNED_NETWORK_INTERFACES = 16
PRICE_MAX_AGE_SECONDS = 3_600
NEW_LOAD_STOP_USD = 12.0
HARD_CAP_USD = 15.0
BOOTSTRAP_VERSION_PARAMETER = "/cdk-bootstrap/hnb659fds/version"
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"

SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "max_attempts": 5},
    user_agent_extra="loopad-phase4-preflight/1",
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    observed: Any
    required: Any
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pass": self.passed,
            "observed": self.observed,
            "required": self.required,
            "detail": self.detail,
        }


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def price_age_seconds(price_document: dict[str, Any], now: datetime) -> float:
    as_of = price_document.get("asOf")
    if not isinstance(as_of, str):
        raise ValueError("price document has no asOf timestamp")
    return max(0.0, (now.astimezone(UTC) - parse_utc(as_of)).total_seconds())


def operator_check(identity_arn: str, allow_root: bool) -> Check:
    is_root = identity_arn.endswith(":root")
    return Check(
        "operator principal accepted",
        not is_root or allow_root,
        {"arn": identity_arn, "isRoot": is_root, "rootExplicitlyAccepted": allow_root},
        "non-root principal, or explicit --allow-root after user confirmation",
        "Root credentials require explicit user approval before any AWS mutation.",
    )


def cost_checks(cost_model: dict[str, Any]) -> list[Check]:
    operational = float(cost_model["operationalMaximumUsd"])
    maximum = float(cost_model["maximumIncludingCleanupUsd"])
    cleanup_reserve = float(cost_model["cleanupReserveUsd"])
    return [
        Check(
            "cost before cleanup reserve",
            operational < NEW_LOAD_STOP_USD,
            operational,
            f"< {NEW_LOAD_STOP_USD:.2f} USD",
            "No new load starts at or above the 12 USD modeled threshold.",
        ),
        Check(
            "cost hard cap",
            maximum <= HARD_CAP_USD and cleanup_reserve >= 3.0,
            {"maximumIncludingCleanupUsd": maximum, "cleanupReserveUsd": cleanup_reserve},
            {"hardCapUsd": HARD_CAP_USD, "minimumCleanupReserveUsd": 3.0},
            "Maximum includes the unconditional cleanup reserve.",
        ),
    ]


class AwsPreflight:
    def __init__(self, region: str, expected_account: str, allow_root: bool) -> None:
        self.region = region
        self.expected_account = expected_account
        self.allow_root = allow_root
        self.session = boto3.Session(region_name=region)
        self.client: Callable[[str], Any] = lambda service: self.session.client(
            service,
            region_name=region,
            config=SDK_CONFIG,
        )

    def run(self, price_document: dict[str, Any], cost_model: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC)
        checks: list[Check] = []
        identity = self.client("sts").get_caller_identity()
        account = str(identity["Account"])
        arn = str(identity["Arn"])
        checks.extend([
            Check(
                "explicit region",
                self.region == EXPECTED_REGION,
                self.region,
                EXPECTED_REGION,
                "All clients are constructed with the explicit experiment region.",
            ),
            Check(
                "AWS account",
                account == self.expected_account,
                account,
                self.expected_account,
                "The account must match the frozen experiment contract.",
            ),
            operator_check(arn, self.allow_root),
        ])

        price_age = price_age_seconds(price_document, now)
        checks.append(Check(
            "price freshness",
            price_document.get("region") == self.region and price_age <= PRICE_MAX_AGE_SECONDS,
            {"region": price_document.get("region"), "ageSeconds": round(price_age, 3)},
            {"region": self.region, "maximumAgeSeconds": PRICE_MAX_AGE_SECONDS},
            "Prices come from the public AWS Price List API immediately before preflight.",
        ))
        checks.extend(cost_checks(cost_model))
        checks.extend(self._ownership_checks())
        checks.extend(self._compute_checks())
        checks.extend(self._kinesis_checks())
        checks.extend(self._lambda_checks())
        checks.extend(self._network_checks())
        checks.extend(self._bootstrap_and_ami_checks(account))

        check_documents = [check.as_dict() for check in checks]
        non_operator = [item for item in check_documents if item["name"] != "operator principal accepted"]
        return {
            "schemaVersion": 1,
            "generatedAt": now.isoformat().replace("+00:00", "Z"),
            "readOnly": True,
            "account": account,
            "region": self.region,
            "identity": {"arn": arn},
            "checks": check_documents,
            "gateSummary": {
                "infrastructurePass": all(item["pass"] for item in non_operator),
                "operatorPass": next(
                    item["pass"] for item in check_documents
                    if item["name"] == "operator principal accepted"
                ),
                "passForDeploy": all(item["pass"] for item in check_documents),
            },
        }

    def _ownership_checks(self) -> list[Check]:
        cloudformation = self.client("cloudformation")
        try:
            stack = cloudformation.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
            stack_observed: Any = {"name": stack["StackName"], "status": stack["StackStatus"]}
            stack_absent = stack["StackStatus"] == "DELETE_COMPLETE"
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ValidationError":
                stack_observed = "absent"
                stack_absent = True
            else:
                raise

        ec2_instances = self._tagged_ec2_instances()
        tagged_resources = self._tagged_resource_arns()
        return [
            Check(
                "Phase 4 stack ownership baseline",
                stack_absent,
                stack_observed,
                "stack absent before deploy",
                "An existing isolated stack is not adopted or replaced.",
            ),
            Check(
                "live Phase 4 EC2 inventory",
                len(ec2_instances) == 0,
                ec2_instances,
                [],
                "Only non-terminated instances with the Phase tag are included.",
            ),
            Check(
                "tagged Phase 4 resource inventory",
                len(tagged_resources) == 0,
                tagged_resources,
                [],
                "No pre-existing resource may share the experiment ownership tag.",
            ),
        ]

    def _tagged_ec2_instances(self) -> list[dict[str, str]]:
        ec2 = self.client("ec2")
        paginator = ec2.get_paginator("describe_instances")
        instances: list[dict[str, str]] = []
        for page in paginator.paginate(Filters=[
            {"Name": "tag:Phase", "Values": [PHASE_TAG]},
            {"Name": "instance-state-name", "Values": [
                "pending", "running", "stopping", "stopped", "shutting-down",
            ]},
        ]):
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    instances.append({
                        "instanceId": instance["InstanceId"],
                        "state": instance["State"]["Name"],
                    })
        return sorted(instances, key=lambda item: item["instanceId"])

    def _tagged_resource_arns(self) -> list[str]:
        tagging = self.client("resourcegroupstaggingapi")
        paginator = tagging.get_paginator("get_resources")
        arns: list[str] = []
        for page in paginator.paginate(TagFilters=[{"Key": "Phase", "Values": [PHASE_TAG]}]):
            arns.extend(mapping["ResourceARN"] for mapping in page["ResourceTagMappingList"])
        return sorted(set(arns))

    def _compute_checks(self) -> list[Check]:
        ec2 = self.client("ec2")
        quota = self.client("service-quotas").get_service_quota(
            ServiceCode="ec2",
            QuotaCode="L-1216C47A",
        )["Quota"]
        quota_vcpus = int(quota["Value"])
        running_types: list[str] = []
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate(Filters=[{
            "Name": "instance-state-name", "Values": ["pending", "running"],
        }]):
            running_types.extend(
                instance["InstanceType"]
                for reservation in page["Reservations"]
                for instance in reservation["Instances"]
            )
        type_vcpus: dict[str, int] = {}
        if running_types:
            for offset in range(0, len(set(running_types)), 100):
                chunk = sorted(set(running_types))[offset:offset + 100]
                response = ec2.describe_instance_types(InstanceTypes=chunk)
                type_vcpus.update({
                    item["InstanceType"]: int(item["VCpuInfo"]["DefaultVCpus"])
                    for item in response["InstanceTypes"]
                })
        current_vcpus = sum(type_vcpus[item] for item in running_types)

        offerings: dict[str, bool] = {}
        for instance_type in ["r7g.2xlarge", "c7g.2xlarge"]:
            response = ec2.describe_instance_type_offerings(
                LocationType="availability-zone",
                Filters=[
                    {"Name": "location", "Values": [EXPECTED_AZ]},
                    {"Name": "instance-type", "Values": [instance_type]},
                ],
            )
            offerings[instance_type] = any(
                item["InstanceType"] == instance_type
                for item in response["InstanceTypeOfferings"]
            )
        return [
            Check(
                "EC2 Standard On-Demand vCPU quota",
                current_vcpus + REQUIRED_STANDARD_VCPUS <= quota_vcpus,
                {
                    "conservativeCurrentAllFamilyVcpus": current_vcpus,
                    "requiredAdditionalVcpus": REQUIRED_STANDARD_VCPUS,
                    "quotaVcpus": quota_vcpus,
                    "runningInstanceTypes": sorted(running_types),
                },
                f"current + {REQUIRED_STANDARD_VCPUS} <= {quota_vcpus}",
                "All current instance families are conservatively counted against the Standard quota.",
            ),
            Check(
                "EC2 instance type offerings",
                all(offerings.values()),
                {"availabilityZone": EXPECTED_AZ, "offerings": offerings},
                {"r7g.2xlarge": True, "c7g.2xlarge": True},
                "Offering presence is not a capacity reservation; launch failure remains a stop gate.",
            ),
        ]

    def _kinesis_checks(self) -> list[Check]:
        limits = self.client("kinesis").describe_limits()
        open_shards = int(limits["OpenShardCount"])
        shard_limit = int(limits["ShardLimit"])
        return [Check(
            "Kinesis shard quota",
            open_shards + REQUIRED_KINESIS_SHARDS <= shard_limit,
            {"openShards": open_shards, "additionalShards": REQUIRED_KINESIS_SHARDS, "limit": shard_limit},
            f"open + {REQUIRED_KINESIS_SHARDS} <= {shard_limit}",
            "The test creates one provisioned 120-shard stream.",
        )]

    def _lambda_checks(self) -> list[Check]:
        client = self.client("lambda")
        settings = client.get_account_settings()["AccountLimit"]
        concurrent_limit = int(settings["ConcurrentExecutions"])
        reserved_total = 0
        paginator = client.get_paginator("list_functions")
        for page in paginator.paginate():
            for function in page["Functions"]:
                try:
                    response = client.get_function_concurrency(FunctionName=function["FunctionName"])
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                        continue
                    raise
                reserved_total += int(response.get("ReservedConcurrentExecutions", 0))
        remaining = concurrent_limit - reserved_total - REQUIRED_LAMBDA_RESERVED_CONCURRENCY
        return [Check(
            "Lambda reserved concurrency",
            remaining >= MINIMUM_UNRESERVED_LAMBDA_CONCURRENCY,
            {
                "accountLimit": concurrent_limit,
                "currentlyReserved": reserved_total,
                "phase4Reservation": REQUIRED_LAMBDA_RESERVED_CONCURRENCY,
                "unreservedAfterDeploy": remaining,
            },
            f"unreserved after deploy >= {MINIMUM_UNRESERVED_LAMBDA_CONCURRENCY}",
            "The fixed reservation must not reduce account unreserved capacity below 100.",
        )]

    def _network_checks(self) -> list[Check]:
        ec2 = self.client("ec2")
        quotas = self.client("service-quotas")
        vpc_quota = int(quotas.get_service_quota(
            ServiceCode="vpc", QuotaCode="L-F678F1CE",
        )["Quota"]["Value"])
        eni_quota = int(quotas.get_service_quota(
            ServiceCode="vpc", QuotaCode="L-DF5E4CA3",
        )["Quota"]["Value"])
        vpc_count = len(ec2.describe_vpcs()["Vpcs"])
        eni_count = len(ec2.describe_network_interfaces()["NetworkInterfaces"])
        bucket_count = len(self.client("s3").list_buckets()["Buckets"])
        return [
            Check(
                "VPC quota",
                vpc_count + 1 <= vpc_quota,
                {"current": vpc_count, "additional": 1, "limit": vpc_quota},
                "current + 1 <= quota",
                "Phase 4 creates an isolated VPC and never replaces the shared dev VPC.",
            ),
            Check(
                "network interface quota",
                eni_count + PLANNED_NETWORK_INTERFACES <= eni_quota,
                {"current": eni_count, "conservativeAdditional": PLANNED_NETWORK_INTERFACES, "limit": eni_quota},
                "current + 16 <= quota",
                "Allowance covers EC2, interface endpoint, producer, and Lambda Hyperplane ENIs.",
            ),
            Check(
                "S3 general-purpose bucket count",
                bucket_count + 2 <= 10_000,
                {"current": bucket_count, "additional": 2, "documentedDefaultQuota": 10_000},
                "current + 2 <= 10,000",
                "The stack creates run-scoped failure and archive buckets.",
            ),
            Check(
                "NAT and Elastic IP requirement",
                True,
                {"natGateways": 0, "elasticIps": 0, "autoAssignedPublicIpv4": 2},
                {"natGateways": 0, "elasticIps": 0},
                "ClickHouse and the later producer use auto-assigned public IPv4; 8123 remains private-only.",
            ),
        ]

    def _bootstrap_and_ami_checks(self, account: str) -> list[Check]:
        ssm = self.client("ssm")
        ec2 = self.client("ec2")
        bootstrap_version = int(ssm.get_parameter(Name=BOOTSTRAP_VERSION_PARAMETER)["Parameter"]["Value"])
        ami_id = ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]
        images = ec2.describe_images(ImageIds=[ami_id])["Images"]
        ami_ok = (
            len(images) == 1
            and images[0].get("Architecture") == "arm64"
            and images[0].get("State") == "available"
        )
        iam = self.client("iam")
        deploy_role_name = f"cdk-hnb659fds-deploy-role-{account}-{self.region}"
        try:
            role = iam.get_role(RoleName=deploy_role_name)["Role"]
            deploy_role_observed: Any = {"roleName": role["RoleName"], "arn": role["Arn"]}
            deploy_role_exists = True
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "NoSuchEntity":
                deploy_role_observed = "absent"
                deploy_role_exists = False
            else:
                raise
        return [
            Check(
                "CDK bootstrap version",
                bootstrap_version >= 6,
                bootstrap_version,
                ">= 6",
                "Modern bootstrap resources are required for isolated named-stack deployment.",
            ),
            Check(
                "CDK deployment role exists",
                deploy_role_exists,
                deploy_role_observed,
                deploy_role_name,
                "Existence is checked read-only; deploy role assumption is verified after operator approval.",
            ),
            Check(
                "AL2023 arm64 AMI",
                ami_ok,
                {
                    "parameter": AMI_PARAMETER,
                    "imageId": ami_id,
                    "architecture": images[0].get("Architecture") if images else None,
                    "state": images[0].get("State") if images else None,
                },
                {"architecture": "arm64", "state": "available"},
                "Both fixed Graviton instance types require an available arm64 image.",
            ),
        ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--cost-model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-root", action="store_true")
    return parser.parse_args()


def write_json_private(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")


def main() -> int:
    args = parse_args()
    if args.region != EXPECTED_REGION:
        raise SystemExit(f"region must be {EXPECTED_REGION}")
    price_document = json.loads(args.prices.read_text(encoding="utf-8"))
    cost_model = json.loads(args.cost_model.read_text(encoding="utf-8"))
    result = AwsPreflight(args.region, args.expected_account, args.allow_root).run(
        price_document,
        cost_model,
    )
    write_json_private(args.output, result)
    print(json.dumps({
        "account": result["account"],
        "region": result["region"],
        "identity": result["identity"],
        "failedChecks": [
            item["name"] for item in result["checks"] if not item["pass"]
        ],
        "gateSummary": result["gateSummary"],
    }, indent=2))
    return 0 if result["gateSummary"]["passForDeploy"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

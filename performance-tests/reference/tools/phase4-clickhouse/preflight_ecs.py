#!/usr/bin/env python3
"""Read-only AWS, quota, cost, and ownership preflight for Phase 4 ECS."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


EXPECTED_REGION = "ap-northeast-2"
EXPECTED_AZ = "ap-northeast-2a"
RUNTIME_STACK_NAME = "LoopAdPerfPhase4ClickHouseEcsStack"
IMAGE_STACK_NAME = "LoopAdPerfPhase4ClickHouseEcsImageStack"
PHASE_TAG_VALUES = {"phase4-clickhouse-ecs", "phase4-clickhouse-ecs-image"}
REQUIRED_STANDARD_VCPUS = 20
REQUIRED_KINESIS_SHARDS = 120
PLANNED_NETWORK_INTERFACES = 16
PLANNED_INTERFACE_ENDPOINTS = 9
PLANNED_DYNAMODB_TABLES = 3
PLANNED_IAM_ROLES = 5
PLANNED_INSTANCE_PROFILES = 3
ECR_REQUIRED_PUSH_QUOTAS = {
    "Basic image scans per 24 hours": 1.0,
    "Rate of BatchCheckLayerAvailability requests": 1.0,
    "Rate of CompleteLayerUpload requests": 1.0,
    "Rate of GetAuthorizationToken requests": 1.0,
    "Rate of InitiateLayerUpload requests": 1.0,
    "Rate of PutImage requests": 1.0,
    "Rate of UploadLayerPart requests": 1.0,
}
PRICE_MAX_AGE_SECONDS = 3_600
NEW_LOAD_STOP_USD = 17.0
HARD_CAP_USD = 20.0
BOOTSTRAP_VERSION_PARAMETER = "/cdk-bootstrap/hnb659fds/version"
ECS_AMI_PARAMETER = "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id"
CLICKHOUSE_AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"

SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "total_max_attempts": 5},
    user_agent_appid="loopad-phase4-ecs-preflight/1",
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
    workload = cost_model.get("workload")
    return [
        Check(
            "ECS cost model identity",
            workload == "phase4-kinesis-ecs-ec2-clickhouse",
            workload,
            "phase4-kinesis-ecs-ec2-clickhouse",
            "A Lambda cost model cannot authorize the ECS experiment.",
        ),
        Check(
            "cost before cleanup reserve",
            operational < NEW_LOAD_STOP_USD,
            operational,
            f"< {NEW_LOAD_STOP_USD:.2f} USD",
            "No new load starts at or above the 17 USD modeled threshold.",
        ),
        Check(
            "cost hard cap",
            maximum <= HARD_CAP_USD and cleanup_reserve >= 3.0,
            {"maximumIncludingCleanupUsd": maximum, "cleanupReserveUsd": cleanup_reserve},
            {"hardCapUsd": HARD_CAP_USD, "minimumCleanupReserveUsd": 3.0},
            "Maximum includes the unconditional cleanup reserve.",
        ),
    ]


def quota_value(quotas: Iterable[dict[str, Any]], names: set[str]) -> float:
    matches = [
        float(quota["Value"])
        for quota in quotas
        if str(quota.get("QuotaName", "")) in names
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one quota named {sorted(names)}, found {len(matches)}")
    return matches[0]


def named_quota_values(quotas: Iterable[dict[str, Any]]) -> dict[str, float]:
    return {
        str(quota["QuotaName"]): float(quota["Value"])
        for quota in quotas
        if "QuotaName" in quota and "Value" in quota
    }


class AwsPreflight:
    def __init__(
        self,
        region: str,
        expected_account: str,
        allow_root: bool,
        run_id: str,
        session_id: str,
    ) -> None:
        self.region = region
        self.expected_account = expected_account
        self.allow_root = allow_root
        self.run_id = run_id
        self.session_id = session_id
        self.session = boto3.Session(region_name=region)
        self._clients: dict[str, Any] = {}
        self.client: Callable[[str], Any] = self._client

    def _client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = self.session.client(
                service,
                region_name=self.region,
                config=SDK_CONFIG,
            )
        return self._clients[service]

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
                "Every client uses the frozen experiment region.",
            ),
            Check(
                "AWS account",
                account == self.expected_account,
                account,
                self.expected_account,
                "The account must match run.json.",
            ),
            operator_check(arn, self.allow_root),
        ])
        price_age = price_age_seconds(price_document, now)
        checks.append(Check(
            "price freshness",
            price_document.get("region") == self.region and price_age <= PRICE_MAX_AGE_SECONDS,
            {"region": price_document.get("region"), "ageSeconds": round(price_age, 3)},
            {"region": self.region, "maximumAgeSeconds": PRICE_MAX_AGE_SECONDS},
            "Prices must be refreshed immediately before the live preflight.",
        ))
        checks.extend(cost_checks(cost_model))
        checks.extend(self._ownership_checks())
        checks.extend(self._compute_checks())
        checks.extend(self._kinesis_checks())
        checks.extend(self._network_checks())
        checks.extend(self._service_capacity_checks())
        checks.extend(self._identity_capacity_checks())
        checks.extend(self._bootstrap_and_ami_checks(account))

        documents = [check.as_dict() for check in checks]
        non_operator = [item for item in documents if item["name"] != "operator principal accepted"]
        return {
            "schemaVersion": 1,
            "workload": "phase4-kinesis-ecs-ec2-clickhouse",
            "generatedAt": now.isoformat().replace("+00:00", "Z"),
            "readOnly": True,
            "account": account,
            "region": self.region,
            "identity": {"arn": arn},
            "checks": documents,
            "gateSummary": {
                "infrastructurePass": all(item["pass"] for item in non_operator),
                "operatorPass": next(
                    item["pass"] for item in documents
                    if item["name"] == "operator principal accepted"
                ),
                "passForDeploy": all(item["pass"] for item in documents),
            },
        }

    def _ownership_checks(self) -> list[Check]:
        stacks = {name: self._stack_state(name) for name in [RUNTIME_STACK_NAME, IMAGE_STACK_NAME]}
        instances = self._tagged_ec2_instances()
        resources = self._tagged_resource_arns()
        return [
            Check(
                "Phase 4 ECS stack ownership baseline",
                all(value == "absent" for value in stacks.values()),
                stacks,
                {name: "absent" for name in stacks},
                "Neither the runtime nor image support stack may be adopted or replaced.",
            ),
            Check(
                "live Phase 4 ECS EC2 inventory",
                len(instances) == 0,
                instances,
                [],
                "Only non-terminated instances with active ECS Phase tags are included.",
            ),
            Check(
                "tagged Phase 4 ECS resource inventory",
                len(resources) == 0,
                resources,
                [],
                "No pre-existing resource may share the exact active run/session identity.",
            ),
        ]

    def _stack_state(self, stack_name: str) -> Any:
        client = self.client("cloudformation")
        try:
            stack = client.describe_stacks(StackName=stack_name)["Stacks"][0]
            return {"name": stack["StackName"], "status": stack["StackStatus"]}
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ValidationError":
                return "absent"
            raise

    def _tagged_ec2_instances(self) -> list[dict[str, str]]:
        paginator = self.client("ec2").get_paginator("describe_instances")
        instances: list[dict[str, str]] = []
        for phase in sorted(PHASE_TAG_VALUES):
            for page in paginator.paginate(Filters=[
                {"Name": "tag:Phase", "Values": [phase]},
                {"Name": "instance-state-name", "Values": [
                    "pending", "running", "stopping", "stopped", "shutting-down",
                ]},
            ]):
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        instances.append({
                            "instanceId": instance["InstanceId"],
                            "state": instance["State"]["Name"],
                            "phase": phase,
                        })
        return sorted(instances, key=lambda item: item["instanceId"])

    def _tagged_resource_arns(self) -> list[str]:
        paginator = self.client("resourcegroupstaggingapi").get_paginator("get_resources")
        arns = [
            item["ResourceARN"]
            for page in paginator.paginate(TagFilters=[
                {"Key": "RunId", "Values": [self.run_id]},
                {"Key": "SessionId", "Values": [self.session_id]},
            ])
            for item in page["ResourceTagMappingList"]
        ]
        return sorted(set(arns))

    def _compute_checks(self) -> list[Check]:
        ec2 = self.client("ec2")
        quota = self.client("service-quotas").get_service_quota(
            ServiceCode="ec2",
            QuotaCode="L-1216C47A",
        )["Quota"]
        quota_vcpus = int(quota["Value"])
        running_types: list[str] = []
        for page in ec2.get_paginator("describe_instances").paginate(Filters=[{
            "Name": "instance-state-name", "Values": ["pending", "running"],
        }]):
            running_types.extend(
                instance["InstanceType"]
                for reservation in page["Reservations"]
                for instance in reservation["Instances"]
            )
        type_vcpus: dict[str, int] = {}
        unique_types = sorted(set(running_types))
        for offset in range(0, len(unique_types), 100):
            response = ec2.describe_instance_types(InstanceTypes=unique_types[offset:offset + 100])
            type_vcpus.update({
                item["InstanceType"]: int(item["VCpuInfo"]["DefaultVCpus"])
                for item in response["InstanceTypes"]
            })
        current_vcpus = sum(type_vcpus[item] for item in running_types)

        required_types = ["r7g.2xlarge", "c7g.2xlarge", "c7g.large"]
        offerings: dict[str, bool] = {}
        for instance_type in required_types:
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
        host_type = ec2.describe_instance_types(InstanceTypes=["c7g.large"])["InstanceTypes"][0]
        maximum_enis = int(host_type["NetworkInfo"]["MaximumNetworkInterfaces"])
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
                "The allowance includes ClickHouse 8, producer 8, and two ECS hosts totaling 4 vCPU.",
            ),
            Check(
                "EC2 instance type offerings",
                all(offerings.values()),
                {"availabilityZone": EXPECTED_AZ, "offerings": offerings},
                {instance_type: True for instance_type in required_types},
                "Offering presence is not a capacity reservation; a launch failure remains a stop gate.",
            ),
            Check(
                "c7g.large awsvpc ENI placement",
                maximum_enis >= 2,
                {"maximumNetworkInterfaces": maximum_enis, "primaryPlusTaskRequired": 2},
                "maximumNetworkInterfaces >= 2",
                "Each host needs its primary ENI and one distinct awsvpc task ENI.",
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

    def _network_checks(self) -> list[Check]:
        ec2 = self.client("ec2")
        service_quotas = self.client("service-quotas")
        vpc_quota = int(service_quotas.get_service_quota(
            ServiceCode="vpc", QuotaCode="L-F678F1CE",
        )["Quota"]["Value"])
        eni_quota = int(service_quotas.get_service_quota(
            ServiceCode="vpc", QuotaCode="L-DF5E4CA3",
        )["Quota"]["Value"])
        endpoint_quota = int(service_quotas.get_service_quota(
            ServiceCode="vpc", QuotaCode="L-29B6F2EB",
        )["Quota"]["Value"])
        vpc_count = self._ec2_count("describe_vpcs", "Vpcs")
        eni_count = self._ec2_count("describe_network_interfaces", "NetworkInterfaces")
        bucket_count = len(self.client("s3").list_buckets()["Buckets"])
        return [
            Check(
                "VPC quota",
                vpc_count + 1 <= vpc_quota,
                {"current": vpc_count, "additional": 1, "limit": vpc_quota},
                "current + 1 <= quota",
                "Phase 4 creates one isolated VPC and never replaces the shared dev VPC.",
            ),
            Check(
                "network interface quota",
                eni_count + PLANNED_NETWORK_INTERFACES <= eni_quota,
                {"current": eni_count, "additional": PLANNED_NETWORK_INTERFACES, "limit": eni_quota},
                f"current + {PLANNED_NETWORK_INTERFACES} <= quota",
                "Allowance covers ClickHouse, two hosts, two task ENIs, nine endpoints, and producer.",
            ),
            Check(
                "interface endpoint quota per new VPC",
                PLANNED_INTERFACE_ENDPOINTS <= endpoint_quota,
                {"planned": PLANNED_INTERFACE_ENDPOINTS, "limit": endpoint_quota},
                f"{PLANNED_INTERFACE_ENDPOINTS} <= quota",
                "The private consumer subnet uses nine single-AZ interface endpoints.",
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
                "Only ClickHouse and the temporary producer use auto-assigned public IPv4.",
            ),
        ]

    def _ec2_count(self, operation: str, result_key: str) -> int:
        client = self.client("ec2")
        paginator = client.get_paginator(operation)
        return sum(len(page.get(result_key, [])) for page in paginator.paginate())

    def _service_capacity_checks(self) -> list[Check]:
        ecs_quotas = self._all_default_service_quotas("ecs")
        ecs_quota = quota_value(
            ecs_quotas,
            {"Clusters per Region", "Clusters per account"},
        )
        ecs_container_instances = quota_value(
            ecs_quotas,
            {"Container instances per cluster"},
        )
        ecs_tasks_per_service = quota_value(
            ecs_quotas,
            {"Tasks per service"},
        )
        ecs_provisioning_tasks = quota_value(
            ecs_quotas,
            {"Tasks in PROVISIONING state per cluster"},
        )
        cluster_count = sum(
            len(page.get("clusterArns", []))
            for page in self.client("ecs").get_paginator("list_clusters").paginate()
        )
        ecr_quotas = named_quota_values(self._all_service_quotas("ecr"))
        ecr_push_observed = {
            name: ecr_quotas.get(name)
            for name in ECR_REQUIRED_PUSH_QUOTAS
        }
        ecr_push_quota_pass = all(
            ecr_push_observed[name] is not None
            and float(ecr_push_observed[name]) >= minimum
            for name, minimum in ECR_REQUIRED_PUSH_QUOTAS.items()
        )
        repository_count = sum(
            len(page.get("repositories", []))
            for page in self.client("ecr").get_paginator("describe_repositories").paginate()
        )
        dynamodb_quota = quota_value(
            self._all_service_quotas("dynamodb"),
            {"Tables per Region", "Maximum number of tables"},
        )
        table_count = sum(
            len(page.get("TableNames", []))
            for page in self.client("dynamodb").get_paginator("list_tables").paginate()
        )
        autoscaling_limits = self.client("autoscaling").describe_account_limits()
        asg_current = int(autoscaling_limits["NumberOfAutoScalingGroups"])
        asg_limit = int(autoscaling_limits["MaxNumberOfAutoScalingGroups"])

        ebs_quota = quota_value(
            self._all_service_quotas("ebs"),
            {"Storage for General Purpose SSD (gp3) volumes, in TiB"},
        )
        current_gp3_gib = 0
        for page in self.client("ec2").get_paginator("describe_volumes").paginate(
            Filters=[{"Name": "volume-type", "Values": ["gp3"]}],
        ):
            current_gp3_gib += sum(int(volume["Size"]) for volume in page["Volumes"])
        return [
            Check(
                "ECS cluster quota",
                cluster_count + 1 <= ecs_quota,
                {"current": cluster_count, "additional": 1, "limit": ecs_quota},
                "current + 1 <= quota",
                "The runtime stack creates one ECS cluster and one two-task service.",
            ),
            Check(
                "ECS container instances per cluster quota",
                2 <= ecs_container_instances,
                {"plannedContainerInstances": 2, "defaultLimit": ecs_container_instances},
                "2 <= default quota",
                "The new cluster registers exactly two fixed c7g.large container instances.",
            ),
            Check(
                "ECS tasks per service quota",
                2 <= ecs_tasks_per_service,
                {"plannedDesiredTasks": 2, "defaultLimit": ecs_tasks_per_service},
                "2 <= default quota",
                "The only service has desiredCount=2 and does not autoscale.",
            ),
            Check(
                "ECS provisioning tasks per cluster quota",
                2 <= ecs_provisioning_tasks,
                {"maximumInitialProvisioningTasks": 2, "defaultLimit": ecs_provisioning_tasks},
                "2 <= default quota",
                "The ASG capacity provider initially provisions two tasks.",
            ),
            Check(
                "Auto Scaling group quota",
                asg_current + 1 <= asg_limit,
                {"current": asg_current, "additional": 1, "limit": asg_limit},
                "current + 1 <= quota",
                "The runtime stack creates one fixed-size ASG.",
            ),
            Check(
                "ECR image push quotas",
                ecr_push_quota_pass,
                {
                    "currentRepositories": repository_count,
                    "requiredSinglePushQuotaValues": ecr_push_observed,
                    "registeredRepositoryQuota": (
                        "not exposed by the current private ECR Service Quotas API"
                    ),
                },
                ECR_REQUIRED_PUSH_QUOTAS,
                (
                    "The image support stack creates one immutable repository and pushes one "
                    "scanned image; every currently exposed API/scan quota must allow at least one."
                ),
            ),
            Check(
                "DynamoDB table quota",
                table_count + PLANNED_DYNAMODB_TABLES <= dynamodb_quota,
                {"current": table_count, "additional": PLANNED_DYNAMODB_TABLES, "limit": dynamodb_quota},
                f"current + {PLANNED_DYNAMODB_TABLES} <= quota",
                "KCL 3.x uses lease, worker-metrics, and coordinator-state tables.",
            ),
            Check(
                "EBS gp3 storage quota",
                current_gp3_gib + 500 <= ebs_quota * 1024,
                {"currentGiB": current_gp3_gib, "additionalGiB": 500, "limitTiB": ebs_quota},
                "current GiB + 500 <= quota TiB * 1024",
                "The ClickHouse root volume is gp3 500 GiB, 3,000 IOPS, and 500 MiB/s.",
            ),
        ]

    def _all_service_quotas(self, service_code: str) -> list[dict[str, Any]]:
        paginator = self.client("service-quotas").get_paginator("list_service_quotas")
        return [
            quota
            for page in paginator.paginate(ServiceCode=service_code)
            for quota in page.get("Quotas", [])
        ]

    def _all_default_service_quotas(self, service_code: str) -> list[dict[str, Any]]:
        paginator = self.client("service-quotas").get_paginator(
            "list_aws_default_service_quotas"
        )
        return [
            quota
            for page in paginator.paginate(ServiceCode=service_code)
            for quota in page.get("Quotas", [])
        ]

    def _identity_capacity_checks(self) -> list[Check]:
        summary = self.client("iam").get_account_summary()["SummaryMap"]
        roles = int(summary["Roles"])
        roles_quota = int(summary["RolesQuota"])
        profiles = int(summary["InstanceProfiles"])
        profiles_quota = int(summary["InstanceProfilesQuota"])
        return [
            Check(
                "IAM role quota",
                roles + PLANNED_IAM_ROLES <= roles_quota,
                {"current": roles, "additional": PLANNED_IAM_ROLES, "limit": roles_quota},
                f"current + {PLANNED_IAM_ROLES} <= quota",
                "The runtime stack creates five run-scoped roles including the producer role.",
            ),
            Check(
                "IAM instance profile quota",
                profiles + PLANNED_INSTANCE_PROFILES <= profiles_quota,
                {"current": profiles, "additional": PLANNED_INSTANCE_PROFILES, "limit": profiles_quota},
                f"current + {PLANNED_INSTANCE_PROFILES} <= quota",
                "ClickHouse, the ECS host ASG, and the producer each require an instance profile.",
            ),
        ]

    def _bootstrap_and_ami_checks(self, account: str) -> list[Check]:
        ssm = self.client("ssm")
        ec2 = self.client("ec2")
        bootstrap_version = int(ssm.get_parameter(Name=BOOTSTRAP_VERSION_PARAMETER)["Parameter"]["Value"])
        ami_parameters = {
            "ecs": ECS_AMI_PARAMETER,
            "clickhouse": CLICKHOUSE_AMI_PARAMETER,
        }
        ami_ids = {
            key: ssm.get_parameter(Name=parameter)["Parameter"]["Value"]
            for key, parameter in ami_parameters.items()
        }
        images = {
            image["ImageId"]: image
            for image in ec2.describe_images(ImageIds=sorted(set(ami_ids.values())))["Images"]
        }
        ami_observed = {
            key: {
                "parameter": ami_parameters[key],
                "imageId": image_id,
                "architecture": images.get(image_id, {}).get("Architecture"),
                "state": images.get(image_id, {}).get("State"),
            }
            for key, image_id in ami_ids.items()
        }
        ami_ok = all(
            item["architecture"] == "arm64" and item["state"] == "available"
            for item in ami_observed.values()
        )
        deploy_role_name = f"cdk-hnb659fds-deploy-role-{account}-{self.region}"
        iam = self.client("iam")
        try:
            role = iam.get_role(RoleName=deploy_role_name)["Role"]
            role_observed: Any = {"roleName": role["RoleName"], "arn": role["Arn"]}
            role_exists = True
        except iam.exceptions.NoSuchEntityException:
            role_observed = "absent"
            role_exists = False
        return [
            Check(
                "CDK bootstrap version",
                bootstrap_version >= 6,
                bootstrap_version,
                ">= 6",
                "Modern bootstrap resources are required for the two isolated named stacks.",
            ),
            Check(
                "CDK deployment role exists",
                role_exists,
                role_observed,
                deploy_role_name,
                "Role existence is checked read-only; mutation remains gated separately.",
            ),
            Check(
                "ARM64 AMIs",
                ami_ok,
                ami_observed,
                {"ecs": {"architecture": "arm64", "state": "available"}, "clickhouse": {"architecture": "arm64", "state": "available"}},
                "The ECS-optimized and generic AL2023 images are resolved independently.",
            ),
        ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--expected-account")
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--cost-model", type=Path)
    parser.add_argument("--output", type=Path)
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
    run_path = args.run_dir / "run.json"
    run_document = json.loads(run_path.read_text(encoding="utf-8"))
    expected_account = args.expected_account or run_document.get("account") or run_document.get("expectedAccount")
    if not isinstance(expected_account, str) or not expected_account.isdigit():
        raise ValueError("expected account must be provided or present in run.json")
    prices_path = args.prices or args.run_dir / "prices-ecs.json"
    cost_path = args.cost_model or args.run_dir / "cost-model-ecs.json"
    output_path = args.output or args.run_dir / "preflight-ecs.json"
    run_id = str(run_document.get("runId", ""))
    session_id = str(run_document.get("sessionId", ""))
    result = AwsPreflight(
        args.region,
        expected_account,
        args.allow_root,
        run_id,
        session_id,
    ).run(
        json.loads(prices_path.read_text(encoding="utf-8")),
        json.loads(cost_path.read_text(encoding="utf-8")),
    )
    write_json_private(output_path, result)
    print(json.dumps({
        "account": result["account"],
        "region": result["region"],
        "identity": result["identity"],
        "failedChecks": [item["name"] for item in result["checks"] if not item["pass"]],
        "gateSummary": result["gateSummary"],
    }, indent=2))
    return 0 if result["gateSummary"]["passForDeploy"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only Phase 7-2 identity, ownership, quota, price, and image preflight."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from common import (
    EXPECTED_ACCOUNT,
    EXPECTED_OPERATOR_ARN,
    EXPECTED_REGION,
    IMAGE_STACK_NAME,
    PHASE7_COLLECTOR_COMMIT,
    RUNTIME_STACK_NAME,
    Check,
    checks_document,
    expected_tags,
    handoff_checks,
    image_source_closure_sha256,
    parse_utc,
    read_json,
    reject_strict_paid_work_under_composite_policy,
    tag_map,
    tags_match,
    scoped_diagnostic_source_checks,
    utc_now,
    validate_identifiers,
    write_json,
)


PLANNED_STANDARD_VCPUS = 64
PLANNED_KINESIS_SHARDS = 120
INSTANCE_TYPES = ["c6i.xlarge", "c6in.xlarge", "c6in.large", "c7g.large", "r7g.2xlarge"]
PHASE7_AVAILABILITY_ZONES = {"ap-northeast-2a", "ap-northeast-2c"}
STANDARD_VCPU_QUOTA_CODE = "L-1216C47A"
BOOTSTRAP_PARAMETER = "/cdk-bootstrap/hnb659fds/version"
PRICE_MAX_AGE_SECONDS = 3600
SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "total_max_attempts": 5},
    user_agent_appid="loopad-phase7-preflight/1",
)
FORBIDDEN_CREDENTIAL_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


class AwsSnapshot:
    def __init__(self, region: str = EXPECTED_REGION) -> None:
        reject_credential_environment()
        self.session = boto3.Session(region_name=region)
        self.region = region
        self._clients: dict[str, Any] = {}

    def client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = self.session.client(service, region_name=self.region, config=SDK_CONFIG)
        return self._clients[service]

    def collect(self, run_id: str, session_id: str, x86_ami: str, arm_ami: str,
                certificate_arn: str) -> dict[str, Any]:
        identity = self.client("sts").get_caller_identity()
        cli_identity = collect_cli_identity()
        instance_ids, current_vcpus = self._current_standard_vcpus()
        kinesis_limits = self.client("kinesis").describe_limits()
        return {
            "capturedAt": utc_now(),
            "region": self.region,
            "identity": {"account": identity["Account"], "arn": identity["Arn"]},
            "cliIdentity": cli_identity,
            "stacks": {
                "runtime": self._stack(RUNTIME_STACK_NAME),
                "image": self._stack(IMAGE_STACK_NAME),
            },
            "ecrRepositories": self._ecr_repositories(run_id),
            "ownedTaggedResources": self._tagged_resources(run_id, session_id),
            "quota": {
                "standardVcpus": float(self.client("service-quotas").get_service_quota(
                    ServiceCode="ec2", QuotaCode=STANDARD_VCPU_QUOTA_CODE
                )["Quota"]["Value"]),
                "currentStandardVcpus": current_vcpus,
                "currentInstanceIds": instance_ids,
                "kinesisShardLimit": int(kinesis_limits["ShardLimit"]),
                "kinesisOpenShardCount": int(kinesis_limits["OpenShardCount"]),
            },
            "offerings": self._offerings(),
            "bootstrapVersion": int(self.client("ssm").get_parameter(Name=BOOTSTRAP_PARAMETER)["Parameter"]["Value"]),
            "amis": {
                "x86": self._ami(x86_ami),
                "arm": self._ami(arm_ami),
            },
            "certificate": self._certificate(certificate_arn),
        }

    def _stack(self, name: str) -> dict[str, Any] | None:
        client = self.client("cloudformation")
        try:
            stack = client.describe_stacks(StackName=name)["Stacks"][0]
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ValidationError":
                return None
            raise
        return {
            "name": stack["StackName"],
            "arn": stack["StackId"],
            "status": stack["StackStatus"],
            "tags": tag_map(stack.get("Tags", [])),
            "outputs": {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])},
        }

    def _ecr_repositories(self, run_id: str) -> list[dict[str, Any]]:
        prefix = f"loop-ad/perf-phase7/{run_id}/"
        client = self.client("ecr")
        repositories = [
            repository
            for page in client.get_paginator("describe_repositories").paginate()
            for repository in page.get("repositories", [])
            if str(repository.get("repositoryName", "")).startswith(prefix)
        ]
        result: list[dict[str, Any]] = []
        for repository in repositories:
            name = repository["repositoryName"]
            images = [
                detail
                for page in client.get_paginator("describe_images").paginate(repositoryName=name)
                for detail in page.get("imageDetails", [])
            ]
            result.append({
                "name": name,
                "arn": repository["repositoryArn"],
                "mutability": repository.get("imageTagMutability"),
                "scanOnPush": repository.get("imageScanningConfiguration", {}).get("scanOnPush"),
                "images": [{"digest": image.get("imageDigest"), "tags": sorted(image.get("imageTags", []))} for image in images],
            })
        return sorted(result, key=lambda item: item["name"])

    def _tagged_resources(self, run_id: str, session_id: str) -> list[str]:
        filters = [{"Key": key, "Values": [value]} for key, value in expected_tags(run_id, session_id).items()]
        return sorted({
            item["ResourceARN"]
            for page in self.client("resourcegroupstaggingapi").get_paginator("get_resources").paginate(TagFilters=filters)
            for item in page.get("ResourceTagMappingList", [])
        })

    def _current_standard_vcpus(self) -> tuple[list[str], int]:
        client = self.client("ec2")
        instances = [
            instance
            for page in client.get_paginator("describe_instances").paginate(Filters=[{
                "Name": "instance-state-name", "Values": ["pending", "running"]
            }])
            for reservation in page.get("Reservations", [])
            for instance in reservation.get("Instances", [])
        ]
        types = sorted({instance["InstanceType"] for instance in instances})
        counts = {item["InstanceType"]: int(item["VCpuInfo"]["DefaultVCpus"]) for item in (
            client.describe_instance_types(InstanceTypes=types).get("InstanceTypes", []) if types else []
        )}
        return sorted(instance["InstanceId"] for instance in instances), sum(counts[instance["InstanceType"]] for instance in instances)

    def _offerings(self) -> dict[str, list[str]]:
        response = self.client("ec2").describe_instance_type_offerings(
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": INSTANCE_TYPES}],
        )
        result = {instance_type: [] for instance_type in INSTANCE_TYPES}
        for item in response.get("InstanceTypeOfferings", []):
            result[item["InstanceType"]].append(item["Location"])
        return {key: sorted(set(value)) for key, value in result.items()}

    def _ami(self, image_id: str) -> dict[str, Any] | None:
        images = self.client("ec2").describe_images(ImageIds=[image_id]).get("Images", [])
        if len(images) != 1:
            return None
        image = images[0]
        return {"imageId": image["ImageId"], "state": image["State"], "architecture": image["Architecture"], "rootDeviceType": image.get("RootDeviceType")}

    def _certificate(self, arn: str) -> dict[str, Any]:
        certificate = self.client("acm").describe_certificate(CertificateArn=arn)["Certificate"]
        return {"arn": arn, "status": certificate["Status"], "domainName": certificate["DomainName"], "inUseBy": certificate.get("InUseBy", [])}


def evaluate_preflight(snapshot: dict[str, Any], handoff: dict[str, Any], handoff_gate: list[Check],
                       price_document: dict[str, Any], cost_model: dict[str, Any], run_id: str,
                       session_id: str, image_state: str, x86_ami: str, arm_ami: str,
                       protocol_dns_name: str, image_manifest: dict[str, Any] | None = None,
                       source_kind: str = "phase7-1-handoff",
                       source_path: str | None = None,
                       campaign_ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_identifiers(run_id, session_id)
    if image_state not in {"absent", "prepared"}:
        raise ValueError("image_state must be absent or prepared")
    now = parse_utc(snapshot["capturedAt"])
    price_age = abs((now - parse_utc(str(price_document.get("asOf")))).total_seconds())
    identity = snapshot.get("identity", {})
    cli_identity = snapshot.get("cliIdentity", {})
    stacks = snapshot.get("stacks", {})
    runtime_stack = stacks.get("runtime")
    image_stack = stacks.get("image")
    repositories = snapshot.get("ecrRepositories", [])
    quota = snapshot.get("quota", {})
    offerings = snapshot.get("offerings", {})
    expected_repository_names = {f"loop-ad/perf-phase7/{run_id}/{role}" for role in ("collector", "consumer", "archive")}
    actual_repository_names = {item.get("name") for item in repositories}
    if source_kind not in {"phase7-1-handoff", "full-stack-scoped-diagnostic-source"}:
        raise ValueError("unsupported source_kind")
    expected_workload = (
        "phase7-end-to-end-integration"
        if source_kind == "phase7-1-handoff"
        else "phase7-full-stack-scoped-archive-diagnostic"
    )
    if source_kind == "full-stack-scoped-diagnostic-source":
        from full_stack_scoped_cost_model import (
            canonical_sha256,
            validate_cost_model,
        )

        deterministic_cost_gate = (
            campaign_ledger is not None
            and validate_cost_model(
                price_document,
                campaign_ledger,
                cost_model,
                expected_run_id=run_id,
                expected_session_id=session_id,
            )
        )
        cost_observed = {
            "workload": cost_model.get("workload"),
            "passed": cost_model.get("passed"),
            "maximumIncludingCleanupUsd": cost_model.get("maximumIncludingCleanupUsd"),
            "projectedCampaignMaximumIncludingCleanupUsd": cost_model.get(
                "projectedCampaignMaximumIncludingCleanupUsd"
            ),
            "phase8PaidAwsExperimentOperationalUpperBoundUsd": cost_model.get(
                "phase8PaidAwsExperimentOperationalUpperBoundUsd"
            ),
            "campaignLedgerSha256": (
                canonical_sha256(campaign_ledger)
                if campaign_ledger is not None
                else None
            ),
            "expectedCampaignLedgerSha256": cost_model.get(
                "campaignLedgerSha256"
            ),
        }
    else:
        deterministic_cost_gate = (
            cost_model.get("workload") == expected_workload
            and cost_model.get("passed") is True
        )
        cost_observed = {
            "workload": cost_model.get("workload"),
            "passed": cost_model.get("passed"),
            "maximumIncludingCleanupUsd": cost_model.get("maximumIncludingCleanupUsd"),
        }
    checks = list(handoff_gate)
    checks.extend([
        Check("explicit AWS region", snapshot.get("region") == EXPECTED_REGION, snapshot.get("region"), EXPECTED_REGION, "Every AWS client is pinned to ap-northeast-2."),
        Check("AWS root identity", identity.get("account") == EXPECTED_ACCOUNT and identity.get("arn") == EXPECTED_OPERATOR_ARN,
              identity, {"account": EXPECTED_ACCOUNT, "arn": EXPECTED_OPERATOR_ARN}, "The user explicitly approved this root operator; no other identity is accepted."),
        Check("AWS CLI root identity", cli_identity.get("account") == EXPECTED_ACCOUNT and cli_identity.get("arn") == EXPECTED_OPERATOR_ARN,
              cli_identity, {"account": EXPECTED_ACCOUNT, "arn": EXPECTED_OPERATOR_ARN}, "Fresh AWS CLI and locked boto3 must independently resolve to the same approved operator."),
        Check("runtime stack absent", runtime_stack is None, runtime_stack, None, "Pre-deployment readiness must not create the runtime stack."),
        Check("EC2 Standard vCPU quota", int(quota.get("currentStandardVcpus", -1)) + PLANNED_STANDARD_VCPUS <= int(quota.get("standardVcpus", -1)),
              {"current": quota.get("currentStandardVcpus"), "planned": PLANNED_STANDARD_VCPUS, "projected": int(quota.get("currentStandardVcpus", -1)) + PLANNED_STANDARD_VCPUS},
              {"quota": quota.get("standardVcpus")}, "Current usage plus all Phase 7 hosts must fit before deployment."),
        Check("Kinesis shard quota", int(quota.get("kinesisOpenShardCount", -1)) + PLANNED_KINESIS_SHARDS <= int(quota.get("kinesisShardLimit", -1)),
              {"current": quota.get("kinesisOpenShardCount"), "planned": PLANNED_KINESIS_SHARDS},
              {"limit": quota.get("kinesisShardLimit")}, "The 120-shard stream must fit without a quota change."),
        Check("instance offerings", all(PHASE7_AVAILABILITY_ZONES.issubset(set(offerings.get(instance_type, []))) for instance_type in INSTANCE_TYPES), offerings,
              "every instance type in ap-northeast-2a and ap-northeast-2c", "The fixed two-AZ topology must be placeable in the exact synthesized AZs."),
        Check("CDK bootstrap", int(snapshot.get("bootstrapVersion", -1)) >= 6, snapshot.get("bootstrapVersion"), ">= 6", "Modern CDK bootstrap is required."),
        Check("x86 ECS AMI", snapshot.get("amis", {}).get("x86") == {"imageId": x86_ami, "state": "available", "architecture": "x86_64", "rootDeviceType": "ebs"},
              snapshot.get("amis", {}).get("x86"), {"imageId": x86_ami, "state": "available", "architecture": "x86_64", "rootDeviceType": "ebs"}, "Pin the current ECS-optimized x86 image."),
        Check("ARM ECS AMI", snapshot.get("amis", {}).get("arm") == {"imageId": arm_ami, "state": "available", "architecture": "arm64", "rootDeviceType": "ebs"},
              snapshot.get("amis", {}).get("arm"), {"imageId": arm_ami, "state": "available", "architecture": "arm64", "rootDeviceType": "ebs"}, "Pin the current ECS-optimized ARM image."),
        Check("ACM certificate", snapshot.get("certificate", {}).get("status") == "ISSUED" and snapshot.get("certificate", {}).get("domainName") == protocol_dns_name,
              snapshot.get("certificate"), {"status": "ISSUED", "domainName": protocol_dns_name}, "Use the existing certificate without changing shared DNS."),
        Check("fresh public prices", price_document.get("region") == EXPECTED_REGION and price_age <= PRICE_MAX_AGE_SECONDS,
              {"region": price_document.get("region"), "ageSeconds": round(price_age, 3)}, {"region": EXPECTED_REGION, "maximumAgeSeconds": PRICE_MAX_AGE_SECONDS}, "Refresh public AWS prices immediately before deployment."),
        Check("deterministic cost gate", deterministic_cost_gate,
              cost_observed,
              {"workload": expected_workload, "passed": True, "hardCapUsd": "60", "cleanupReserveUsd": ">=5"}, "No deployment is allowed when the log/cost reserve gate fails."),
    ])
    if source_kind == "full-stack-scoped-diagnostic-source":
        attempts = campaign_ledger.get("attempts", []) if campaign_ledger else []
        identity_unused = all(
            item.get("runId") != run_id and item.get("sessionId") != session_id
            for item in attempts
            if isinstance(item, dict)
        )
        checks.append(Check(
            "fresh immutable attempt identity",
            identity_unused,
            {"runId": run_id, "sessionId": session_id},
            "neither identifier exists in the hash-linked attempt ledger",
            "A cleaned historical identity is still immutable and may not be reused.",
        ))
    if image_state == "absent":
        checks.extend([
            Check("image stack absent", image_stack is None, image_stack, None, "The first preparation preflight requires no prior image stack."),
            Check("image repositories absent", not repositories, repositories, [], "Do not adopt an existing repository prefix."),
            Check("run-owned tagged inventory absent", snapshot.get("ownedTaggedResources") == [], snapshot.get("ownedTaggedResources"), [], "No active resource may already use this run/session identity."),
        ])
    else:
        manifest_images = image_manifest.get("images", []) if isinstance(image_manifest, dict) else []
        if source_kind == "phase7-1-handoff":
            manifest_time_valid = True
        else:
            try:
                manifest_paid_at = parse_utc(str(image_manifest["paidStartedAt"]))
                manifest_prepared_at = parse_utc(str(image_manifest["preparedAt"]))
                manifest_time_valid = (
                    manifest_paid_at <= manifest_prepared_at <= datetime.now(UTC)
                )
            except (KeyError, TypeError, ValueError):
                manifest_time_valid = False
        manifest_by_role = {item.get("role"): item for item in manifest_images if isinstance(item, dict)}
        actual_by_repository = {item.get("name"): item for item in repositories}
        manifest_matches = (
            len(manifest_images) == 3
            and manifest_time_valid
            and len(manifest_by_role) == 3
            and set(manifest_by_role) == {"collector", "consumer", "archive"}
            and (
                source_kind != "full-stack-scoped-diagnostic-source"
                or image_manifest.get("collectorCommit")
                == PHASE7_COLLECTOR_COMMIT
            )
            and all(
            item.get("architecture") == ("linux/amd64" if role == "collector" else "linux/arm64")
            and item.get("repository") == f"loop-ad/perf-phase7/{run_id}/{role}"
            and (
                source_kind != "full-stack-scoped-diagnostic-source"
                or (
                    re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(item.get("sourceClosureSha256", "")),
                    ) is not None
                    and item.get("sourceClosureSha256")
                    == image_source_closure_sha256(
                        str(role),
                        str(handoff.get("implementationTreeSha256")),
                    )
                )
            )
            and len(actual_by_repository.get(item.get("repository"), {}).get("images", [])) == 1
            and actual_by_repository[item.get("repository")]["images"][0].get("digest") == item.get("digest")
            for role, item in manifest_by_role.items()
            )
        )
        allowed_owned_arns = {item.get("arn") for item in repositories}
        if isinstance(image_stack, dict):
            allowed_owned_arns.add(image_stack.get("arn"))
        unexpected_owned = sorted(set(snapshot.get("ownedTaggedResources", [])) - allowed_owned_arns)
        checks.extend([
            Check("prepared image stack owned", isinstance(image_stack, dict) and image_stack.get("status") == "CREATE_COMPLETE" and tags_match(image_stack.get("tags", {}), run_id, session_id),
                  image_stack, "CREATE_COMPLETE with exact run/session tags", "Only the exact run-owned image stack may be retained."),
            Check("three immutable repositories", actual_repository_names == expected_repository_names and all(item.get("mutability") == "IMMUTABLE" and item.get("scanOnPush") is True for item in repositories),
                  repositories, sorted(expected_repository_names), "Prepared repositories must be exact, immutable, and scan on push."),
            Check("image digest and architecture manifest", manifest_matches, image_manifest, "collector linux/amd64 and consumer/archive linux/arm64 digests exist in exact repositories", "Runtime CDK context must use verified immutable digests."),
            Check("no unexpected prepared resources", unexpected_owned == [], unexpected_owned, [], "Only the image stack and its three repositories may exist before runtime deployment."),
        ])
    documents = checks_document(checks)
    image_authorization = None
    if (
        source_kind == "full-stack-scoped-diagnostic-source"
        and image_state == "prepared"
        and isinstance(image_manifest, dict)
    ):
        image_authorization = {
            "imageManifestSha256": canonical_sha256(image_manifest),
            "digests": {
                role: manifest_by_role[role].get("digest")
                for role in ("collector", "consumer", "archive")
                if role in manifest_by_role
            },
        }
    return {
        "schemaVersion": 1,
        "workload": expected_workload,
        "attemptType": "aws-integration-strict" if source_kind == "phase7-1-handoff" else "aws-full-stack-scoped-diagnostic",
        "promotionEligible": source_kind == "phase7-1-handoff",
        "generatedAt": utc_now(),
        "readOnly": True,
        "runId": run_id,
        "sessionId": session_id,
        "imageState": image_state,
        "handoff": {
            "path": str(handoff.get("localRunPath")) if source_kind == "phase7-1-handoff" else None,
            "implementationTreeSha256": handoff.get("implementationTreeSha256"),
        },
        "sourceAuthorization": {
            "kind": source_kind,
            "path": source_path or (
                str(handoff.get("localRunPath")) if source_kind == "phase7-1-handoff" else None
            ),
            "implementationTreeSha256": handoff.get("implementationTreeSha256"),
        },
        "costAuthorization": {
            "campaignLedgerSha256": cost_model.get("campaignLedgerSha256"),
            "priceDocumentSha256": cost_model.get("priceDocumentSha256"),
            "phase8PromotionPolicySha256": cost_model.get(
                "phase8PromotionPolicySha256"
            ),
        } if source_kind == "full-stack-scoped-diagnostic-source" else None,
        "imageAuthorization": image_authorization,
        "checks": documents,
        "passed": all(item["pass"] for item in documents),
        "snapshot": snapshot,
    }


def collect_cli_identity() -> dict[str, str]:
    reject_credential_environment()
    completed = subprocess.run(
        [
            "aws", "sts", "get-caller-identity", "--region", EXPECTED_REGION,
            "--output", "json", "--no-cli-pager",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(completed.stdout)
    identity = {"account": str(value.get("Account", "")), "arn": str(value.get("Arn", ""))}
    if identity != {"account": EXPECTED_ACCOUNT, "arn": EXPECTED_OPERATOR_ARN}:
        raise RuntimeError("AWS CLI identity differs from the user-approved operator")
    return identity


def reject_credential_environment() -> None:
    if any(os.environ.get(key) for key in FORBIDDEN_CREDENTIAL_ENV):
        raise RuntimeError("preflight refuses AWS credential environment variables; use fresh aws login")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-root", required=True, type=Path)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--handoff", type=Path)
    source_group.add_argument("--scoped-diagnostic-source", type=Path)
    parser.add_argument("--prices", required=True, type=Path)
    parser.add_argument("--cost-model", required=True, type=Path)
    parser.add_argument("--attempt-ledger", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--x86-ami", required=True)
    parser.add_argument("--arm-ami", required=True)
    parser.add_argument("--certificate-arn", required=True)
    parser.add_argument("--protocol-dns-name", required=True)
    parser.add_argument("--image-state", choices=["absent", "prepared"], required=True)
    parser.add_argument("--image-manifest", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("preflight output is immutable and may not be overwritten")
    if args.handoff:
        reject_strict_paid_work_under_composite_policy(args.infra_root)
        handoff_gate, handoff = handoff_checks(args.infra_root, args.handoff)
        source_kind = "phase7-1-handoff"
        source_path = str(args.handoff.resolve())
    else:
        expected_ledger = (
            args.infra_root.resolve()
            / "performance-tests/phase7_2-stabilization/attempt-ledger.json"
        )
        if args.attempt_ledger is None or args.attempt_ledger.resolve() != expected_ledger:
            parser.error(
                "scoped diagnostic requires the exact campaign attempt ledger"
            )
        if args.fixture is not None:
            parser.error("scoped diagnostic production preflight forbids fixtures")
        handoff_gate, handoff = scoped_diagnostic_source_checks(
            args.infra_root, args.scoped_diagnostic_source
        )
        source_kind = "full-stack-scoped-diagnostic-source"
        source_path = str(args.scoped_diagnostic_source.resolve())
    snapshot = read_json(args.fixture) if args.fixture else AwsSnapshot().collect(
        args.run_id, args.session_id, args.x86_ami, args.arm_ami, args.certificate_arn
    )
    result = evaluate_preflight(
        snapshot, handoff, handoff_gate, read_json(args.prices), read_json(args.cost_model),
        args.run_id, args.session_id, args.image_state, args.x86_ami, args.arm_ami,
        args.protocol_dns_name, read_json(args.image_manifest) if args.image_manifest else None,
        source_kind, source_path,
        read_json(args.attempt_ledger) if args.attempt_ledger else None,
    )
    write_json(args.output, result)
    print(json.dumps({"generatedAt": result["generatedAt"], "passed": result["passed"], "checks": result["checks"]}, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

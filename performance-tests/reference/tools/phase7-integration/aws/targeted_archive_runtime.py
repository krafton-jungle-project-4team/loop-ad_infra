#!/usr/bin/env python3
"""Run one immutable targeted archive deployment, validation, and exact cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from targeted_archive_cleanup import cleanup, describe_stack, inventory, stack_resources
from targeted_archive_common import (
    DIGEST_PATTERN,
    EXPECTED_OPERATOR_ARN,
    EXPECTED_REGION,
    SDK_CONFIG,
    app_command,
    canonical_sha256,
    cdk_context,
    cdk_environment,
    expected_tags,
    file_sha256,
    git_identity,
    locked_session,
    repository_name,
    run,
    source_closure,
    stack_names,
    tag_map,
    tags_match,
    utc_now,
    validate_identifiers,
    write_json,
)


INSTANCE_TYPE = "r7g.2xlarge"
EXPECTED_ROWS = 15_000_000
ROWS_PER_PART = 5_000_000


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def check_deadline(paid_start: datetime, *, cleanup: bool = False) -> None:
    elapsed = (datetime.now(UTC) - paid_start).total_seconds()
    limit = 180 * 60 if cleanup else 160 * 60
    if elapsed >= limit:
        stage = "hard deadline" if cleanup else "cleanup-start deadline"
        raise RuntimeError(f"targeted attempt reached its {stage}")


def validate_image_manifest(
    manifest: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    validate_identifiers(args.run_id, args.session_id)
    if manifest.get("runId") != args.run_id or manifest.get("sessionId") != args.session_id:
        raise RuntimeError("image manifest identity mismatch")
    if manifest.get("platform") != "linux/arm64":
        raise RuntimeError("targeted image platform is not linux/arm64")
    if not DIGEST_PATTERN.fullmatch(str(manifest.get("digest", ""))):
        raise RuntimeError("targeted image manifest lacks an exact digest")
    if manifest.get("repository") != repository_name(args.run_id):
        raise RuntimeError("targeted image repository mismatch")
    identity = git_identity(args.infra_root)
    if manifest.get("implementation") != identity:
        raise RuntimeError("targeted implementation changed after image preparation")
    closure = source_closure(args.infra_root)
    if manifest.get("sourceClosure") != closure:
        raise RuntimeError("targeted image source closure changed after image preparation")


def validate_cost(cost: dict[str, Any]) -> None:
    if cost.get("passed") is not True:
        raise RuntimeError("targeted cost model did not pass")
    charge = Decimal(str(cost.get("chargedOperationalUpperBoundUsd")))
    modeled = Decimal(str(cost.get("modeledOperationalUpperBoundUsd")))
    active_prior = Decimal(str(cost.get("activeEpochPriorUpperBoundUsd")))
    strict = Decimal(str(cost.get("strictAttemptReservedUpperBoundUsd")))
    cleanup_reserve = Decimal(str(cost.get("cleanupReserveUsd")))
    maximum = Decimal(str(cost.get("maximumIncludingTargetedStrictAndCleanupUsd")))
    hard_cap = Decimal(str(cost.get("hardCapUsd")))
    if charge < modeled:
        raise RuntimeError("targeted cost charge does not cover the modeled upper bound")
    if active_prior != Decimal("0"):
        raise RuntimeError("targeted active-epoch prior is not the authorized post-Attempt-18 reset value")
    if maximum != active_prior + charge + strict + cleanup_reserve:
        raise RuntimeError("targeted cost identity is inconsistent")
    if maximum > hard_cap:
        raise RuntimeError("targeted cost model does not preserve the strict retry and cleanup reserve")
    checks = cost.get("checks")
    if not isinstance(checks, dict) or not checks or not all(checks.values()):
        raise RuntimeError("targeted cost checks are incomplete")


def prepared_preflight(
    session: Any,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    cost: dict[str, Any],
) -> dict[str, Any]:
    image_stack_name, runtime_stack_name = stack_names(args.session_id)
    cloudformation = session.client("cloudformation", config=SDK_CONFIG)
    image_stack = describe_stack(cloudformation, image_stack_name)
    runtime_stack = describe_stack(cloudformation, runtime_stack_name)
    observed = inventory(session, args.run_id, args.session_id)
    classes = observed["classes"]
    allowed_tagged_fragments = (image_stack_name, repository_name(args.run_id))
    unexpected_tagged = sorted({
        arn
        for key in ("taggingApiRunId", "taggingApiSessionId")
        for arn in classes[key]
        if not any(fragment in arn for fragment in allowed_tagged_fragments)
    })
    image_count = len(classes["ecrImages"])
    repository_count = len(classes["ecrRepositories"])
    runtime_classes = {
        key: value
        for key, value in classes.items()
        if key not in {
            "cloudformationStacks", "ecrRepositories", "ecrImages",
            "taggingApiRunId", "taggingApiSessionId",
        }
    }
    bootstrap = describe_stack(cloudformation, "CDKToolkit")
    ec2 = session.client("ec2", config=SDK_CONFIG)
    images = ec2.describe_images(ImageIds=[args.arm_ami]).get("Images", [])
    offerings = ec2.describe_instance_type_offerings(
        LocationType="region",
        Filters=[{"Name": "instance-type", "Values": [INSTANCE_TYPE]}],
    ).get("InstanceTypeOfferings", [])
    quotas = session.client("service-quotas", config=SDK_CONFIG)
    quota = quotas.get_service_quota(
        ServiceCode="ec2", QuotaCode="L-1216C47A"
    ).get("Quota", {})
    checks = {
        "identityExact": session.client("sts", config=SDK_CONFIG).get_caller_identity().get("Arn") == EXPECTED_OPERATOR_ARN,
        "regionExact": session.region_name == EXPECTED_REGION,
        "imageStackOwnedComplete": (
            image_stack is not None
            and image_stack.get("StackStatus") == "CREATE_COMPLETE"
            and tags_match(tag_map(image_stack.get("Tags", [])), args.run_id, args.session_id)
        ),
        "runtimeStackAbsent": runtime_stack is None,
        "oneRepositoryOneImage": repository_count == 1 and image_count == 1,
        "runtimeResourcesAbsent": all(not value for value in runtime_classes.values()),
        "noUnexpectedTaggedResource": not unexpected_tagged,
        "bootstrapReady": bootstrap is not None and str(bootstrap.get("StackStatus", "")).endswith("COMPLETE"),
        "armEcsAmiExact": (
            len(images) == 1
            and images[0].get("State") == "available"
            and images[0].get("Architecture") == "arm64"
            and images[0].get("OwnerId") == "591542846629"
        ),
        "instanceOfferingPresent": any(item.get("InstanceType") == INSTANCE_TYPE for item in offerings),
        "standardVpcQuotaSufficient": float(quota.get("Value", 0)) >= 8,
        "costGatePassed": cost.get("passed") is True,
        "pricesFreshWithinTwoHours": (
            isinstance(cost.get("priceAsOf"), str)
            and timedelta(0) <= datetime.now(UTC) - parse_timestamp(cost["priceAsOf"]) < timedelta(hours=2)
        ),
        "imageDigestExact": classes["ecrImages"][0].endswith("@" + manifest["digest"]) if image_count == 1 else False,
    }
    return {
        "schemaVersion": 1,
        "checkedAt": utc_now(),
        "runId": args.run_id,
        "sessionId": args.session_id,
        "checks": checks,
        "unexpectedTaggedResources": unexpected_tagged,
        "preparedInventory": observed,
        "quota": {"code": "L-1216C47A", "value": quota.get("Value")},
        "ami": images,
        "offerings": offerings,
        "passed": all(checks.values()),
    }


def decode_user_data_upper_bound(value: Any) -> int:
    encoded = value.get("Fn::Base64") if isinstance(value, dict) else None
    if isinstance(encoded, str):
        return len(encoded.encode())
    join = encoded.get("Fn::Join") if isinstance(encoded, dict) else None
    if not isinstance(join, list) or len(join) != 2 or not isinstance(join[0], str) or not isinstance(join[1], list):
        raise RuntimeError("unsupported targeted LaunchTemplate UserData shape")
    return sum(
        len(part.encode()) if isinstance(part, str) else 1024
        for part in join[1]
    ) + max(0, len(join[1]) - 1) * len(join[0].encode())


def validate_template(template: dict[str, Any], run_id: str, session_id: str) -> dict[str, Any]:
    resources = template.get("Resources")
    if not isinstance(resources, dict):
        raise RuntimeError("targeted synthesized template has no Resources object")
    counts = Counter(item.get("Type") for item in resources.values())
    exact = {
        "AWS::EC2::VPC": 1,
        "AWS::AutoScaling::AutoScalingGroup": 1,
        "AWS::EC2::LaunchTemplate": 1,
        "AWS::ECS::Cluster": 1,
        "AWS::ECS::CapacityProvider": 1,
        "AWS::ECS::ClusterCapacityProviderAssociations": 1,
        "AWS::ECS::Service": 1,
        "AWS::ECS::TaskDefinition": 3,
        "AWS::CloudFormation::WaitCondition": 1,
        "AWS::CloudFormation::WaitConditionHandle": 1,
        "AWS::S3::Bucket": 1,
        "AWS::SecretsManager::Secret": 1,
    }
    forbidden = {
        "AWS::EC2::NatGateway", "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::Kinesis::Stream", "AWS::DynamoDB::Table", "AWS::Lambda::Function",
        "AWS::Route53::RecordSet", "AWS::CertificateManager::Certificate",
    }
    exact_pass = all(counts[name] == value for name, value in exact.items())
    forbidden_pass = all(counts[name] == 0 for name in forbidden)
    launch_templates = [
        item for item in resources.values() if item.get("Type") == "AWS::EC2::LaunchTemplate"
    ]
    user_data_bytes = decode_user_data_upper_bound(
        launch_templates[0]["Properties"]["LaunchTemplateData"]["UserData"]
    ) if len(launch_templates) == 1 else 10**9
    user_data_text = json.dumps(
        launch_templates[0]["Properties"]["LaunchTemplateData"]["UserData"]
    ) if len(launch_templates) == 1 else ""
    aws_cli_bootstrap = user_data_text.find("yum install -y awscli")
    readiness_signal = user_data_text.find("aws cloudformation signal-resource")
    registration_check = user_data_text.find("aws ecs describe-container-instances")
    entries = list(resources.items())
    services = [
        (logical_id, item)
        for logical_id, item in entries
        if item.get("Type") == "AWS::ECS::Service"
    ]
    associations = [
        (logical_id, item)
        for logical_id, item in entries
        if item.get("Type") == "AWS::ECS::ClusterCapacityProviderAssociations"
    ]
    wait_conditions = [
        (logical_id, item)
        for logical_id, item in entries
        if item.get("Type") == "AWS::CloudFormation::WaitCondition"
    ]
    auto_scaling_groups = [
        item for item in resources.values()
        if item.get("Type") == "AWS::AutoScaling::AutoScalingGroup"
    ]
    service_dependencies = (
        services[0][1].get("DependsOn", []) if len(services) == 1 else []
    )
    if isinstance(service_dependencies, str):
        service_dependencies = [service_dependencies]
    wait_dependencies = (
        wait_conditions[0][1].get("DependsOn", [])
        if len(wait_conditions) == 1 else []
    )
    if isinstance(wait_dependencies, str):
        wait_dependencies = [wait_dependencies]
    registration_dependency_exact = (
        len(services) == 1
        and len(associations) == 1
        and len(wait_conditions) == 1
        and wait_conditions[0][0] in service_dependencies
        and associations[0][0] in service_dependencies
        and associations[0][0] in wait_dependencies
    )
    asg_creation_signal_exact = (
        len(auto_scaling_groups) == 1
        and auto_scaling_groups[0].get("CreationPolicy", {}).get("ResourceSignal")
        == {"Count": 1, "Timeout": "PT10M"}
    )
    text = json.dumps(template, sort_keys=True)
    checks = {
        "exactResourceCounts": exact_pass,
        "forbiddenResourcesAbsent": forbidden_pass,
        "userDataBelowEc2Limit": user_data_bytes <= 16_384,
        "userDataKeepsMargin": user_data_bytes <= 15_360,
        "blockingEcsRestartAbsent": "systemctl restart ecs" not in text,
        "awsCliBootstrapBeforeSignals": (
            aws_cli_bootstrap >= 0
            and readiness_signal > aws_cli_bootstrap
            and registration_check > aws_cli_bootstrap
            and "command -v aws >/dev/null 2>&1" in user_data_text
        ),
        "ecsReadinessSignalsPresent": all(marker in text for marker in (
            "http://127.0.0.1:51678/v1/metadata",
            "ecs describe-container-instances",
            "agentConnected,status,capacityProviderName",
            "signal_wait_condition SUCCESS",
        )),
        "asgCreationSignalExact": asg_creation_signal_exact,
        "serviceWaitsForExactRegistration": registration_dependency_exact,
        "sourceDeletePermissionAbsent": "s3:DeleteObject" not in text,
        "diagnosticRetentionEnabled": "ARCHIVE_RETAIN_SOURCE_AFTER_COMMIT" in text and "true" in text,
        "promotionDisabled": "PromotionEligible" in text and "false" in text,
        "runIdentityPresent": run_id in text and session_id in text,
    }
    return {
        "schemaVersion": 1,
        "checkedAt": utc_now(),
        "resourceCounts": dict(sorted((str(key), value) for key, value in counts.items() if key)),
        "requiredExactCounts": exact,
        "forbiddenTypes": sorted(forbidden),
        "decodedUserDataByteUpperBound": user_data_bytes,
        "checks": checks,
        "passed": all(checks.values()),
    }


def synthesize(args: argparse.Namespace, manifest: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    output = args.readiness_dir / "runtime-cdk.out"
    output.mkdir(parents=True, exist_ok=False)
    cdk = args.infra_root / "node_modules/.bin/cdk"
    command = [
        str(cdk), "--app", app_command(args.infra_root),
        *cdk_context(args.run_id, args.session_id, manifest["digest"], args.arm_ami),
        "synth", "LoopAdPerfPhase7ArchiveDiagnosticStack", "--exclusively",
        "--output", str(output),
    ]
    run(command, args.infra_root, env=cdk_environment())
    candidates = list(output.glob("*ArchiveDiagnosticStack.template.json"))
    if len(candidates) != 1:
        candidates = list(output.glob("*.template.json"))
    if len(candidates) != 1:
        raise RuntimeError("exact targeted runtime template could not be selected")
    template = json.loads(candidates[0].read_text(encoding="utf-8"))
    return candidates[0], template


def run_cfn_lint(template_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    command = [
        "uvx", "--from", "cfn-lint==1.53.0", "cfn-lint",
        "-t", str(template_path), "-r", EXPECTED_REGION, "-f", "json",
    ]
    raw = run(
        command,
        args.infra_root,
        capture=True,
        allowed_codes=(0, 2, 4, 6),
        env={**os.environ, "UV_CACHE_DIR": "/tmp/loopad-phase7-uv-cache"},
    )
    findings = json.loads(raw) if raw else []
    if not isinstance(findings, list):
        raise RuntimeError("cfn-lint did not return a JSON finding list")
    accepted = {"E1022", "W3005"}
    unexpected = [item for item in findings if item.get("Rule", {}).get("Id") not in accepted]
    result = {
        "schemaVersion": 1,
        "checkedAt": utc_now(),
        "version": "1.53.0",
        "findings": findings,
        "acceptedAssemblyOnlyRules": sorted(accepted),
        "unexpectedFindings": unexpected,
        "passed": not unexpected,
    }
    write_json(args.readiness_dir / "cfn-lint.json", result)
    return result


def run_local_diff(args: argparse.Namespace, manifest: dict[str, Any]) -> dict[str, Any]:
    cdk = args.infra_root / "node_modules/.bin/cdk"
    command = [
        str(cdk), "--app", app_command(args.infra_root),
        *cdk_context(args.run_id, args.session_id, manifest["digest"], args.arm_ami),
        "diff", "LoopAdPerfPhase7ArchiveDiagnosticStack", "--exclusively",
        "--no-change-set",
    ]
    output = run(
        command,
        args.infra_root,
        capture=True,
        allowed_codes=(0, 1),
        env=cdk_environment(),
    )
    (args.readiness_dir / "cdk-diff.txt").write_text(output + "\n", encoding="utf-8")
    forbidden = ("Replacement", "AWS::Route53", "AWS::CertificateManager", "dev", "prod")
    result = {
        "schemaVersion": 1,
        "checkedAt": utc_now(),
        "runtimeStackWasAbsent": True,
        "changeSetUsed": False,
        "forbiddenMarkers": [marker for marker in forbidden if marker in output],
    }
    result["passed"] = not result["forbiddenMarkers"]
    write_json(args.readiness_dir / "cdk-diff-summary.json", result)
    return result


def deployment_outputs(stack: dict[str, Any]) -> dict[str, str]:
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
    required = {
        "ArchiveBucketName", "ArchiveClusterName", "ArchiveCapacityProviderName",
        "ClickHouseServiceName", "ClickHouseAutoScalingGroupName",
        "SeedTaskDefinitionArn", "ArchiveTaskDefinitionArn",
        "WorkerSecurityGroupId", "WorkerSubnetId", "ArchiveImageDigest",
        "ClickHouseContainerMemoryMiB", "ClickHouseServerMemoryBytes", "ArchiveQueryMemoryBytes",
    }
    missing = sorted(required.difference(outputs))
    if missing:
        raise RuntimeError(f"targeted runtime outputs are missing: {', '.join(missing)}")
    return outputs


def verify_deployment(
    session: Any,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    stack: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    outputs = deployment_outputs(stack)
    cloudformation = session.client("cloudformation", config=SDK_CONFIG)
    resources = stack_resources(cloudformation, stack["StackName"])
    resource_counts = Counter(item.get("ResourceType") for item in resources)
    autoscaling = session.client("autoscaling", config=SDK_CONFIG)
    groups = autoscaling.describe_auto_scaling_groups(
        AutoScalingGroupNames=[outputs["ClickHouseAutoScalingGroupName"]]
    ).get("AutoScalingGroups", [])
    if len(groups) != 1 or not tags_match(tag_map(groups[0].get("Tags", [])), args.run_id, args.session_id):
        raise RuntimeError("targeted Auto Scaling group is not exactly owned")
    instance_ids = [item["InstanceId"] for item in groups[0].get("Instances", [])]
    if len(instance_ids) != 1:
        raise RuntimeError("targeted Auto Scaling group must contain exactly one instance")
    ec2 = session.client("ec2", config=SDK_CONFIG)
    reservations = ec2.describe_instances(InstanceIds=instance_ids).get("Reservations", [])
    instances = [item for reservation in reservations for item in reservation.get("Instances", [])]
    if len(instances) != 1:
        raise RuntimeError("targeted EC2 host cardinality is not exact")
    instance = instances[0]
    root_mapping = next(
        item for item in instance.get("BlockDeviceMappings", [])
        if item.get("DeviceName") == instance.get("RootDeviceName")
    )
    volumes = ec2.describe_volumes(VolumeIds=[root_mapping["Ebs"]["VolumeId"]]).get("Volumes", [])
    volume = volumes[0] if len(volumes) == 1 else {}

    ecs = session.client("ecs", config=SDK_CONFIG)
    ecs.get_waiter("services_stable").wait(
        cluster=outputs["ArchiveClusterName"],
        services=[outputs["ClickHouseServiceName"]],
        WaiterConfig={"Delay": 10, "MaxAttempts": 90},
    )
    services = ecs.describe_services(
        cluster=outputs["ArchiveClusterName"],
        services=[outputs["ClickHouseServiceName"]],
        include=["TAGS"],
    ).get("services", [])
    if len(services) != 1:
        raise RuntimeError("targeted ClickHouse service cardinality is not exact")
    service = services[0]
    task_arns = ecs.list_tasks(
        cluster=outputs["ArchiveClusterName"],
        serviceName=outputs["ClickHouseServiceName"],
        desiredStatus="RUNNING",
    ).get("taskArns", [])
    if len(task_arns) != 1:
        raise RuntimeError("targeted ClickHouse running task cardinality is not exact")
    tasks = ecs.describe_tasks(
        cluster=outputs["ArchiveClusterName"], tasks=task_arns, include=["TAGS"]
    ).get("tasks", [])
    if len(tasks) != 1:
        raise RuntimeError("targeted ClickHouse task description is not exact")
    clickhouse_task = tasks[0]
    eni_id = next(
        detail["value"]
        for attachment in clickhouse_task.get("attachments", [])
        for detail in attachment.get("details", [])
        if detail.get("name") == "networkInterfaceId"
    )
    enis = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id]).get("NetworkInterfaces", [])
    if len(enis) != 1:
        raise RuntimeError("targeted ClickHouse task ENI is missing")
    clickhouse_ip = enis[0]["PrivateIpAddress"]
    container_instances = ecs.list_container_instances(
        cluster=outputs["ArchiveClusterName"], status="ACTIVE"
    ).get("containerInstanceArns", [])
    described_container_instances = ecs.describe_container_instances(
        cluster=outputs["ArchiveClusterName"], containerInstances=container_instances
    ).get("containerInstances", []) if container_instances else []

    service_strategy = service.get("capacityProviderStrategy", [])
    container_by_name = {item.get("name"): item for item in clickhouse_task.get("containers", [])}
    checks = {
        "stackCreateComplete": stack.get("StackStatus") == "CREATE_COMPLETE",
        "stackTagsExact": tags_match(tag_map(stack.get("Tags", [])), args.run_id, args.session_id),
        "oneRuntimeDeploy": True,
        "oneHost": len(instance_ids) == 1,
        "hostTypeExact": instance.get("InstanceType") == INSTANCE_TYPE,
        "hostAmiExact": instance.get("ImageId") == args.arm_ami,
        "hostTagsExact": tags_match(tag_map(instance.get("Tags", [])), args.run_id, args.session_id),
        "volumeExact": (
            volume.get("Size") == 500 and volume.get("VolumeType") == "gp3"
            and volume.get("Iops") == 3000 and volume.get("Throughput") == 500
            and volume.get("Encrypted") is True
        ),
        "serviceReadyExact": service.get("desiredCount") == 1 and service.get("runningCount") == 1 and service.get("pendingCount") == 0,
        "capacityProviderExact": len(service_strategy) == 1 and service_strategy[0].get("capacityProvider") == outputs["ArchiveCapacityProviderName"],
        "oneConnectedContainerInstance": (
            len(described_container_instances) == 1
            and described_container_instances[0].get("agentConnected") is True
            and described_container_instances[0].get("ec2InstanceId") == instance_ids[0]
        ),
        "clickHouseHealthy": container_by_name.get("clickhouse", {}).get("healthStatus") == "HEALTHY",
        "schemaGuardHealthy": container_by_name.get("schema-guard", {}).get("healthStatus") == "HEALTHY",
        "imageDigestExact": outputs["ArchiveImageDigest"] == manifest["digest"],
        "containerMemoryExact": outputs["ClickHouseContainerMemoryMiB"] == "8192",
        "serverMemoryExact": outputs["ClickHouseServerMemoryBytes"] == str(7 * 1024**3),
        "archiveQueryMemoryExact": outputs["ArchiveQueryMemoryBytes"] == str(5 * 1024**3),
        "resourceCountsExpected": (
            resource_counts["AWS::EC2::LaunchTemplate"] == 1
            and resource_counts["AWS::AutoScaling::AutoScalingGroup"] == 1
            and resource_counts["AWS::ECS::Service"] == 1
            and resource_counts["AWS::ECS::TaskDefinition"] == 3
        ),
    }
    evidence = {
        "schemaVersion": 1,
        "verifiedAt": utc_now(),
        "stack": {"name": stack["StackName"], "status": stack["StackStatus"], "tags": tag_map(stack.get("Tags", []))},
        "outputs": outputs,
        "resourceCounts": dict(sorted((str(key), value) for key, value in resource_counts.items() if key)),
        "autoScalingGroup": groups[0],
        "instance": {
            "instanceId": instance["InstanceId"], "instanceType": instance.get("InstanceType"),
            "imageId": instance.get("ImageId"), "state": instance.get("State"),
            "tags": tag_map(instance.get("Tags", [])),
        },
        "volume": {key: volume.get(key) for key in ("VolumeId", "Size", "VolumeType", "Iops", "Throughput", "Encrypted", "State")},
        "service": service,
        "clickHouseTask": clickhouse_task,
        "containerInstances": described_container_instances,
        "clickHousePrivateIp": clickhouse_ip,
        "checks": checks,
        "passed": all(checks.values()),
    }
    return evidence, clickhouse_ip


def run_task(
    ecs: Any,
    outputs: dict[str, str],
    args: argparse.Namespace,
    task_definition: str,
    container: str,
    action: str,
    clickhouse_ip: str,
    partition: str,
    today: str,
) -> dict[str, Any]:
    response = ecs.run_task(
        cluster=outputs["ArchiveClusterName"],
        taskDefinition=task_definition,
        count=1,
        capacityProviderStrategy=[{
            "capacityProvider": outputs["ArchiveCapacityProviderName"],
            "weight": 1,
            "base": 0,
        }],
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": [outputs["WorkerSubnetId"]],
            "securityGroups": [outputs["WorkerSecurityGroupId"]],
            "assignPublicIp": "DISABLED",
        }},
        overrides={"containerOverrides": [{
            "name": container,
            "environment": [
                {"name": "CLICKHOUSE_HTTP_URL", "value": f"http://{clickhouse_ip}:8123"},
                {"name": "ARCHIVE_PARTITION", "value": partition},
                {"name": "ARCHIVE_TODAY", "value": today},
                {"name": "TARGETED_ACTION", "value": action},
            ],
        }]},
        tags=[{"key": key, "value": value} for key, value in expected_tags(args.run_id, args.session_id).items()],
        enableECSManagedTags=False,
        startedBy=f"p7diag-{action}-{args.session_id[-15:-1]}",
    )
    failures = response.get("failures", [])
    tasks = response.get("tasks", [])
    if failures or len(tasks) != 1:
        raise RuntimeError(f"targeted {action} task did not start exactly once: {failures}")
    task_arn = tasks[0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(
        cluster=outputs["ArchiveClusterName"],
        tasks=[task_arn],
        WaiterConfig={"Delay": 15, "MaxAttempts": 160},
    )
    described = ecs.describe_tasks(
        cluster=outputs["ArchiveClusterName"], tasks=[task_arn], include=["TAGS"]
    ).get("tasks", [])
    if len(described) != 1:
        raise RuntimeError(f"targeted {action} task description is missing")
    task = described[0]
    target = next((item for item in task.get("containers", []) if item.get("name") == container), None)
    if target is None:
        raise RuntimeError(f"targeted {action} container result is missing")
    return {
        "action": action,
        "taskArn": task_arn,
        "lastStatus": task.get("lastStatus"),
        "stopCode": task.get("stopCode"),
        "stoppedReason": task.get("stoppedReason"),
        "createdAt": task.get("createdAt").isoformat() if task.get("createdAt") else None,
        "startedAt": task.get("startedAt").isoformat() if task.get("startedAt") else None,
        "stoppedAt": task.get("stoppedAt").isoformat() if task.get("stoppedAt") else None,
        "exitCode": target.get("exitCode"),
        "reason": target.get("reason"),
        "tags": tag_map(task.get("tags", [])),
        "passed": target.get("exitCode") == 0 and task.get("stopCode") == "EssentialContainerExited",
    }


def get_json_object(s3: Any, bucket: str, key: str, destination: Path) -> tuple[dict[str, Any], bytes]:
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    value = json.loads(body)
    if not isinstance(value, dict):
        raise RuntimeError(f"S3 object is not one JSON object: {key}")
    return value, body


def equivalence_passed(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("passed") is True
        and value.get("expectedMetrics", {}).get("rows") == EXPECTED_ROWS
        and value.get("archiveMetrics", {}).get("rows") == EXPECTED_ROWS
        and len(value.get("twoWayDifferences", [])) == 3
        and all(
            item.get("leftMinusRight") == 0 and item.get("rightMinusLeft") == 0
            for item in value.get("twoWayDifferences", [])
        )
    )


def validate_archive(
    s3: Any,
    bucket: str,
    args: argparse.Namespace,
    partition: str,
) -> dict[str, Any]:
    result_key = (
        f"attempts/v1/table=events/event_date={partition}/"
        f"phase7-result-{args.run_id}.json"
    )
    result, result_body = get_json_object(
        s3, bucket, result_key, args.runtime_dir / "archive-result.json"
    )
    result_reread, result_body_reread = get_json_object(
        s3, bucket, result_key, args.runtime_dir / "archive-result-reread.json"
    )
    commit_key = f"commits/v1/table=events/event_date={partition}/COMMITTED"
    commit_1, commit_body_1 = get_json_object(
        s3, bucket, commit_key, args.runtime_dir / "committed-reread-1.json"
    )
    commit_2, commit_body_2 = get_json_object(
        s3, bucket, commit_key, args.runtime_dir / "committed-reread-2.json"
    )
    manifest_key = str(commit_1.get("manifestKey", ""))
    manifest_1, manifest_body_1 = get_json_object(
        s3, bucket, manifest_key, args.runtime_dir / "manifest-reread-1.json"
    )
    manifest_2, manifest_body_2 = get_json_object(
        s3, bucket, manifest_key, args.runtime_dir / "manifest-reread-2.json"
    )
    manifest_sha = hashlib.sha256(manifest_body_1).hexdigest()
    parts = result.get("parts", [])
    part_heads = []
    for part in parts if isinstance(parts, list) else []:
        head = s3.head_object(Bucket=bucket, Key=part["key"])
        part_heads.append({
            "key": part["key"],
            "rows": part.get("rows"),
            "bytes": part.get("bytes"),
            "contentLength": head.get("ContentLength"),
            "sha256": part.get("sha256"),
            "metadataSha256": head.get("Metadata", {}).get("sha256"),
            "storageClass": head.get("StorageClass", "STANDARD"),
        })
    listed = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        listed.extend({"key": item["Key"], "size": item["Size"]} for item in page.get("Contents", []))
    checks = {
        "resultPassed": result.get("status") == "passed" and result.get("runId") == args.run_id,
        "newAttemptPath": result.get("recoveryState") == "NEW_ATTEMPT",
        "diagnosticSourceRetained": result.get("diagnosticSourceRetention") is True,
        "dropNotExecuted": result.get("dropExecuted") is False and result.get("postDrop") is None,
        "sourceRowsRetained": result.get("sourceRowsAfter") == EXPECTED_ROWS,
        "threePartsExact": (
            isinstance(parts, list) and len(parts) == 3
            and [item.get("rows") for item in parts] == [ROWS_PER_PART] * 3
        ),
        "partObjectsExact": (
            len(part_heads) == 3
            and all(
                item["bytes"] == item["contentLength"]
                and item["sha256"] == item["metadataSha256"]
                and item["storageClass"] == "STANDARD"
                for item in part_heads
            )
        ),
        "preDropEquivalent": equivalence_passed(result.get("preDrop")),
        "committedPreDropEquivalent": equivalence_passed(result.get("committedPreDrop")),
        "commitRereadImmutable": commit_body_1 == commit_body_2 and commit_1 == commit_2,
        "manifestRereadImmutable": manifest_body_1 == manifest_body_2 and manifest_1 == manifest_2,
        "commitManifestHashExact": (
            commit_1.get("manifestSha256") == manifest_sha
            and result.get("manifestSha256") == manifest_sha
        ),
        "commitAndResultIdentityExact": (
            commit_1.get("runId") == args.run_id
            and commit_1.get("archiveId") == result.get("archiveId")
            and manifest_1.get("archiveId") == result.get("archiveId")
            and manifest_1.get("parts") == parts
        ),
        "resultObjectImmutable": result_body == result_body_reread and result == result_reread,
    }
    return {
        "schemaVersion": 1,
        "validatedAt": utc_now(),
        "runId": args.run_id,
        "partition": partition,
        "resultKey": result_key,
        "commitKey": commit_key,
        "manifestKey": manifest_key,
        "manifestSha256": manifest_sha,
        "partHeads": part_heads,
        "objects": sorted(listed, key=lambda item: item["key"]),
        "result": result,
        "commit": commit_1,
        "manifest": manifest_1,
        "checks": checks,
        "passed": all(checks.values()),
    }


def collect_logs(session: Any, args: argparse.Namespace) -> dict[str, Any]:
    logs = session.client("logs", config=SDK_CONFIG)
    prefix = f"/loopad/perf/phase7-targeted/{args.run_id}/"
    groups = []
    for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
        groups.extend(
            item["logGroupName"] for item in page.get("logGroups", [])
            if item.get("logGroupName", "").startswith(prefix)
        )
    evidence = []
    for group in sorted(groups):
        streams = []
        for page in logs.get_paginator("describe_log_streams").paginate(
            logGroupName=group,
            orderBy="LastEventTime",
            descending=True,
        ):
            streams.extend(page.get("logStreams", []))
        events = []
        for stream in streams:
            for page in logs.get_paginator("get_log_events").paginate(
                logGroupName=group,
                logStreamName=stream["logStreamName"],
                startFromHead=True,
            ):
                events.extend(page.get("events", []))
        evidence.append({"logGroup": group, "streams": streams, "events": events})
    result = {"schemaVersion": 1, "collectedAt": utc_now(), "logGroups": evidence}
    write_json(args.runtime_dir / "cloudwatch-logs.json", result)
    return result


def collect_cloudtrail(session: Any, args: argparse.Namespace, paid_start: datetime) -> dict[str, Any]:
    cloudtrail = session.client("cloudtrail", config=SDK_CONFIG)
    selected = []
    next_token = None
    while True:
        parameters: dict[str, Any] = {
            "StartTime": paid_start - timedelta(minutes=5),
            "EndTime": datetime.now(UTC) + timedelta(minutes=1),
            "MaxResults": 50,
            "LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": "RunTask"}],
        }
        if next_token:
            parameters["NextToken"] = next_token
        response = cloudtrail.lookup_events(**parameters)
        for event in response.get("Events", []):
            raw = event.get("CloudTrailEvent", "")
            if args.run_id in raw or args.session_id in raw:
                selected.append({
                    "EventId": event.get("EventId"),
                    "EventName": event.get("EventName"),
                    "EventTime": event.get("EventTime"),
                    "EventSource": event.get("EventSource"),
                    "Username": event.get("Username"),
                    "ReadOnly": event.get("ReadOnly"),
                    "Resources": event.get("Resources", []),
                })
        next_token = response.get("NextToken")
        if not next_token:
            break
    counts = Counter(item.get("EventName") for item in selected)
    result = {
        "schemaVersion": 1,
        "collectedAt": utc_now(),
        "eventCounts": dict(sorted((str(key), value) for key, value in counts.items() if key)),
        "events": selected,
        "rawCloudTrailEventPersisted": False,
    }
    write_json(args.runtime_dir / "cloudtrail-events.json", result)
    return result


def execute(args: argparse.Namespace) -> dict[str, Any]:
    args.infra_root = args.infra_root.resolve()
    args.readiness_dir = args.readiness_dir.resolve()
    args.runtime_dir = args.runtime_dir.resolve()
    if not args.readiness_dir.is_dir() or not args.runtime_dir.is_dir():
        raise RuntimeError("fresh targeted evidence directories must be created before execution")
    manifest = read_object(args.image_manifest)
    cost = read_object(args.cost_model)
    validate_image_manifest(manifest, args)
    validate_cost(cost)
    paid_start = parse_timestamp(manifest["preparedAt"])
    check_deadline(paid_start)
    session = locked_session()

    preflight = prepared_preflight(session, args, manifest, cost)
    write_json(args.readiness_dir / "preflight-prepared.json", preflight)
    if not preflight["passed"]:
        raise RuntimeError("targeted prepared preflight failed")
    template_path, template = synthesize(args, manifest)
    template_gate = validate_template(template, args.run_id, args.session_id)
    template_gate["templatePath"] = str(template_path)
    template_gate["templateSha256"] = file_sha256(template_path)
    write_json(args.readiness_dir / "template-validation.json", template_gate)
    if not template_gate["passed"]:
        raise RuntimeError("targeted synthesized template validation failed")
    lint = run_cfn_lint(template_path, args)
    if not lint["passed"]:
        raise RuntimeError("targeted cfn-lint validation failed")
    diff = run_local_diff(args, manifest)
    if not diff["passed"]:
        raise RuntimeError("targeted local CDK diff contained a forbidden marker")
    check_deadline(paid_start)

    command_set = {
        "schemaVersion": 1,
        "runId": args.run_id,
        "sessionId": args.session_id,
        "imagePreparationAttempts": 1,
        "runtimeDeployAttempts": 1,
        "seedAttempts": 1,
        "archiveAttempts": 1,
        "verifyAttempts": 1,
        "cleanupAttempts": 1,
        "archiveImageDigest": manifest["digest"],
        "armEcsAmiId": args.arm_ami,
        "sourceClosureSha256": manifest["sourceClosure"]["sha256"],
        "implementationCommit": manifest["implementation"]["commit"],
        "templateSha256": template_gate["templateSha256"],
    }
    command_set["sha256"] = canonical_sha256(command_set)
    write_json(args.runtime_dir / "command-set.json", command_set)

    cdk = args.infra_root / "node_modules/.bin/cdk"
    deploy_command = [
        str(cdk), "--app", app_command(args.infra_root),
        *cdk_context(args.run_id, args.session_id, manifest["digest"], args.arm_ami),
        "deploy", "LoopAdPerfPhase7ArchiveDiagnosticStack", "--exclusively",
        "--require-approval", "never", "--concurrency", "1",
    ]
    deployment_started_at = utc_now()
    run(deploy_command, args.infra_root, env=cdk_environment())
    deployment_finished_at = utc_now()
    image_stack_name, runtime_stack_name = stack_names(args.session_id)
    cloudformation = session.client("cloudformation", config=SDK_CONFIG)
    stack = describe_stack(cloudformation, runtime_stack_name)
    if stack is None:
        raise RuntimeError("targeted runtime stack is absent after its single deploy command")
    deployment, clickhouse_ip = verify_deployment(session, args, manifest, stack)
    deployment.update({
        "deployAttempts": 1,
        "deploymentStartedAt": deployment_started_at,
        "deploymentFinishedAt": deployment_finished_at,
        "imageStackName": image_stack_name,
    })
    write_json(args.runtime_dir / "deployment-verification.json", deployment)
    if not deployment["passed"]:
        raise RuntimeError("targeted deployment verification failed")

    outputs = deployment["outputs"]
    utc_today = datetime.now(UTC).date()
    today = utc_today.isoformat()
    partition = (utc_today - timedelta(days=8)).isoformat()
    ecs = session.client("ecs", config=SDK_CONFIG)
    s3 = session.client("s3", config=SDK_CONFIG)
    check_deadline(paid_start)
    seed_task = run_task(
        ecs, outputs, args, outputs["SeedTaskDefinitionArn"], "seed", "seed",
        clickhouse_ip, partition, today,
    )
    write_json(args.runtime_dir / "seed-task.json", seed_task)
    seed_result, _ = get_json_object(
        s3,
        outputs["ArchiveBucketName"],
        f"diagnostics/{args.run_id}/seed.json",
        args.runtime_dir / "seed-result.json",
    )
    seed_passed = (
        seed_task["passed"]
        and seed_result.get("status") == "passed"
        and seed_result.get("rows") == EXPECTED_ROWS
        and seed_result.get("uniqueEvents") == EXPECTED_ROWS
    )
    if not seed_passed:
        raise RuntimeError("targeted 15M seed failed")

    check_deadline(paid_start)
    archive_task = run_task(
        ecs, outputs, args, outputs["ArchiveTaskDefinitionArn"], "archive", "archive",
        clickhouse_ip, partition, today,
    )
    write_json(args.runtime_dir / "archive-task.json", archive_task)
    archive_validation = None
    archive_error = None
    if archive_task["passed"]:
        try:
            archive_validation = validate_archive(
                s3, outputs["ArchiveBucketName"], args, partition
            )
            write_json(args.runtime_dir / "archive-validation.json", archive_validation)
        except Exception as error:
            archive_error = f"{type(error).__name__}: {error}"

    check_deadline(paid_start)
    verify_task = run_task(
        ecs, outputs, args, outputs["SeedTaskDefinitionArn"], "seed", "verify",
        clickhouse_ip, partition, today,
    )
    write_json(args.runtime_dir / "verify-task.json", verify_task)
    verify_result = None
    try:
        verify_result, _ = get_json_object(
            s3,
            outputs["ArchiveBucketName"],
            f"diagnostics/{args.run_id}/verify.json",
            args.runtime_dir / "verify-result.json",
        )
    except Exception as error:
        archive_error = archive_error or f"{type(error).__name__}: {error}"
    verify_passed = (
        verify_task["passed"]
        and isinstance(verify_result, dict)
        and verify_result.get("status") == "passed"
        and verify_result.get("sourceRowsAfter") == EXPECTED_ROWS
        and verify_result.get("sourceUniqueEventsAfter") == EXPECTED_ROWS
        and verify_result.get("code241Exceptions") == 0
        and verify_result.get("sourceDropQueries") == 0
    )

    final_service = verify_deployment(session, args, manifest, stack)[0]
    final_ecs = session.client("ecs", config=SDK_CONFIG)
    final_service["stoppedStandaloneTasks"] = final_ecs.list_tasks(
        cluster=outputs["ArchiveClusterName"], desiredStatus="STOPPED"
    ).get("taskArns", [])
    final_service["stoppedServiceTasks"] = final_ecs.list_tasks(
        cluster=outputs["ArchiveClusterName"],
        serviceName=outputs["ClickHouseServiceName"],
        desiredStatus="STOPPED",
    ).get("taskArns", [])
    final_service["unexpectedServiceRestart"] = (
        len(final_service.get("service", {}).get("deployments", [])) != 1
        or bool(final_service["stoppedServiceTasks"])
    )
    write_json(args.runtime_dir / "post-archive-runtime-verification.json", final_service)
    logs = collect_logs(session, args)
    cloudtrail = collect_cloudtrail(session, args, paid_start)
    stage_passed = (
        archive_task["passed"]
        and archive_validation is not None
        and archive_validation["passed"]
        and verify_passed
        and final_service["passed"]
        and not final_service["unexpectedServiceRestart"]
    )
    if not stage_passed:
        raise RuntimeError(
            archive_error or "targeted archive acceptance failed"
        )
    return {
        "schemaVersion": 1,
        "runId": args.run_id,
        "sessionId": args.session_id,
        "attemptType": "aws-targeted-diagnostic",
        "promotionEligible": False,
        "phase5": "skipped",
        "paidStartAt": manifest["preparedAt"],
        "stages": {
            "imagePreparation": True,
            "runtimeDeploy": deployment["passed"],
            "seed15M": seed_passed,
            "archive": archive_validation["passed"],
            "postArchiveVerification": verify_passed,
            "logsCollected": bool(logs.get("logGroups")),
            "cloudTrailCollected": bool(cloudtrail.get("events")),
        },
        "taskAttempts": {"seed": 1, "archive": 1, "verify": 1},
        "commandSetSha256": command_set["sha256"],
        "chargedOperationalUpperBoundUsd": cost["chargedOperationalUpperBoundUsd"],
        "passedBeforeCleanup": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--arm-ami", required=True)
    parser.add_argument("--image-manifest", required=True, type=Path)
    parser.add_argument("--cost-model", required=True, type=Path)
    parser.add_argument("--readiness-dir", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        charged_upper_bound = str(
            read_object(args.cost_model).get("chargedOperationalUpperBoundUsd")
        )
    except Exception:
        charged_upper_bound = "unavailable"
    result = None
    failure = None
    cleanup_result = None
    try:
        result = execute(args)
        write_json(args.runtime_dir / "execution-result-before-cleanup.json", result)
    except KeyboardInterrupt:
        failure = {
            "schemaVersion": 1,
            "failedAt": utc_now(),
            "errorType": "KeyboardInterrupt",
            "error": "operator interrupted the active targeted attempt",
            "interrupted": True,
        }
        write_json(args.runtime_dir / "failure.json", failure)
    except Exception as error:
        failure = {
            "schemaVersion": 1,
            "failedAt": utc_now(),
            "errorType": type(error).__name__,
            "error": str(error),
        }
        write_json(args.runtime_dir / "failure.json", failure)
    finally:
        cleanup_started = utc_now()
        try:
            cleanup_result = cleanup(locked_session(), args.run_id, args.session_id)
            write_json(args.runtime_dir / "cleanup-verification.json", cleanup_result)
        except Exception as error:
            if failure is None:
                failure = {
                    "schemaVersion": 1,
                    "failedAt": utc_now(),
                    "errorType": type(error).__name__,
                    "error": str(error),
                }
            failure["cleanupErrorType"] = type(error).__name__
            failure["cleanupError"] = str(error)
            write_json(args.runtime_dir / "failure.json", failure)

    passed = result is not None and failure is None and cleanup_result is not None and cleanup_result.get("passed") is True
    verdict = "passed" if passed else "failed"
    report = {
        "schemaVersion": 1,
        "runId": args.run_id,
        "sessionId": args.session_id,
        "attemptType": "aws-targeted-diagnostic",
        "promotionEligible": False,
        "verdict": verdict,
        "phase5": "skipped",
        "execution": result,
        "failure": failure,
        "cleanupStartedAt": cleanup_started,
        "cleanupPassed": cleanup_result.get("passed") if cleanup_result else False,
        "chargedOperationalUpperBoundUsd": charged_upper_bound,
        "finishedAt": utc_now(),
    }
    write_json(args.runtime_dir / "report.json", report)
    (args.runtime_dir / "report.md").write_text(
        "# Phase 7 Targeted Archive Diagnostic\n\n"
        f"- Verdict: `{verdict}`\n"
        f"- Run ID: `{args.run_id}`\n"
        f"- Runtime deployment attempts: `1`\n"
        f"- Archive attempts: `1`\n"
        f"- Source DROP: `not executed`\n"
        f"- Cleanup authoritative zero: `{str(bool(cleanup_result and cleanup_result.get('passed'))).lower()}`\n"
        f"- Charged upper bound: `${charged_upper_bound}`\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())

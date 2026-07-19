#!/usr/bin/env python3
"""Exact-scope cleanup and authoritative inventory for targeted archive attempts."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError, WaiterError

from targeted_archive_common import (
    EXPECTED_ACCOUNT,
    EXPECTED_REGION,
    SDK_CONFIG,
    expected_tags,
    locked_session,
    repository_name,
    stack_names,
    tag_map,
    tags_match,
    utc_now,
    validate_identifiers,
    write_json,
)


def missing(error: ClientError, codes: set[str]) -> bool:
    return error.response.get("Error", {}).get("Code") in codes


def describe_stack(cloudformation: Any, name: str) -> dict[str, Any] | None:
    try:
        stacks = cloudformation.describe_stacks(StackName=name).get("Stacks", [])
    except ClientError as error:
        if missing(error, {"ValidationError"}) and "does not exist" in str(error):
            return None
        raise
    if len(stacks) != 1:
        raise RuntimeError(f"expected exactly one stack description for {name}")
    return stacks[0]


def stack_resources(cloudformation: Any, name: str) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for page in cloudformation.get_paginator("list_stack_resources").paginate(StackName=name):
        resources.extend(page.get("StackResourceSummaries", []))
    return resources


def owned_stack(cloudformation: Any, name: str, run_id: str, session_id: str) -> dict[str, Any] | None:
    stack = describe_stack(cloudformation, name)
    if stack is not None and not tags_match(tag_map(stack.get("Tags", [])), run_id, session_id):
        raise RuntimeError(f"refusing non-owned CloudFormation stack: {name}")
    return stack


def inventory(session: Any, run_id: str, session_id: str) -> dict[str, Any]:
    validate_identifiers(run_id, session_id)
    image_stack_name, runtime_stack_name = stack_names(session_id)
    cloudformation = session.client("cloudformation", config=SDK_CONFIG)
    stacks = []
    for name in (runtime_stack_name, image_stack_name):
        stack = owned_stack(cloudformation, name, run_id, session_id)
        if stack is not None:
            stacks.append({"name": name, "status": stack.get("StackStatus")})

    ecr = session.client("ecr", config=SDK_CONFIG)
    repositories: list[str] = []
    images: list[str] = []
    try:
        repository = ecr.describe_repositories(
            repositoryNames=[repository_name(run_id)]
        ).get("repositories", [])
    except ClientError as error:
        if not missing(error, {"RepositoryNotFoundException"}):
            raise
        repository = []
    if repository:
        if len(repository) != 1:
            raise RuntimeError("targeted repository cardinality is not exact")
        tags = tag_map(ecr.list_tags_for_resource(resourceArn=repository[0]["repositoryArn"]).get("tags", []))
        if not tags_match(tags, run_id, session_id):
            raise RuntimeError("targeted ECR repository does not have exact ownership tags")
        repositories.append(repository[0]["repositoryName"])
        for page in ecr.get_paginator("list_images").paginate(
            repositoryName=repository[0]["repositoryName"]
        ):
            images.extend(
                sorted(
                    f"{item.get('imageTag', '<untagged>')}@{item.get('imageDigest', '<missing>')}"
                    for item in page.get("imageIds", [])
                )
            )

    ec2 = session.client("ec2", config=SDK_CONFIG)
    instances = []
    response = ec2.describe_instances(Filters=[
        {"Name": "tag:RunId", "Values": [run_id]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]},
    ])
    for reservation in response.get("Reservations", []):
        instances.extend(item["InstanceId"] for item in reservation.get("Instances", []))
    volumes = [item["VolumeId"] for item in ec2.describe_volumes(
        Filters=[{"Name": "tag:RunId", "Values": [run_id]}]
    ).get("Volumes", [])]
    vpcs = [item["VpcId"] for item in ec2.describe_vpcs(
        Filters=[{"Name": "tag:RunId", "Values": [run_id]}]
    ).get("Vpcs", [])]
    subnets = [item["SubnetId"] for item in ec2.describe_subnets(
        Filters=[{"Name": "tag:RunId", "Values": [run_id]}]
    ).get("Subnets", [])]
    security_groups = [item["GroupId"] for item in ec2.describe_security_groups(
        Filters=[{"Name": "tag:RunId", "Values": [run_id]}]
    ).get("SecurityGroups", [])]

    autoscaling = session.client("autoscaling", config=SDK_CONFIG)
    groups = []
    for page in autoscaling.get_paginator("describe_auto_scaling_groups").paginate():
        for group in page.get("AutoScalingGroups", []):
            if tag_map(group.get("Tags", [])).get("RunId") == run_id:
                groups.append(group["AutoScalingGroupName"])

    logs = session.client("logs", config=SDK_CONFIG)
    log_groups = []
    prefix = f"/loopad/perf/phase7-targeted/{run_id}/"
    for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
        log_groups.extend(
            group["logGroupName"]
            for group in page.get("logGroups", [])
            if group.get("logGroupName", "").startswith(prefix)
        )

    secrets = session.client("secretsmanager", config=SDK_CONFIG)
    secret_arns = []
    for page in secrets.get_paginator("list_secrets").paginate(
        IncludePlannedDeletion=True,
        Filters=[{"Key": "tag-key", "Values": ["RunId"]}],
    ):
        for secret in page.get("SecretList", []):
            if tag_map(secret.get("Tags", [])).get("RunId") == run_id:
                secret_arns.append(secret["ARN"])

    s3 = session.client("s3", config=SDK_CONFIG)
    buckets = []
    for item in s3.list_buckets().get("Buckets", []):
        name = item.get("Name")
        if not isinstance(name, str):
            continue
        try:
            tags = tag_map(s3.get_bucket_tagging(Bucket=name).get("TagSet", []))
        except ClientError as error:
            if missing(error, {"NoSuchTagSet", "NoSuchBucket"}):
                continue
            raise
        if tags.get("RunId") == run_id:
            if not tags_match(tags, run_id, session_id):
                raise RuntimeError(f"run ID collision on S3 bucket: {name}")
            buckets.append(name)

    ecs = session.client("ecs", config=SDK_CONFIG)
    capacity_providers = []
    next_token = None
    while True:
        request = {"nextToken": next_token} if next_token else {}
        response = ecs.describe_capacity_providers(**request)
        capacity_providers.extend(
            item for item in response.get("capacityProviders", []) if isinstance(item, dict)
        )
        next_token = response.get("nextToken")
        if not next_token:
            break
    capacity_provider_names = sorted(
        item["name"] for item in capacity_providers
        if run_id in str(item.get("name", "")) and item.get("status") != "INACTIVE"
    )

    tagging = session.client("resourcegroupstaggingapi", config=SDK_CONFIG)
    tagged: dict[str, list[str]] = {"RunId": [], "SessionId": []}
    for key, value in (("RunId", run_id), ("SessionId", session_id)):
        for page in tagging.get_paginator("get_resources").paginate(
            TagFilters=[{"Key": key, "Values": [value]}]
        ):
            tagged[key].extend(
                item["ResourceARN"] for item in page.get("ResourceTagMappingList", [])
            )

    classes = {
        "cloudformationStacks": sorted(stacks, key=lambda item: item["name"]),
        "ecrRepositories": sorted(repositories),
        "ecrImages": sorted(images),
        "ec2Instances": sorted(instances),
        "ebsVolumes": sorted(volumes),
        "vpcs": sorted(vpcs),
        "subnets": sorted(subnets),
        "securityGroups": sorted(security_groups),
        "autoScalingGroups": sorted(groups),
        "ecsCapacityProviders": capacity_provider_names,
        "cloudWatchLogGroups": sorted(log_groups),
        "secrets": sorted(secret_arns),
        "s3Buckets": sorted(buckets),
        "taggingApiRunId": sorted(set(tagged["RunId"])),
        "taggingApiSessionId": sorted(set(tagged["SessionId"])),
    }
    all_zero = all(not value for value in classes.values())
    return {
        "schemaVersion": 1,
        "workload": "phase7-targeted-archive-diagnostic",
        "checkedAt": utc_now(),
        "runId": run_id,
        "sessionId": session_id,
        "classes": classes,
        "serviceClassCount": len(classes),
        "allZero": all_zero,
    }


def clear_bucket(s3: Any, bucket: str, run_id: str, session_id: str) -> None:
    tags = tag_map(s3.get_bucket_tagging(Bucket=bucket).get("TagSet", []))
    if not tags_match(tags, run_id, session_id):
        raise RuntimeError(f"refusing non-owned S3 cleanup: {bucket}")
    while True:
        page = s3.list_objects_v2(Bucket=bucket, MaxKeys=1000)
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            response = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
            if response.get("Errors"):
                raise RuntimeError(f"targeted S3 cleanup failed: {response['Errors']}")
        if not page.get("IsTruncated"):
            break


def stop_owned_standalone_tasks(
    session: Any,
    cluster: str,
    run_id: str,
    session_id: str,
) -> None:
    ecs = session.client("ecs", config=SDK_CONFIG)
    arns: list[str] = []
    for page in ecs.get_paginator("list_tasks").paginate(cluster=cluster, desiredStatus="RUNNING"):
        arns.extend(page.get("taskArns", []))
    if not arns:
        return
    described = ecs.describe_tasks(cluster=cluster, tasks=arns, include=["TAGS"]).get("tasks", [])
    stopped = []
    for task in described:
        if str(task.get("group", "")).startswith("service:"):
            continue
        if not tags_match(tag_map(task.get("tags", [])), run_id, session_id):
            raise RuntimeError(f"refusing non-owned standalone ECS task cleanup: {task.get('taskArn')}")
        ecs.stop_task(
            cluster=cluster,
            task=task["taskArn"],
            reason="Exact-scope targeted archive attempt cleanup",
        )
        stopped.append(task["taskArn"])
    if stopped:
        ecs.get_waiter("tasks_stopped").wait(
            cluster=cluster,
            tasks=stopped,
            WaiterConfig={"Delay": 6, "MaxAttempts": 100},
        )


def cleanup_terminal_residuals(
    session: Any,
    run_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Remove exact-owned terminal tombstones left after stack deletion."""
    ownership = expected_tags(run_id, session_id)
    tagging = session.client("resourcegroupstaggingapi", config=SDK_CONFIG)
    mappings = [
        item
        for page in tagging.get_paginator("get_resources").paginate(
            TagFilters=[
                {"Key": key, "Values": [value]}
                for key, value in ownership.items()
            ]
        )
        for item in page.get("ResourceTagMappingList", [])
    ]
    for item in mappings:
        if not tags_match(tag_map(item.get("Tags", [])), run_id, session_id):
            raise RuntimeError(
                "refusing targeted terminal cleanup without exact ownership"
            )

    ecs_prefix = f"arn:aws:ecs:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:"
    task_prefix = f"{ecs_prefix}task-definition/"
    cluster_prefix = f"{ecs_prefix}cluster/"
    service_prefix = f"{ecs_prefix}service/"
    endpoint_prefix = (
        f"arn:aws:ec2:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:vpc-endpoint/"
    )
    instance_prefix = (
        f"arn:aws:ec2:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:instance/"
    )
    volume_prefix = (
        f"arn:aws:ec2:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:volume/"
    )
    arns = sorted(str(item.get("ResourceARN", "")) for item in mappings)
    task_definitions = [arn for arn in arns if arn.startswith(task_prefix)]
    clusters = [arn for arn in arns if arn.startswith(cluster_prefix)]
    services = [arn for arn in arns if arn.startswith(service_prefix)]
    endpoints = [arn for arn in arns if arn.startswith(endpoint_prefix)]
    instances = [arn for arn in arns if arn.startswith(instance_prefix)]
    volumes = [arn for arn in arns if arn.startswith(volume_prefix)]
    classified = set(
        task_definitions + clusters + services + endpoints + instances + volumes
    )
    if classified != set(arns):
        raise RuntimeError(
            "refusing unrecognized targeted terminal residual cleanup: "
            + ", ".join(sorted(set(arns) - classified))
        )

    ecs = session.client("ecs", config=SDK_CONFIG)
    inactive_task_definitions: list[str] = []
    for arn in task_definitions:
        response = ecs.describe_task_definition(
            taskDefinition=arn,
            include=["TAGS"],
        )
        definition = response.get("taskDefinition", {})
        if (
            definition.get("taskDefinitionArn") != arn
            or definition.get("status") not in {"INACTIVE", "DELETE_IN_PROGRESS"}
            or not tags_match(
                tag_map(response.get("tags", [])), run_id, session_id
            )
        ):
            raise RuntimeError(
                f"refusing non-terminal or non-owned targeted task definition: {arn}"
            )
        if definition.get("status") == "INACTIVE":
            inactive_task_definitions.append(arn)
    for offset in range(0, len(inactive_task_definitions), 10):
        batch = inactive_task_definitions[offset:offset + 10]
        response = ecs.delete_task_definitions(taskDefinitions=batch)
        deleted = {
            item.get("taskDefinitionArn")
            for item in response.get("taskDefinitions", [])
        }
        if response.get("failures", []) or deleted != set(batch):
            raise RuntimeError(
                "targeted terminal task definition deletion was not fully accepted"
            )

    ownership_keys = list(ownership)
    services_by_cluster: dict[str, list[tuple[str, str]]] = {}
    for arn in services:
        parts = arn.removeprefix(service_prefix).split("/")
        if len(parts) != 2 or not all(parts):
            raise RuntimeError(f"malformed targeted ECS service ARN: {arn}")
        cluster_name, service_name = parts
        response = ecs.describe_services(
            cluster=cluster_name,
            services=[service_name],
            include=["TAGS"],
        )
        described = response.get("services", [])
        if (
            len(described) != 1
            or response.get("failures", [])
            or described[0].get("serviceArn") != arn
            or described[0].get("status") != "INACTIVE"
            or int(described[0].get("desiredCount", 0)) != 0
            or int(described[0].get("runningCount", 0)) != 0
            or int(described[0].get("pendingCount", 0)) != 0
            or not tags_match(
                tag_map(described[0].get("tags", [])), run_id, session_id
            )
        ):
            raise RuntimeError(
                f"refusing non-terminal or non-owned targeted ECS service: {arn}"
            )
        services_by_cluster.setdefault(cluster_name, []).append((arn, service_name))

    clusters_by_name: dict[str, str] = {}
    for arn in clusters:
        described = ecs.describe_clusters(
            clusters=[arn],
            include=["TAGS"],
        ).get("clusters", [])
        if (
            len(described) != 1
            or described[0].get("clusterArn") != arn
            or described[0].get("status") != "INACTIVE"
            or int(described[0].get("runningTasksCount", 0)) != 0
            or int(described[0].get("pendingTasksCount", 0)) != 0
            or int(described[0].get("activeServicesCount", 0)) != 0
            or int(described[0].get("registeredContainerInstancesCount", 0)) != 0
            or not tags_match(
                tag_map(described[0].get("tags", [])), run_id, session_id
            )
        ):
            raise RuntimeError(
                f"refusing non-terminal or non-owned targeted ECS cluster: {arn}"
            )
        cluster_name = arn.rsplit("/", 1)[-1]
        clusters_by_name[cluster_name] = arn
    if set(services_by_cluster).difference(clusters_by_name):
        raise RuntimeError(
            "targeted inactive service has no exact-owned terminal cluster"
        )

    terminal_task_arn: str | None = None
    if services:
        family_suffix = (
            run_id.removeprefix("run_")
            .removesuffix("_phase7_archive_diagnostic")
            .replace("_", "-")
        )
        registered = ecs.register_task_definition(
            family=f"loopad-phase7-targeted-terminal-cleanup-{family_suffix}",
            networkMode="bridge",
            requiresCompatibilities=["EC2"],
            cpu="256",
            memory="512",
            containerDefinitions=[{
                "name": "terminal-cleanup",
                "image": "public.ecr.aws/docker/library/busybox:1.36",
                "essential": True,
                "memory": 32,
            }],
            tags=[{"key": key, "value": value} for key, value in ownership.items()],
        ).get("taskDefinition", {})
        terminal_task_arn = str(registered.get("taskDefinitionArn", ""))
        if not terminal_task_arn or registered.get("status") != "ACTIVE":
            raise RuntimeError(
                "targeted terminal cleanup task definition was not accepted"
            )
        described = ecs.describe_task_definition(
            taskDefinition=terminal_task_arn,
            include=["TAGS"],
        )
        if (
            described.get("taskDefinition", {}).get("taskDefinitionArn")
            != terminal_task_arn
            or described.get("taskDefinition", {}).get("status") != "ACTIVE"
            or not tags_match(
                tag_map(described.get("tags", [])), run_id, session_id
            )
        ):
            raise RuntimeError(
                "targeted terminal cleanup task definition lost exact ownership"
            )

    for cluster_name, arn in sorted(clusters_by_name.items()):
        recreated = ecs.create_cluster(
            clusterName=cluster_name,
            tags=[{"key": key, "value": value} for key, value in ownership.items()],
        ).get("cluster", {})
        if (
            recreated.get("clusterArn") != arn
            or recreated.get("status") != "ACTIVE"
            or int(recreated.get("runningTasksCount", 0)) != 0
            or int(recreated.get("pendingTasksCount", 0)) != 0
            or int(recreated.get("activeServicesCount", 0)) != 0
            or int(recreated.get("registeredContainerInstancesCount", 0)) != 0
        ):
            raise RuntimeError(
                f"targeted terminal ECS cluster could not be safely reactivated: {arn}"
            )
        if not tags_match(
            tag_map(ecs.list_tags_for_resource(resourceArn=arn).get("tags", [])),
            run_id,
            session_id,
        ):
            raise RuntimeError(
                f"reactivated targeted ECS cluster lost exact ownership: {arn}"
            )
        service_names: list[str] = []
        for service_arn, service_name in sorted(
            services_by_cluster.get(cluster_name, [])
        ):
            if terminal_task_arn is None:
                raise RuntimeError("targeted terminal service lacks a cleanup task")
            recreated_service = ecs.create_service(
                cluster=cluster_name,
                serviceName=service_name,
                taskDefinition=terminal_task_arn,
                desiredCount=0,
                launchType="EC2",
                tags=[
                    {"key": key, "value": value}
                    for key, value in ownership.items()
                ],
            ).get("service", {})
            if (
                recreated_service.get("serviceArn") != service_arn
                or recreated_service.get("status") != "ACTIVE"
                or int(recreated_service.get("desiredCount", 0)) != 0
                or int(recreated_service.get("runningCount", 0)) != 0
                or int(recreated_service.get("pendingCount", 0)) != 0
                or not tags_match(
                    tag_map(
                        ecs.list_tags_for_resource(
                            resourceArn=service_arn
                        ).get("tags", [])
                    ),
                    run_id,
                    session_id,
                )
            ):
                raise RuntimeError(
                    f"targeted terminal service could not be safely reactivated: {service_arn}"
                )
            ecs.untag_resource(
                resourceArn=service_arn,
                tagKeys=ownership_keys,
            )
            remaining = tag_map(
                ecs.list_tags_for_resource(resourceArn=service_arn).get(
                    "tags", []
                )
            )
            if any(key in remaining for key in ownership):
                raise RuntimeError(
                    f"reactivated targeted service retained ownership: {service_arn}"
                )
            deleted_service = ecs.delete_service(
                cluster=cluster_name,
                service=service_name,
                force=True,
            ).get("service", {})
            if (
                deleted_service.get("serviceArn") != service_arn
                or deleted_service.get("status") not in {"DRAINING", "INACTIVE"}
                or int(deleted_service.get("desiredCount", 0)) != 0
                or int(deleted_service.get("runningCount", 0)) != 0
                or int(deleted_service.get("pendingCount", 0)) != 0
            ):
                raise RuntimeError(
                    f"reactivated targeted service did not enter deletion: {service_arn}"
                )
            service_names.append(service_name)
        if service_names:
            ecs.get_waiter("services_inactive").wait(
                cluster=cluster_name,
                services=service_names,
                WaiterConfig={"Delay": 6, "MaxAttempts": 50},
            )
        ecs.untag_resource(resourceArn=arn, tagKeys=ownership_keys)
        remaining = tag_map(
            ecs.list_tags_for_resource(resourceArn=arn).get("tags", [])
        )
        if any(key in remaining for key in ownership):
            raise RuntimeError(
                f"reactivated targeted ECS cluster retained ownership tags: {arn}"
            )
        deleted = ecs.delete_cluster(cluster=arn).get("cluster", {})
        if (
            deleted.get("clusterArn") != arn
            or deleted.get("status") != "INACTIVE"
            or int(deleted.get("runningTasksCount", 0)) != 0
            or int(deleted.get("pendingTasksCount", 0)) != 0
            or int(deleted.get("activeServicesCount", 0)) != 0
            or int(deleted.get("registeredContainerInstancesCount", 0)) != 0
        ):
            raise RuntimeError(
                f"reactivated targeted ECS cluster did not return to terminal state: {arn}"
            )

    if terminal_task_arn is not None:
        ecs.untag_resource(
            resourceArn=terminal_task_arn,
            tagKeys=ownership_keys,
        )
        remaining = tag_map(
            ecs.list_tags_for_resource(resourceArn=terminal_task_arn).get(
                "tags", []
            )
        )
        if any(key in remaining for key in ownership):
            raise RuntimeError(
                "targeted terminal cleanup task definition retained ownership"
            )
        deregistered = ecs.deregister_task_definition(
            taskDefinition=terminal_task_arn
        ).get("taskDefinition", {})
        if (
            deregistered.get("taskDefinitionArn") != terminal_task_arn
            or deregistered.get("status") != "INACTIVE"
        ):
            raise RuntimeError(
                "targeted terminal cleanup task definition did not deregister"
            )
        deleted = ecs.delete_task_definitions(
            taskDefinitions=[terminal_task_arn]
        )
        if (
            deleted.get("failures", [])
            or {
                item.get("taskDefinitionArn")
                for item in deleted.get("taskDefinitions", [])
            } != {terminal_task_arn}
        ):
            raise RuntimeError(
                "targeted terminal cleanup task definition deletion failed"
            )

    ec2 = session.client("ec2", config=SDK_CONFIG)
    endpoint_ids = [arn.removeprefix(endpoint_prefix) for arn in endpoints]
    if endpoint_ids:
        try:
            live_endpoints = ec2.describe_vpc_endpoints(
                VpcEndpointIds=endpoint_ids
            ).get("VpcEndpoints", [])
        except ClientError as error:
            if not missing(error, {"InvalidVpcEndpointId.NotFound"}):
                raise
        else:
            if live_endpoints:
                raise RuntimeError(
                    "refusing to untag a targeted VPC endpoint that still exists"
                )
    for arn in instances:
        instance_id = arn.removeprefix(instance_prefix)
        reservations = ec2.describe_instances(
            InstanceIds=[instance_id]
        ).get("Reservations", [])
        described = [
            item
            for reservation in reservations
            for item in reservation.get("Instances", [])
        ]
        if (
            len(described) != 1
            or described[0].get("InstanceId") != instance_id
            or described[0].get("State", {}).get("Name") != "terminated"
            or not tags_match(
                tag_map(described[0].get("Tags", [])), run_id, session_id
            )
        ):
            raise RuntimeError(
                f"refusing non-terminal or non-owned targeted instance: {instance_id}"
            )
    for arn in volumes:
        volume_id = arn.removeprefix(volume_prefix)
        try:
            described = ec2.describe_volumes(VolumeIds=[volume_id]).get(
                "Volumes", []
            )
        except ClientError as error:
            if not missing(error, {"InvalidVolume.NotFound"}):
                raise
        else:
            if described:
                raise RuntimeError(
                    f"refusing to untag a targeted volume that still exists: {volume_id}"
                )

    resource_group_untag = sorted(
        set(
            task_definitions
            + endpoints
            + instances
            + volumes
            + ([terminal_task_arn] if terminal_task_arn else [])
        )
    )
    for offset in range(0, len(resource_group_untag), 20):
        batch = resource_group_untag[offset:offset + 20]
        response = tagging.untag_resources(
            ResourceARNList=batch,
            TagKeys=ownership_keys,
        )
        if response.get("FailedResourcesMap", {}):
            raise RuntimeError(
                "targeted terminal Resource Groups untag was not fully accepted: "
                + json.dumps(response["FailedResourcesMap"], sort_keys=True)
            )
    return {
        "mappingCount": len(mappings),
        "deletedTaskDefinitions": len(inactive_task_definitions),
        "untaggedServices": len(services),
        "reactivatedAndDeletedClusters": len(clusters),
        "temporaryTaskDefinitions": 1 if terminal_task_arn else 0,
        "resourceGroupUntagged": len(resource_group_untag),
    }


def cleanup(session: Any, run_id: str, session_id: str) -> dict[str, Any]:
    validate_identifiers(run_id, session_id)
    started_at = utc_now()
    image_stack_name, runtime_stack_name = stack_names(session_id)
    cloudformation = session.client("cloudformation", config=SDK_CONFIG)
    runtime = owned_stack(cloudformation, runtime_stack_name, run_id, session_id)
    if runtime is not None:
        resources = stack_resources(cloudformation, runtime_stack_name)
        clusters = [
            item.get("PhysicalResourceId") for item in resources
            if item.get("ResourceType") == "AWS::ECS::Cluster" and item.get("PhysicalResourceId")
        ]
        for cluster in clusters:
            stop_owned_standalone_tasks(session, str(cluster), run_id, session_id)
        s3 = session.client("s3", config=SDK_CONFIG)
        buckets = [
            item.get("PhysicalResourceId") for item in resources
            if item.get("ResourceType") == "AWS::S3::Bucket" and item.get("PhysicalResourceId")
        ]
        for bucket in buckets:
            try:
                clear_bucket(s3, str(bucket), run_id, session_id)
            except ClientError as error:
                if not missing(error, {"NoSuchBucket"}):
                    raise
        cloudformation.delete_stack(StackName=runtime_stack_name)
        try:
            cloudformation.get_waiter("stack_delete_complete").wait(
                StackName=runtime_stack_name,
                WaiterConfig={"Delay": 15, "MaxAttempts": 240},
            )
        except WaiterError as error:
            raise RuntimeError(f"targeted runtime stack cleanup did not complete: {error}") from error

    ecr = session.client("ecr", config=SDK_CONFIG)
    name = repository_name(run_id)
    try:
        repositories = ecr.describe_repositories(repositoryNames=[name]).get("repositories", [])
    except ClientError as error:
        if not missing(error, {"RepositoryNotFoundException"}):
            raise
        repositories = []
    if repositories:
        if len(repositories) != 1:
            raise RuntimeError("targeted ECR repository cardinality is not exact during cleanup")
        tags = tag_map(ecr.list_tags_for_resource(
            resourceArn=repositories[0]["repositoryArn"]
        ).get("tags", []))
        if not tags_match(tags, run_id, session_id):
            raise RuntimeError("refusing non-owned targeted ECR cleanup")
        image_ids: list[dict[str, str]] = []
        for page in ecr.get_paginator("list_images").paginate(repositoryName=name):
            image_ids.extend(page.get("imageIds", []))
        for offset in range(0, len(image_ids), 100):
            ecr.batch_delete_image(repositoryName=name, imageIds=image_ids[offset:offset + 100])
        ecr.delete_repository(repositoryName=name, force=False)

    image = owned_stack(cloudformation, image_stack_name, run_id, session_id)
    if image is not None:
        cloudformation.delete_stack(StackName=image_stack_name)
        try:
            cloudformation.get_waiter("stack_delete_complete").wait(
                StackName=image_stack_name,
                WaiterConfig={"Delay": 10, "MaxAttempts": 180},
            )
        except WaiterError as error:
            raise RuntimeError(f"targeted image stack cleanup did not complete: {error}") from error

    terminal_cleanup = cleanup_terminal_residuals(session, run_id, session_id)
    latest = inventory(session, run_id, session_id)
    for _ in range(20):
        if latest["allZero"]:
            break
        time.sleep(15)
        latest = inventory(session, run_id, session_id)
    return {
        "schemaVersion": 1,
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "runId": run_id,
        "sessionId": session_id,
        "exactCleanupOrder": ["runtime", "archive-image-and-repository", "image-stack"],
        "terminalResidualCleanup": terminal_cleanup,
        "authoritativeInventory": latest,
        "passed": latest["allZero"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    session = locked_session()
    result = (
        inventory(session, args.run_id, args.session_id)
        if args.inventory_only
        else cleanup(session, args.run_id, args.session_id)
    )
    write_json(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    passed = result.get("allZero") if args.inventory_only else result.get("passed")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())

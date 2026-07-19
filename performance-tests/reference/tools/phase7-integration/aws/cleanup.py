#!/usr/bin/env python3
"""Exact-ownership Phase 7-2 teardown and authoritative residual inventory."""

from __future__ import annotations

import argparse
import json
import time
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
    RUNTIME_STACK_NAME,
    expected_tags,
    tag_map,
    tags_match,
    utc_now,
    validate_identifiers,
    write_json,
)


SDK_CONFIG = Config(retries={"mode": "standard", "total_max_attempts": 8}, user_agent_appid="loopad-phase7-cleanup/1")


class Cleanup:
    def __init__(self, run_id: str, session_id: str) -> None:
        validate_identifiers(run_id, session_id)
        self.run_id = run_id
        self.session_id = session_id
        self.session = boto3.Session(region_name=EXPECTED_REGION)
        self._clients: dict[str, Any] = {}
        self.deadline_breached = False

    def client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = self.session.client(service, region_name=EXPECTED_REGION, config=SDK_CONFIG)
        return self._clients[service]

    def assert_identity(self) -> dict[str, str]:
        identity = self.client("sts").get_caller_identity()
        if identity.get("Account") != EXPECTED_ACCOUNT or identity.get("Arn") != EXPECTED_OPERATOR_ARN:
            raise RuntimeError("cleanup requires the exact user-approved root identity")
        return {"account": identity["Account"], "arn": identity["Arn"]}

    def stack(self, name: str) -> dict[str, Any] | None:
        try:
            stack = self.client("cloudformation").describe_stacks(StackName=name)["Stacks"][0]
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ValidationError":
                return None
            raise
        tags = tag_map(stack.get("Tags", []))
        if not tags_match(tags, self.run_id, self.session_id):
            raise RuntimeError(f"refusing non-owned stack: {name}")
        return {"name": name, "status": stack["StackStatus"], "tags": tags,
                "outputs": {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}}

    def execute(self, deadline: float | None = None) -> None:
        self.assert_identity()
        runtime = self.stack(RUNTIME_STACK_NAME)
        if runtime:
            outputs = runtime["outputs"]
            archive_outputs_present = bool(
                outputs.get("ArchiveClusterName")
                and outputs.get("ArchiveTaskDefinitionArn")
            )
            if archive_outputs_present:
                # A normally-created runtime must retain the exact archive-task
                # stop before stack deletion. Failed/rolled-back creations may
                # legitimately have no Outputs at all; CloudFormation remains
                # the authoritative exact-owned cleanup path in that case.
                self.stop_archive_tasks(outputs, deadline)
            elif runtime["status"] in {
                "CREATE_COMPLETE",
                "UPDATE_COMPLETE",
                "UPDATE_ROLLBACK_COMPLETE",
            }:
                raise RuntimeError(
                    "owned healthy runtime stack is missing archive task cleanup outputs"
                )
            for output in ("ArchiveBucketName", "FailureBucketName"):
                bucket = outputs.get(output)
                if bucket:
                    self.empty_bucket(bucket, deadline, expected_name=bucket)
            self.quiesce_runtime_capacity(runtime["status"], deadline)
            self.delete_stack(
                RUNTIME_STACK_NAME,
                deadline,
                already_deleting=runtime["status"] == "DELETE_IN_PROGRESS",
            )
        image = self.stack(IMAGE_STACK_NAME)
        if image:
            repository_outputs = {
                "CollectorRepositoryName": "collector",
                "ConsumerRepositoryName": "consumer",
                "ArchiveRepositoryName": "archive",
            }
            for output, role in repository_outputs.items():
                repository = image["outputs"].get(output)
                if repository:
                    expected_name = f"loop-ad/perf-phase7/{self.run_id}/{role}"
                    self.empty_repository(
                        repository, deadline, expected_name=expected_name
                    )
            self.delete_stack(
                IMAGE_STACK_NAME,
                deadline,
                already_deleting=image["status"] == "DELETE_IN_PROGRESS",
            )
        self.cleanup_terminal_residuals(deadline)

    def quiesce_runtime_capacity(
        self, stack_status: str, deadline: float | None = None
    ) -> None:
        """Stop exact-owned paid capacity before CloudFormation deletion waits.

        CloudFormation can omit Outputs after a failed create, so the stack's
        physical-resource inventory is the authoritative discovery source.
        Every surviving resource is validated before the first mutation.
        """
        expected_counts = {
            "AWS::ECS::Service": 4,
            "AWS::AutoScaling::AutoScalingGroup": 5,
            "AWS::ElasticLoadBalancingV2::TargetGroup": 3,
        }
        resources: dict[str, list[str]] = {
            resource_type: [] for resource_type in expected_counts
        }
        cloudformation = self.client("cloudformation")
        for page in cloudformation.get_paginator("list_stack_resources").paginate(
            StackName=RUNTIME_STACK_NAME
        ):
            for summary in page.get("StackResourceSummaries", []):
                resource_type = summary.get("ResourceType")
                physical_id = summary.get("PhysicalResourceId")
                if resource_type not in resources or not physical_id:
                    continue
                if not isinstance(physical_id, str):
                    raise RuntimeError(
                        "runtime capacity resource has a malformed physical ID"
                    )
                resources[resource_type].append(physical_id)

        for resource_type, physical_ids in resources.items():
            if len(physical_ids) != len(set(physical_ids)):
                raise RuntimeError(
                    f"duplicate runtime capacity resource: {resource_type}"
                )
            if len(physical_ids) > expected_counts[resource_type]:
                raise RuntimeError(
                    f"unexpected runtime capacity count: {resource_type}"
                )
        if stack_status in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
            "UPDATE_ROLLBACK_COMPLETE",
        } and {
            resource_type: len(physical_ids)
            for resource_type, physical_ids in resources.items()
        } != expected_counts:
            raise RuntimeError(
                "healthy runtime stack does not expose the exact capacity inventory"
            )

        service_arns = sorted(resources["AWS::ECS::Service"])
        asg_names = sorted(resources["AWS::AutoScaling::AutoScalingGroup"])
        target_group_arns = sorted(
            resources["AWS::ElasticLoadBalancingV2::TargetGroup"]
        )

        # Phase 1: validate the complete surviving set without mutating it.
        ecs = self.client("ecs")
        services_to_delete: list[tuple[str, str, str]] = []
        services_to_wait: dict[str, tuple[str, str]] = {}
        target_group_set = set(target_group_arns)
        service_prefix = (
            f"arn:aws:ecs:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:service/"
        )
        for service_arn in service_arns:
            if not service_arn.startswith(service_prefix):
                raise RuntimeError(
                    f"malformed exact-stack ECS service ARN: {service_arn}"
                )
            path = service_arn[len(service_prefix):].split("/")
            if len(path) != 2 or not all(path):
                raise RuntimeError(
                    f"malformed exact-stack ECS service ARN: {service_arn}"
                )
            cluster_name, service_name = path
            response = ecs.describe_services(
                cluster=cluster_name,
                services=[service_arn],
                include=["TAGS"],
            )
            services = response.get("services", [])
            failures = response.get("failures", [])
            if not services and len(failures) == 1 and failures[0].get(
                "reason"
            ) == "MISSING":
                continue
            if len(services) != 1 or failures:
                raise RuntimeError(
                    f"exact ECS service cannot be resolved for cleanup: {service_arn}"
                )
            service = services[0]
            status = service.get("status")
            if (
                service.get("serviceArn") != service_arn
                or service.get("serviceName") != service_name
                or status not in {"ACTIVE", "DRAINING", "INACTIVE"}
                or not tags_match(
                    tag_map(service.get("tags", [])),
                    self.run_id,
                    self.session_id,
                )
            ):
                raise RuntimeError(
                    f"refusing non-owned or unexpected ECS service cleanup: {service_arn}"
                )
            attached_target_groups = {
                item.get("targetGroupArn")
                for item in service.get("loadBalancers", [])
            }
            if (
                None in attached_target_groups
                or not attached_target_groups.issubset(target_group_set)
            ):
                raise RuntimeError(
                    f"ECS service target group is outside its exact stack: {service_arn}"
                )
            if status == "ACTIVE":
                services_to_delete.append(
                    (cluster_name, service_name, service_arn)
                )
                services_to_wait[service_arn] = (cluster_name, service_name)
            elif status == "DRAINING":
                services_to_wait[service_arn] = (cluster_name, service_name)

        elbv2 = self.client("elbv2")
        existing_target_groups: list[str] = []
        target_group_prefix = (
            f"arn:aws:elasticloadbalancing:{EXPECTED_REGION}:"
            f"{EXPECTED_ACCOUNT}:targetgroup/"
        )
        for target_group_arn in target_group_arns:
            if not target_group_arn.startswith(target_group_prefix):
                raise RuntimeError(
                    f"malformed exact-stack target group ARN: {target_group_arn}"
                )
            try:
                descriptions = elbv2.describe_tags(
                    ResourceArns=[target_group_arn]
                ).get("TagDescriptions", [])
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != (
                    "TargetGroupNotFound"
                ):
                    raise
                continue
            if (
                len(descriptions) != 1
                or descriptions[0].get("ResourceArn") != target_group_arn
                or not tags_match(
                    tag_map(descriptions[0].get("Tags", [])),
                    self.run_id,
                    self.session_id,
                )
            ):
                raise RuntimeError(
                    f"refusing non-owned target group cleanup: {target_group_arn}"
                )
            existing_target_groups.append(target_group_arn)

        autoscaling = self.client("autoscaling")
        groups = (
            autoscaling.describe_auto_scaling_groups(
                AutoScalingGroupNames=asg_names
            ).get("AutoScalingGroups", [])
            if asg_names
            else []
        )
        by_name = {
            str(group.get("AutoScalingGroupName")): group
            for group in groups
        }
        if any(name not in asg_names for name in by_name):
            raise RuntimeError("unexpected ASG returned during exact runtime cleanup")
        existing_asgs: list[str] = []
        for name in asg_names:
            group = by_name.get(name)
            if group is None:
                continue
            if not tags_match(
                tag_map(group.get("Tags", [])),
                self.run_id,
                self.session_id,
            ):
                raise RuntimeError(f"refusing non-owned ASG cleanup: {name}")
            existing_asgs.append(name)

        # Phase 2: mutate only after all surviving resources passed validation.
        for target_group_arn in existing_target_groups:
            self.observe_deadline(deadline)
            try:
                attributes = elbv2.modify_target_group_attributes(
                    TargetGroupArn=target_group_arn,
                    Attributes=[{
                        "Key": "deregistration_delay.timeout_seconds",
                        "Value": "0",
                    }],
                ).get("Attributes", [])
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != (
                    "TargetGroupNotFound"
                ) or not self._target_group_is_missing(elbv2, target_group_arn):
                    raise
                continue
            if not any(
                item.get("Key") == "deregistration_delay.timeout_seconds"
                and item.get("Value") == "0"
                for item in attributes
            ):
                raise RuntimeError(
                    f"target group deregistration delay was not reduced: {target_group_arn}"
                )

        for cluster_name, service_name, service_arn in services_to_delete:
            self.observe_deadline(deadline)
            try:
                deleted = ecs.delete_service(
                    cluster=cluster_name,
                    service=service_arn,
                    force=True,
                ).get("service", {})
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != (
                    "ServiceNotFoundException"
                ) or not self._ecs_service_is_missing(
                    ecs, cluster_name, service_arn
                ):
                    raise
                services_to_wait.pop(service_arn, None)
                continue
            if (
                deleted.get("serviceArn") != service_arn
                or deleted.get("status") not in {"DRAINING", "INACTIVE"}
            ):
                raise RuntimeError(
                    f"exact ECS service did not enter deletion: {service_name}"
                )
            if deleted.get("status") == "INACTIVE":
                services_to_wait.pop(service_arn, None)

        for name in existing_asgs:
            self.observe_deadline(deadline)
            try:
                autoscaling.update_auto_scaling_group(
                    AutoScalingGroupName=name,
                    MinSize=0,
                    DesiredCapacity=0,
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != (
                    "ValidationError"
                ) or not self._auto_scaling_group_is_missing(
                    autoscaling, name
                ):
                    raise

        self._wait_for_ecs_services_inactive(ecs, services_to_wait, deadline)

    @staticmethod
    def _target_group_is_missing(client: Any, arn: str) -> bool:
        try:
            targets = client.describe_target_groups(
                TargetGroupArns=[arn]
            ).get("TargetGroups", [])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == (
                "TargetGroupNotFound"
            ):
                return True
            raise
        return not targets

    @staticmethod
    def _ecs_service_is_missing(client: Any, cluster: str, arn: str) -> bool:
        response = client.describe_services(
            cluster=cluster,
            services=[arn],
        )
        return (
            not response.get("services", [])
            and len(response.get("failures", [])) == 1
            and response["failures"][0].get("reason") == "MISSING"
        )

    @staticmethod
    def _auto_scaling_group_is_missing(client: Any, name: str) -> bool:
        return not client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[name]
        ).get("AutoScalingGroups", [])

    def _wait_for_ecs_services_inactive(
        self,
        client: Any,
        services: dict[str, tuple[str, str]],
        deadline: float | None,
    ) -> None:
        pending = dict(services)
        for attempt in range(waiter_attempts(deadline, delay=6, maximum=50)):
            for service_arn, (cluster_name, service_name) in list(
                pending.items()
            ):
                self.observe_deadline(deadline)
                response = client.describe_services(
                    cluster=cluster_name,
                    services=[service_arn],
                )
                described = response.get("services", [])
                failures = response.get("failures", [])
                if (
                    not described
                    and len(failures) == 1
                    and failures[0].get("reason") == "MISSING"
                ):
                    pending.pop(service_arn)
                    continue
                if len(described) != 1 or failures:
                    raise RuntimeError(
                        f"ECS service deletion state is untrustworthy: {service_name}"
                    )
                service = described[0]
                if service.get("serviceArn") != service_arn:
                    raise RuntimeError(
                        f"ECS service deletion resolved a different ARN: {service_name}"
                    )
                if service.get("status") == "INACTIVE":
                    pending.pop(service_arn)
                elif service.get("status") != "DRAINING":
                    raise RuntimeError(
                        f"ECS service did not remain in deletion: {service_name}"
                    )
            if not pending:
                return
            if attempt < 49:
                time.sleep(6)
        raise RuntimeError(
            "exact ECS services did not become inactive before stack deletion: "
            + ", ".join(sorted(pending))
        )

    def cleanup_terminal_residuals(self, deadline: float | None = None) -> None:
        """Remove exact-owned terminal resources that CloudFormation leaves tagged.

        ECS keeps stopped tasks, inactive clusters and deregistered task definitions
        addressable, and EC2 keeps deleted NAT gateway tombstones visible. Those terminal
        resources are not returned by the active-service inventory, but their
        ownership tags keep the Resource Groups Tagging API inventory non-zero.
        Verify the exact run/session tags and terminal state before deleting the
        task definitions or removing the terminal-resource ownership tags.
        """
        tag_filters = [
            {"Key": key, "Values": [value]}
            for key, value in expected_tags(self.run_id, self.session_id).items()
        ]
        mappings = [
            item
            for page in self.client("resourcegroupstaggingapi").get_paginator(
                "get_resources"
            ).paginate(TagFilters=tag_filters)
            for item in page.get("ResourceTagMappingList", [])
        ]
        for item in mappings:
            if not tags_match(
                tag_map(item.get("Tags", [])), self.run_id, self.session_id
            ):
                raise RuntimeError(
                    "refusing terminal cleanup without exact run/session ownership"
                )

        ecs_prefix = f"arn:aws:ecs:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:"
        task_definitions = sorted(
            str(item["ResourceARN"])
            for item in mappings
            if str(item.get("ResourceARN", "")).startswith(
                f"{ecs_prefix}task-definition/"
            )
        )
        ecs = self.client("ecs")
        inactive_task_definitions: list[str] = []
        for arn in task_definitions:
            self.observe_deadline(deadline)
            response = ecs.describe_task_definition(
                taskDefinition=arn, include=["TAGS"]
            )
            definition = response.get("taskDefinition", {})
            if (
                definition.get("taskDefinitionArn") != arn
                or definition.get("status")
                not in {"INACTIVE", "DELETE_IN_PROGRESS"}
                or not tags_match(
                    tag_map(response.get("tags", [])), self.run_id, self.session_id
                )
            ):
                raise RuntimeError(
                    f"refusing non-terminal or non-owned task definition cleanup: {arn}"
                )
            if definition.get("status") == "INACTIVE":
                inactive_task_definitions.append(arn)
        for offset in range(0, len(inactive_task_definitions), 10):
            batch = inactive_task_definitions[offset:offset + 10]
            if not batch:
                continue
            self.observe_deadline(deadline)
            response = ecs.delete_task_definitions(taskDefinitions=batch)
            failures = response.get("failures", [])
            deleted = {
                item.get("taskDefinitionArn")
                for item in response.get("taskDefinitions", [])
            }
            if failures or deleted != set(batch):
                raise RuntimeError(
                    "exact inactive task definition deletion was not fully accepted"
                )

        clusters = sorted(
            str(item["ResourceARN"])
            for item in mappings
            if str(item.get("ResourceARN", "")).startswith(f"{ecs_prefix}cluster/")
        )
        ownership = expected_tags(self.run_id, self.session_id)
        for offset in range(0, len(clusters), 100):
            batch = clusters[offset:offset + 100]
            if not batch:
                continue
            self.observe_deadline(deadline)
            descriptions = ecs.describe_clusters(clusters=batch, include=["TAGS"])
            by_arn = {
                item.get("clusterArn"): item
                for item in descriptions.get("clusters", [])
            }
            for arn in batch:
                cluster = by_arn.get(arn, {})
                if (
                    cluster.get("status") != "INACTIVE"
                    or int(cluster.get("runningTasksCount", 0)) != 0
                    or int(cluster.get("pendingTasksCount", 0)) != 0
                    or int(cluster.get("activeServicesCount", 0)) != 0
                    or not tags_match(
                        tag_map(cluster.get("tags", [])), self.run_id, self.session_id
                    )
                ):
                    raise RuntimeError(
                        f"refusing non-terminal or non-owned cluster untag: {arn}"
                    )

                cluster_name = arn.rsplit("/", 1)[-1]
                recreated = ecs.create_cluster(
                    clusterName=cluster_name,
                    tags=[
                        {"key": key, "value": value}
                        for key, value in ownership.items()
                    ],
                ).get("cluster", {})
                if (
                    recreated.get("clusterArn") != arn
                    or recreated.get("status") != "ACTIVE"
                    or int(recreated.get("runningTasksCount", 0)) != 0
                    or int(recreated.get("pendingTasksCount", 0)) != 0
                    or int(recreated.get("activeServicesCount", 0)) != 0
                    or int(recreated.get("registeredContainerInstancesCount", 0))
                    != 0
                ):
                    raise RuntimeError(
                        f"terminal cluster could not be safely reactivated: {arn}"
                    )
                recreated_tags = tag_map(
                    ecs.list_tags_for_resource(resourceArn=arn).get("tags", [])
                )
                if not tags_match(
                    recreated_tags, self.run_id, self.session_id
                ):
                    raise RuntimeError(
                        f"reactivated cluster lost exact ownership: {arn}"
                    )
                ecs.untag_resource(
                    resourceArn=arn,
                    tagKeys=list(ownership),
                )
                remaining_tags = tag_map(
                    ecs.list_tags_for_resource(resourceArn=arn).get("tags", [])
                )
                if any(key in remaining_tags for key in ownership):
                    raise RuntimeError(
                        f"reactivated cluster retained ownership tags: {arn}"
                    )
                deleted = ecs.delete_cluster(cluster=arn).get("cluster", {})
                if (
                    deleted.get("clusterArn") != arn
                    or deleted.get("status") != "INACTIVE"
                    or int(deleted.get("runningTasksCount", 0)) != 0
                    or int(deleted.get("pendingTasksCount", 0)) != 0
                    or int(deleted.get("activeServicesCount", 0)) != 0
                    or int(deleted.get("registeredContainerInstancesCount", 0))
                    != 0
                ):
                    raise RuntimeError(
                        f"reactivated cluster did not return to terminal state: {arn}"
                    )

        nat_prefix = (
            f"arn:aws:ec2:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:natgateway/"
        )
        nat_ids = sorted(
            str(item["ResourceARN"]).removeprefix(nat_prefix)
            for item in mappings
            if str(item.get("ResourceARN", "")).startswith(nat_prefix)
        )
        ec2 = self.client("ec2")
        for nat_id in nat_ids:
            self.observe_deadline(deadline)
            try:
                descriptions = ec2.describe_nat_gateways(
                    NatGatewayIds=[nat_id]
                ).get("NatGateways", [])
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != (
                    "NatGatewayNotFound"
                ):
                    raise
                continue
            if (
                len(descriptions) != 1
                or descriptions[0].get("NatGatewayId") != nat_id
                or descriptions[0].get("State") != "deleted"
                or not tags_match(
                    tag_map(descriptions[0].get("Tags", [])),
                    self.run_id,
                    self.session_id,
                )
            ):
                raise RuntimeError(
                    f"refusing non-terminal or non-owned NAT gateway untag: {nat_id}"
                )

        endpoint_prefix = (
            f"arn:aws:ec2:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:vpc-endpoint/"
        )
        endpoint_arns = sorted(
            str(item["ResourceARN"])
            for item in mappings
            if str(item.get("ResourceARN", "")).startswith(endpoint_prefix)
        )
        endpoint_ids = [arn.removeprefix(endpoint_prefix) for arn in endpoint_arns]
        if endpoint_ids:
            try:
                endpoints = ec2.describe_vpc_endpoints(
                    VpcEndpointIds=endpoint_ids
                ).get("VpcEndpoints", [])
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != (
                    "InvalidVpcEndpointId.NotFound"
                ):
                    raise
            else:
                if endpoints:
                    raise RuntimeError(
                        "refusing to untag a VPC endpoint that still exists"
                    )

        instance_prefix = (
            f"arn:aws:ec2:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:instance/"
        )
        instance_arns = sorted(
            str(item["ResourceARN"])
            for item in mappings
            if str(item.get("ResourceARN", "")).startswith(instance_prefix)
        )
        for arn in instance_arns:
            self.observe_deadline(deadline)
            instance_id = arn.removeprefix(instance_prefix)
            try:
                reservations = ec2.describe_instances(
                    InstanceIds=[instance_id]
                ).get("Reservations", [])
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != (
                    "InvalidInstanceID.NotFound"
                ):
                    raise
                continue
            instances = [
                instance
                for reservation in reservations
                for instance in reservation.get("Instances", [])
            ]
            if (
                len(instances) != 1
                or instances[0].get("InstanceId") != instance_id
                or instances[0].get("State", {}).get("Name") != "terminated"
                or not tags_match(
                    tag_map(instances[0].get("Tags", [])),
                    self.run_id,
                    self.session_id,
                )
            ):
                raise RuntimeError(
                    f"refusing non-terminal or non-owned instance untag: {instance_id}"
                )

        volume_prefix = (
            f"arn:aws:ec2:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:volume/"
        )
        volume_arns = sorted(
            str(item["ResourceARN"])
            for item in mappings
            if str(item.get("ResourceARN", "")).startswith(volume_prefix)
        )
        for arn in volume_arns:
            self.observe_deadline(deadline)
            volume_id = arn.removeprefix(volume_prefix)
            try:
                volumes = ec2.describe_volumes(VolumeIds=[volume_id]).get(
                    "Volumes", []
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != (
                    "InvalidVolume.NotFound"
                ):
                    raise
                continue
            if volumes:
                raise RuntimeError(
                    f"refusing to untag an EBS volume that still exists: {volume_id}"
                )

        subnet_prefix = (
            f"arn:aws:ec2:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:subnet/"
        )
        subnet_arns = sorted(
            str(item["ResourceARN"])
            for item in mappings
            if str(item.get("ResourceARN", "")).startswith(subnet_prefix)
        )
        for arn in subnet_arns:
            self.observe_deadline(deadline)
            subnet_id = arn.removeprefix(subnet_prefix)
            try:
                subnets = ec2.describe_subnets(SubnetIds=[subnet_id]).get(
                    "Subnets", []
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != (
                    "InvalidSubnetID.NotFound"
                ):
                    raise
                continue
            if subnets:
                raise RuntimeError(
                    f"refusing to untag a subnet that still exists: {subnet_id}"
                )

        service_prefix = f"{ecs_prefix}service/"
        service_arns = sorted(
            str(item["ResourceARN"])
            for item in mappings
            if str(item.get("ResourceARN", "")).startswith(service_prefix)
        )
        missing_services: list[tuple[str, str, str]] = []
        for arn in service_arns:
            self.observe_deadline(deadline)
            parts = arn.removeprefix(service_prefix).split("/")
            if len(parts) != 2 or not all(parts):
                raise RuntimeError(f"refusing malformed ECS service ARN: {arn}")
            cluster_name, service_name = parts
            response = ecs.describe_services(
                cluster=cluster_name,
                services=[service_name],
                include=["TAGS"],
            )
            services = response.get("services", [])
            failures = response.get("failures", [])
            if not services and len(failures) == 1 and failures[0].get(
                "reason"
            ) == "MISSING":
                missing_services.append((arn, cluster_name, service_name))
                continue
            if (
                len(services) != 1
                or services[0].get("serviceArn") != arn
                or services[0].get("status") != "INACTIVE"
                or int(services[0].get("desiredCount", 0)) != 0
                or int(services[0].get("runningCount", 0)) != 0
                or int(services[0].get("pendingCount", 0)) != 0
                or not tags_match(
                    tag_map(services[0].get("tags", [])),
                    self.run_id,
                    self.session_id,
                )
            ):
                raise RuntimeError(
                    f"refusing non-terminal or non-owned ECS service untag: {arn}"
                )
            ecs.untag_resource(resourceArn=arn, tagKeys=list(ownership))
            remaining_tags = tag_map(
                ecs.list_tags_for_resource(resourceArn=arn).get("tags", [])
            )
            if any(key in remaining_tags for key in ownership):
                raise RuntimeError(
                    f"inactive ECS service retained ownership tags: {arn}"
                )

        # ECS rejects both direct and Tagging API untag operations for stopped
        # tasks. Their exact-owned tag mappings are immutable tombstones, not
        # live service inventory, and must age out under authoritative polling.
        stopped_task_prefix = f"{ecs_prefix}task/"
        stopped_task_arns = sorted(
            str(item["ResourceARN"])
            for item in mappings
            if str(item.get("ResourceARN", "")).startswith(stopped_task_prefix)
        )
        for arn in stopped_task_arns:
            parts = arn.removeprefix(stopped_task_prefix).split("/")
            if len(parts) != 2 or not all(parts):
                raise RuntimeError(f"refusing malformed stopped ECS task ARN: {arn}")

        classified = set(task_definitions + clusters + [
            f"{nat_prefix}{nat_id}" for nat_id in nat_ids
        ] + endpoint_arns + instance_arns + volume_arns + subnet_arns
            + service_arns + stopped_task_arns)
        residual_arns = {str(item["ResourceARN"]) for item in mappings}
        if classified != residual_arns:
            unexpected = sorted(residual_arns - classified)
            raise RuntimeError(
                f"refusing unrecognized terminal residual cleanup: {unexpected}"
            )

        ownership_keys = list(ownership)
        untag_mappings = [
            item
            for item in mappings
            if str(item["ResourceARN"])
            not in set(clusters + service_arns + stopped_task_arns)
        ]
        for offset in range(0, len(untag_mappings), 20):
            batch = sorted(
                str(item["ResourceARN"])
                for item in untag_mappings[offset:offset + 20]
            )
            if not batch:
                continue
            self.observe_deadline(deadline)
            response = self.client("resourcegroupstaggingapi").untag_resources(
                ResourceARNList=batch,
                TagKeys=ownership_keys,
            )
            failures = response.get("FailedResourcesMap", {})
            if failures:
                raise RuntimeError(
                    f"terminal Resource Groups untag failed: {failures}"
                )
        if missing_services:
            self.reactivate_missing_ecs_services(missing_services, deadline)

    def reactivate_missing_ecs_services(
        self,
        services: list[tuple[str, str, str]],
        deadline: float | None = None,
    ) -> None:
        """Remove stale tags from deleted ECS services without starting tasks."""
        ecs = self.client("ecs")
        ownership = expected_tags(self.run_id, self.session_id)
        tags = [
            {"key": key, "value": value}
            for key, value in ownership.items()
        ]
        family_suffix = (
            self.run_id.removeprefix("run_")
            .removesuffix("_phase7_integration")
            .replace("_", "-")
        )
        family = f"loopad-phase7-terminal-cleanup-{family_suffix}"
        self.observe_deadline(deadline)
        registered = ecs.register_task_definition(
            family=family,
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
            tags=tags,
        ).get("taskDefinition", {})
        task_arn = str(registered.get("taskDefinitionArn", ""))
        if not task_arn or registered.get("status") != "ACTIVE":
            raise RuntimeError(
                "terminal cleanup task definition registration was not accepted"
            )
        described = ecs.describe_task_definition(
            taskDefinition=task_arn, include=["TAGS"]
        )
        if (
            described.get("taskDefinition", {}).get("taskDefinitionArn") != task_arn
            or described.get("taskDefinition", {}).get("status") != "ACTIVE"
            or not tags_match(
                tag_map(described.get("tags", [])),
                self.run_id,
                self.session_id,
            )
        ):
            raise RuntimeError(
                "terminal cleanup task definition lost exact ownership"
            )

        by_cluster: dict[str, list[tuple[str, str]]] = {}
        for arn, cluster_name, service_name in services:
            by_cluster.setdefault(cluster_name, []).append((arn, service_name))

        for cluster_name, cluster_services in sorted(by_cluster.items()):
            self.observe_deadline(deadline)
            cluster_arn = (
                f"arn:aws:ecs:{EXPECTED_REGION}:{EXPECTED_ACCOUNT}:"
                f"cluster/{cluster_name}"
            )
            existing = ecs.describe_clusters(
                clusters=[cluster_name], include=["TAGS"]
            ).get("clusters", [])
            if existing and (
                len(existing) != 1
                or existing[0].get("clusterArn") != cluster_arn
                or existing[0].get("status") not in {"ACTIVE", "INACTIVE"}
                or int(existing[0].get("runningTasksCount", 0)) != 0
                or int(existing[0].get("pendingTasksCount", 0)) != 0
                or int(existing[0].get("activeServicesCount", 0)) != 0
                or int(existing[0].get("registeredContainerInstancesCount", 0))
                != 0
                or (
                    existing[0].get("status") == "ACTIVE"
                    and not tags_match(
                        tag_map(existing[0].get("tags", [])),
                        self.run_id,
                        self.session_id,
                    )
                )
            ):
                raise RuntimeError(
                    f"refusing nonempty or non-owned ECS cluster reactivation: {cluster_arn}"
                )
            cluster = ecs.create_cluster(
                clusterName=cluster_name,
                tags=tags,
            ).get("cluster", {})
            if (
                cluster.get("clusterArn") != cluster_arn
                or cluster.get("status") != "ACTIVE"
                or int(cluster.get("runningTasksCount", 0)) != 0
                or int(cluster.get("pendingTasksCount", 0)) != 0
                or int(cluster.get("activeServicesCount", 0)) != 0
                or int(cluster.get("registeredContainerInstancesCount", 0)) != 0
                or not tags_match(
                    tag_map(
                        ecs.list_tags_for_resource(
                            resourceArn=cluster_arn
                        ).get("tags", [])
                    ),
                    self.run_id,
                    self.session_id,
                )
            ):
                raise RuntimeError(
                    f"terminal cleanup cluster could not be safely reactivated: {cluster_arn}"
                )

            service_names: list[str] = []
            for service_arn, service_name in sorted(cluster_services):
                self.observe_deadline(deadline)
                service = ecs.create_service(
                    cluster=cluster_name,
                    serviceName=service_name,
                    taskDefinition=task_arn,
                    desiredCount=0,
                    launchType="EC2",
                    tags=tags,
                ).get("service", {})
                if (
                    service.get("serviceArn") != service_arn
                    or service.get("status") != "ACTIVE"
                    or int(service.get("desiredCount", 0)) != 0
                    or int(service.get("runningCount", 0)) != 0
                    or int(service.get("pendingCount", 0)) != 0
                    or not tags_match(
                        tag_map(
                            ecs.list_tags_for_resource(
                                resourceArn=service_arn
                            ).get("tags", [])
                        ),
                        self.run_id,
                        self.session_id,
                    )
                ):
                    raise RuntimeError(
                        f"terminal cleanup service was not recreated exactly: {service_arn}"
                    )
                ecs.untag_resource(
                    resourceArn=service_arn,
                    tagKeys=list(ownership),
                )
                remaining = tag_map(
                    ecs.list_tags_for_resource(resourceArn=service_arn).get(
                        "tags", []
                    )
                )
                if any(key in remaining for key in ownership):
                    raise RuntimeError(
                        f"terminal cleanup service retained ownership: {service_arn}"
                    )
                deleted = ecs.delete_service(
                    cluster=cluster_name,
                    service=service_name,
                    force=True,
                ).get("service", {})
                if (
                    deleted.get("serviceArn") != service_arn
                    or deleted.get("status") not in {"DRAINING", "INACTIVE"}
                    or int(deleted.get("desiredCount", 0)) != 0
                    or int(deleted.get("runningCount", 0)) != 0
                    or int(deleted.get("pendingCount", 0)) != 0
                ):
                    raise RuntimeError(
                        f"terminal cleanup service did not enter deletion: {service_arn}"
                    )
                service_names.append(service_name)
            if service_names:
                self.observe_deadline(deadline)
                ecs.get_waiter("services_inactive").wait(
                    cluster=cluster_name,
                    services=service_names,
                    WaiterConfig={
                        "Delay": 6,
                        "MaxAttempts": waiter_attempts(
                            deadline, delay=6, maximum=50
                        ),
                    },
                )
            ecs.untag_resource(
                resourceArn=cluster_arn,
                tagKeys=list(ownership),
            )
            remaining = tag_map(
                ecs.list_tags_for_resource(resourceArn=cluster_arn).get(
                    "tags", []
                )
            )
            if any(key in remaining for key in ownership):
                raise RuntimeError(
                    f"terminal cleanup cluster retained ownership: {cluster_arn}"
                )
            deleted_cluster = ecs.delete_cluster(cluster=cluster_arn).get(
                "cluster", {}
            )
            if (
                deleted_cluster.get("clusterArn") != cluster_arn
                or deleted_cluster.get("status") != "INACTIVE"
                or int(deleted_cluster.get("runningTasksCount", 0)) != 0
                or int(deleted_cluster.get("pendingTasksCount", 0)) != 0
                or int(deleted_cluster.get("activeServicesCount", 0)) != 0
                or int(
                    deleted_cluster.get("registeredContainerInstancesCount", 0)
                ) != 0
            ):
                raise RuntimeError(
                    f"terminal cleanup cluster did not return to terminal state: {cluster_arn}"
                )

        ecs.untag_resource(
            resourceArn=task_arn,
            tagKeys=list(ownership),
        )
        remaining = tag_map(
            ecs.list_tags_for_resource(resourceArn=task_arn).get("tags", [])
        )
        if any(key in remaining for key in ownership):
            raise RuntimeError(
                "terminal cleanup task definition retained ownership tags"
            )
        deregistered = ecs.deregister_task_definition(
            taskDefinition=task_arn
        ).get("taskDefinition", {})
        if (
            deregistered.get("taskDefinitionArn") != task_arn
            or deregistered.get("status") != "INACTIVE"
        ):
            raise RuntimeError(
                "terminal cleanup task definition did not deregister"
            )
        deleted = ecs.delete_task_definitions(
            taskDefinitions=[task_arn]
        )
        if (
            deleted.get("failures", [])
            or {
                item.get("taskDefinitionArn")
                for item in deleted.get("taskDefinitions", [])
            } != {task_arn}
        ):
            raise RuntimeError(
                "terminal cleanup task definition deletion was not accepted"
            )

    def stop_archive_tasks(self, outputs: dict[str, str], deadline: float | None = None) -> None:
        cluster = outputs.get("ArchiveClusterName")
        task_definition = outputs.get("ArchiveTaskDefinitionArn")
        if not cluster or not task_definition:
            raise RuntimeError("owned runtime stack is missing archive task cleanup outputs")
        client = self.client("ecs")
        definition = client.describe_task_definition(taskDefinition=task_definition).get("taskDefinition", {})
        family = definition.get("family")
        if definition.get("taskDefinitionArn") != task_definition or not family:
            raise RuntimeError("owned archive task definition cannot be resolved exactly")
        task_arns = sorted({
            arn
            for status in ("RUNNING", "PENDING", "STOPPED")
            for page in client.get_paginator("list_tasks").paginate(
                cluster=cluster, family=family, desiredStatus=status
            )
            for arn in page.get("taskArns", [])
        })

        def describe_exact(batch: list[str]) -> dict[str, dict[str, Any]]:
            response = client.describe_tasks(
                cluster=cluster, tasks=batch, include=["TAGS"]
            )
            descriptions = response.get("tasks", [])
            by_arn = {str(task.get("taskArn")): task for task in descriptions}
            if response.get("failures") or set(by_arn) != set(batch):
                raise RuntimeError("owned archive task inventory is incomplete")
            for arn, task in by_arn.items():
                if (
                    task.get("taskDefinitionArn") != task_definition
                    or not tags_match(
                        tag_map(task.get("tags", [])), self.run_id, self.session_id
                    )
                ):
                    raise RuntimeError(
                        f"refusing a task outside the exact archive ownership: {arn}"
                    )
            return by_arn

        described: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(task_arns), 100):
            batch = task_arns[offset:offset + 100]
            described.update(describe_exact(batch))
        active_task_arns = sorted(
            arn for arn, task in described.items() if task.get("lastStatus") != "STOPPED"
        )
        for task_arn in active_task_arns:
            task = described[task_arn]
            if task.get("lastStatus") not in {
                "PROVISIONING", "PENDING", "ACTIVATING", "RUNNING",
                "DEACTIVATING", "STOPPING", "DEPROVISIONING",
            }:
                raise RuntimeError(
                    f"owned archive task has an unsafe cleanup state: {task_arn}"
                )
            client.stop_task(
                cluster=cluster,
                task=task_arn,
                reason=f"Phase 7 exact-owned cleanup for {self.run_id}",
            )
        if active_task_arns:
            self.observe_deadline(deadline)
            attempts = waiter_attempts(deadline, delay=6, maximum=50)
            client.get_waiter("tasks_stopped").wait(
                cluster=cluster,
                tasks=active_task_arns,
                WaiterConfig={"Delay": 6, "MaxAttempts": attempts},
            )
            self.observe_deadline(deadline)

        ownership_keys = list(expected_tags(self.run_id, self.session_id))
        for offset in range(0, len(task_arns), 100):
            batch = task_arns[offset:offset + 100]
            stopped = describe_exact(batch)
            for task_arn, task in stopped.items():
                if task.get("lastStatus") != "STOPPED":
                    raise RuntimeError(
                        f"owned archive task did not stop before untag: {task_arn}"
                    )
                try:
                    client.untag_resource(
                        resourceArn=task_arn,
                        tagKeys=ownership_keys,
                    )
                except ClientError as error:
                    aws_error = error.response.get("Error", {})
                    if not (
                        aws_error.get("Code") == "InvalidParameterException"
                        and "specified task is stopped"
                        in str(aws_error.get("Message", "")).lower()
                    ):
                        raise
                    # ECS does not permit tag mutation after a task reaches
                    # STOPPED. Continue exact-owned stack deletion and let the
                    # authoritative Tagging API inventory track the temporary
                    # stopped-task tombstone until ECS expires it.
                    continue
                remaining = tag_map(
                    client.list_tags_for_resource(resourceArn=task_arn).get("tags", [])
                )
                if any(key in remaining for key in ownership_keys):
                    raise RuntimeError(
                        f"owned archive task retained cleanup tags: {task_arn}"
                    )

    def delete_stack(
        self,
        name: str,
        deadline: float | None = None,
        *,
        already_deleting: bool = False,
    ) -> None:
        self.observe_deadline(deadline)
        client = self.client("cloudformation")
        if not already_deleting:
            client.delete_stack(StackName=name)
        attempts = waiter_attempts(deadline, delay=15, maximum=80)
        client.get_waiter("stack_delete_complete").wait(
            StackName=name,
            WaiterConfig={"Delay": 15, "MaxAttempts": attempts},
        )
        self.observe_deadline(deadline)

    def empty_bucket(
        self, bucket: str, deadline: float | None = None, *, expected_name: str
    ) -> None:
        if bucket != expected_name:
            raise RuntimeError("refusing S3 cleanup for a bucket outside the exact stack output")
        client = self.client("s3")
        tags = tag_map(client.get_bucket_tagging(Bucket=bucket).get("TagSet", []))
        if not tags_match(tags, self.run_id, self.session_id):
            raise RuntimeError(f"refusing non-owned S3 bucket cleanup: {bucket}")
        while True:
            self.observe_deadline(deadline)
            response = client.list_object_versions(Bucket=bucket, MaxKeys=1000)
            objects = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for key in ("Versions", "DeleteMarkers") for item in response.get(key, [])
            ]
            if objects:
                client.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
            if not response.get("IsTruncated") and not objects:
                break

    def empty_repository(
        self, repository: str, deadline: float | None = None, *, expected_name: str
    ) -> None:
        if repository != expected_name:
            raise RuntimeError("refusing ECR cleanup for a repository outside the exact role name")
        self.observe_deadline(deadline)
        client = self.client("ecr")
        descriptions = client.describe_repositories(repositoryNames=[repository]).get(
            "repositories", []
        )
        if len(descriptions) != 1 or descriptions[0].get("repositoryName") != repository:
            raise RuntimeError(f"exact ECR repository cannot be resolved: {repository}")
        repository_arn = descriptions[0].get("repositoryArn")
        if not isinstance(repository_arn, str) or not repository_arn:
            raise RuntimeError(f"exact ECR repository ARN is missing: {repository}")
        tags = tag_map(
            client.list_tags_for_resource(resourceArn=repository_arn).get("tags", [])
        )
        if not tags_match(tags, self.run_id, self.session_id):
            raise RuntimeError(f"refusing non-owned ECR repository cleanup: {repository}")
        digests = sorted({
            image["imageDigest"]
            for page in client.get_paginator("describe_images").paginate(repositoryName=repository)
            for image in page.get("imageDetails", [])
            if image.get("imageDigest")
        })
        for offset in range(0, len(digests), 100):
            self.observe_deadline(deadline)
            client.batch_delete_image(repositoryName=repository, imageIds=[{"imageDigest": digest} for digest in digests[offset:offset + 100]])

    def observe_deadline(self, deadline: float | None) -> bool:
        expired = deadline is not None and time.monotonic() >= deadline
        self.deadline_breached = bool(
            getattr(self, "deadline_breached", False) or expired
        )
        return expired

    def inventory(self) -> dict[str, Any]:
        identity = self.assert_identity()
        tag_filters = [{"Key": key, "Values": [value]} for key, value in expected_tags(self.run_id, self.session_id).items()]
        tagging = sorted({
            item["ResourceARN"]
            for page in self.client("resourcegroupstaggingapi").get_paginator("get_resources").paginate(TagFilters=tag_filters)
            for item in page.get("ResourceTagMappingList", [])
        })
        resources = {
            "cloudFormationStacks": [name for name in (RUNTIME_STACK_NAME, IMAGE_STACK_NAME) if self.stack(name) is not None],
            "ec2Instances": self._ec2("describe_instances", "Instances", "InstanceId", nested=True, states=True),
            "ebsVolumes": self._ec2("describe_volumes", "Volumes", "VolumeId"),
            "ebsSnapshots": self._ec2("describe_snapshots", "Snapshots", "SnapshotId", owner=True),
            "networkInterfaces": self._ec2("describe_network_interfaces", "NetworkInterfaces", "NetworkInterfaceId"),
            "natGateways": self._ec2(
                "describe_nat_gateways",
                "NatGateways",
                "NatGatewayId",
                excluded_states={"deleted"},
            ),
            "vpcs": self._ec2("describe_vpcs", "Vpcs", "VpcId"),
            "subnets": self._ec2("describe_subnets", "Subnets", "SubnetId"),
            "routeTables": self._ec2("describe_route_tables", "RouteTables", "RouteTableId"),
            "internetGateways": self._ec2("describe_internet_gateways", "InternetGateways", "InternetGatewayId"),
            "securityGroups": self._ec2("describe_security_groups", "SecurityGroups", "GroupId"),
            "vpcEndpoints": self._ec2("describe_vpc_endpoints", "VpcEndpoints", "VpcEndpointId"),
            "launchTemplates": self._ec2("describe_launch_templates", "LaunchTemplates", "LaunchTemplateId"),
            "elasticIpAllocations": self._elastic_ips(),
            "autoScalingGroups": self._auto_scaling_groups(),
            "ecsClusters": self._ecs_clusters(),
            "ecsServices": self._ecs_services(),
            "ecsTasks": self._ecs_tasks(),
            "ecsContainerInstances": self._ecs_container_instances(),
            "ecsTaskDefinitions": self._ecs_task_definitions(),
            "ecsCapacityProviders": self._ecs_capacity_providers(),
            "loadBalancers": self._load_balancers(),
            "targetGroups": self._target_groups(),
            "listeners": self._listeners(),
            "s3Buckets": self._s3_buckets(),
            "secrets": self._secrets(),
            "cloudMapNamespaces": self._cloud_map("list_namespaces", "Namespaces"),
            "cloudMapServices": self._cloud_map("list_services", "Services"),
            "cloudWatchAlarms": self._cloudwatch_alarms(),
            "ecrRepositories": self._ecr_repositories(),
            "ecrImages": self._ecr_images(),
            "kinesisStreams": self._kinesis_streams(),
            "dynamoDbTables": self._dynamodb_tables(),
            "logGroups": self._log_groups(),
            "iamRoles": self._iam_roles(),
        }
        return inventory_result(identity, self.run_id, self.session_id, resources, tagging)

    def _elastic_ips(self) -> list[str]:
        filters = [
            {"Name": f"tag:{key}", "Values": [value]}
            for key, value in expected_tags(self.run_id, self.session_id).items()
        ]
        return sorted(
            str(item["AllocationId"])
            for item in self.client("ec2").describe_addresses(Filters=filters).get("Addresses", [])
            if item.get("AllocationId")
        )

    def _ec2(self, operation: str, result_key: str, id_key: str, *, nested: bool = False,
             states: bool = False, owner: bool = False,
             excluded_states: set[str] | None = None) -> list[str]:
        filters = [{"Name": f"tag:{key}", "Values": [value]} for key, value in expected_tags(self.run_id, self.session_id).items()]
        kwargs: dict[str, Any] = {"Filters": filters}
        if owner:
            kwargs["OwnerIds"] = ["self"]
        pages = self.client("ec2").get_paginator(operation).paginate(**kwargs)
        items = []
        for page in pages:
            candidates = [instance for reservation in page.get("Reservations", []) for instance in reservation.get("Instances", [])] if nested else page.get(result_key, [])
            for item in candidates:
                if states and item.get("State", {}).get("Name") == "terminated":
                    continue
                if excluded_states and item.get("State") in excluded_states:
                    continue
                items.append(str(item[id_key]))
        return sorted(items)

    def _auto_scaling_groups(self) -> list[str]:
        result = []
        for page in self.client("autoscaling").get_paginator("describe_auto_scaling_groups").paginate():
            for group in page.get("AutoScalingGroups", []):
                if tags_match(tag_map(group.get("Tags", [])), self.run_id, self.session_id):
                    result.append(group["AutoScalingGroupName"])
        return sorted(result)

    def _ecs_clusters(self) -> list[str]:
        client = self.client("ecs")
        result = []
        for page in client.get_paginator("list_clusters").paginate():
            for arn in page.get("clusterArns", []):
                tags = tag_map(client.list_tags_for_resource(resourceArn=arn).get("tags", []))
                if tags_match(tags, self.run_id, self.session_id):
                    result.append(arn)
        return sorted(result)

    def _ecs_services(self) -> list[str]:
        client = self.client("ecs")
        result = []
        for cluster in self._ecs_clusters():
            for page in client.get_paginator("list_services").paginate(cluster=cluster):
                for arn in page.get("serviceArns", []):
                    tags = tag_map(client.list_tags_for_resource(resourceArn=arn).get("tags", []))
                    if tags_match(tags, self.run_id, self.session_id):
                        result.append(arn)
        return sorted(result)

    def _ecs_tasks(self) -> list[str]:
        client = self.client("ecs")
        return sorted({
            arn
            for cluster in self._ecs_clusters()
            for status in ("RUNNING", "PENDING")
            for page in client.get_paginator("list_tasks").paginate(
                cluster=cluster, desiredStatus=status
            )
            for arn in page.get("taskArns", [])
        })

    def _ecs_container_instances(self) -> list[str]:
        client = self.client("ecs")
        return sorted({
            arn
            for cluster in self._ecs_clusters()
            for page in client.get_paginator("list_container_instances").paginate(cluster=cluster)
            for arn in page.get("containerInstanceArns", [])
        })

    def _ecs_task_definitions(self) -> list[str]:
        client = self.client("ecs")
        result = []
        for page in client.get_paginator("list_task_definitions").paginate(status="ACTIVE"):
            for arn in page.get("taskDefinitionArns", []):
                tags = tag_map(client.list_tags_for_resource(resourceArn=arn).get("tags", []))
                if tags_match(tags, self.run_id, self.session_id):
                    result.append(arn)
        return sorted(set(result))

    def _ecs_capacity_providers(self) -> list[str]:
        client = self.client("ecs")
        result = []
        next_token: str | None = None
        while True:
            request: dict[str, Any] = {"include": ["TAGS"], "maxResults": 100}
            if next_token:
                request["nextToken"] = next_token
            page = client.describe_capacity_providers(**request)
            for provider in page.get("capacityProviders", []):
                if tags_match(tag_map(provider.get("tags", [])), self.run_id, self.session_id):
                    result.append(str(provider["capacityProviderArn"]))
            next_token = page.get("nextToken")
            if not next_token:
                break
        return sorted(result)

    def _load_balancers(self) -> list[str]:
        client = self.client("elbv2")
        load_balancers = [
            item
            for page in client.get_paginator("describe_load_balancers").paginate()
            for item in page.get("LoadBalancers", [])
        ]
        result = []
        for offset in range(0, len(load_balancers), 20):
            batch = load_balancers[offset:offset + 20]
            if not batch:
                continue
            descriptions = client.describe_tags(ResourceArns=[item["LoadBalancerArn"] for item in batch])
            for description in descriptions.get("TagDescriptions", []):
                if tags_match(tag_map(description.get("Tags", [])), self.run_id, self.session_id):
                    result.append(description["ResourceArn"])
        return sorted(result)

    def _target_groups(self) -> list[str]:
        client = self.client("elbv2")
        targets = [
            item["TargetGroupArn"]
            for page in client.get_paginator("describe_target_groups").paginate()
            for item in page.get("TargetGroups", [])
        ]
        return self._tagged_elbv2_resources(targets)

    def _listeners(self) -> list[str]:
        client = self.client("elbv2")
        listeners = [
            item["ListenerArn"]
            for load_balancer in self._load_balancers()
            for page in client.get_paginator("describe_listeners").paginate(
                LoadBalancerArn=load_balancer
            )
            for item in page.get("Listeners", [])
        ]
        return self._tagged_elbv2_resources(listeners)

    def _tagged_elbv2_resources(self, arns: list[str]) -> list[str]:
        client = self.client("elbv2")
        result = []
        for offset in range(0, len(arns), 20):
            batch = arns[offset:offset + 20]
            if not batch:
                continue
            for description in client.describe_tags(ResourceArns=batch).get("TagDescriptions", []):
                if tags_match(tag_map(description.get("Tags", [])), self.run_id, self.session_id):
                    result.append(description["ResourceArn"])
        return sorted(result)

    def _s3_buckets(self) -> list[str]:
        client = self.client("s3")
        result = []
        for bucket in client.list_buckets().get("Buckets", []):
            name = bucket["Name"]
            try:
                tags = tag_map(client.get_bucket_tagging(Bucket=name).get("TagSet", []))
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                if code in {"NoSuchTagSet", "NoSuchBucket"}:
                    continue
                raise
            if tags_match(tags, self.run_id, self.session_id):
                result.append(name)
        return sorted(result)

    def _secrets(self) -> list[str]:
        client = self.client("secretsmanager")
        filters = [{"Key": "tag-key", "Values": sorted(expected_tags(self.run_id, self.session_id))}]
        result = []
        for page in client.get_paginator("list_secrets").paginate(Filters=filters, IncludePlannedDeletion=True):
            for secret in page.get("SecretList", []):
                if tags_match(tag_map(secret.get("Tags", [])), self.run_id, self.session_id):
                    result.append(secret["ARN"])
        return sorted(result)

    def _cloud_map(self, operation: str, result_key: str) -> list[str]:
        client = self.client("servicediscovery")
        result = []
        for page in client.get_paginator(operation).paginate():
            for resource in page.get(result_key, []):
                arn = resource["Arn"]
                tags = tag_map(client.list_tags_for_resource(ResourceARN=arn).get("Tags", []))
                if tags_match(tags, self.run_id, self.session_id):
                    result.append(arn)
        return sorted(result)

    def _cloudwatch_alarms(self) -> list[str]:
        client = self.client("cloudwatch")
        result = []
        for page in client.get_paginator("describe_alarms").paginate():
            alarms = [*page.get("MetricAlarms", []), *page.get("CompositeAlarms", [])]
            for alarm in alarms:
                arn = alarm["AlarmArn"]
                tags = tag_map(client.list_tags_for_resource(ResourceARN=arn).get("Tags", []))
                if tags_match(tags, self.run_id, self.session_id):
                    result.append(arn)
        return sorted(result)

    def _ecr_repositories(self) -> list[str]:
        prefix = f"loop-ad/perf-phase7/{self.run_id}/"
        return sorted(repository["repositoryName"] for page in self.client("ecr").get_paginator("describe_repositories").paginate()
                      for repository in page.get("repositories", []) if repository["repositoryName"].startswith(prefix))

    def _ecr_images(self) -> list[str]:
        client = self.client("ecr")
        return sorted(
            f"{repository}@{image['imageDigest']}"
            for repository in self._ecr_repositories()
            for page in client.get_paginator("describe_images").paginate(repositoryName=repository)
            for image in page.get("imageDetails", [])
            if image.get("imageDigest")
        )

    def _kinesis_streams(self) -> list[str]:
        client = self.client("kinesis")
        result = []
        for page in client.get_paginator("list_streams").paginate():
            for name in page.get("StreamNames", []):
                tags = []
                exclusive_start_tag_key: str | None = None
                while True:
                    request: dict[str, Any] = {"StreamName": name, "Limit": 50}
                    if exclusive_start_tag_key is not None:
                        request["ExclusiveStartTagKey"] = exclusive_start_tag_key
                    tag_page = client.list_tags_for_stream(**request)
                    page_tags = tag_page.get("Tags", [])
                    tags.extend(page_tags)
                    if not tag_page.get("HasMoreTags"):
                        break
                    next_key = str(page_tags[-1].get("Key", "")) if page_tags else ""
                    if not next_key or next_key == exclusive_start_tag_key:
                        raise RuntimeError(f"Kinesis tag pagination did not advance: {name}")
                    exclusive_start_tag_key = next_key
                if tags_match(tag_map(tags), self.run_id, self.session_id):
                    result.append(name)
        return sorted(result)

    def _dynamodb_tables(self) -> list[str]:
        client = self.client("dynamodb")
        result = []
        for page in client.get_paginator("list_tables").paginate():
            for name in page.get("TableNames", []):
                if name.startswith(self.run_id):
                    table = client.describe_table(TableName=name)["Table"]
                    tags = tag_map(client.list_tags_of_resource(ResourceArn=table["TableArn"]).get("Tags", []))
                    if tags_match(tags, self.run_id, self.session_id):
                        result.append(name)
        return sorted(result)

    def _log_groups(self) -> list[str]:
        prefix = f"/loopad/perf/phase7/{self.run_id}/"
        return sorted(group["logGroupName"] for page in self.client("logs").get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix)
                      for group in page.get("logGroups", []))

    def _iam_roles(self) -> list[str]:
        client = self.client("iam")
        result = []
        for page in client.get_paginator("list_roles").paginate():
            for role in page.get("Roles", []):
                tags = tag_map(client.list_role_tags(RoleName=role["RoleName"]).get("Tags", []))
                if tags_match(tags, self.run_id, self.session_id):
                    result.append(role["RoleName"])
        return sorted(result)


def inventory_result(identity: dict[str, str], run_id: str, session_id: str,
                     resources: dict[str, list[str]], tagging: list[str]) -> dict[str, Any]:
    counts = {key: len(value) for key, value in resources.items()}
    service_inventory_zero = all(count == 0 for count in counts.values())
    tagging_residuals_zero = len(tagging) == 0
    return {
        "schemaVersion": 1,
        "workload": "phase7-end-to-end-integration",
        "generatedAt": utc_now(),
        "identity": identity,
        "runId": run_id,
        "sessionId": session_id,
        "counts": counts,
        "resources": resources,
        "taggingApiResiduals": tagging,
        "taggingApiAuthoritative": False,
        "serviceInventoryZero": service_inventory_zero,
        "taggingApiResidualsZero": tagging_residuals_zero,
        "allZero": service_inventory_zero and tagging_residuals_zero,
    }


def waiter_attempts(deadline: float | None, *, delay: int, maximum: int) -> int:
    if delay <= 0 or maximum <= 0:
        raise ValueError("waiter delay and maximum attempts must be positive")
    # The deadline is a verdict boundary, not permission to abandon safe
    # recovery. The outer runner watchdog remains the process-level ceiling.
    return maximum


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    cleanup = Cleanup(args.run_id, args.session_id)
    deadline = time.monotonic() + max(1, args.timeout_seconds)
    if args.execute:
        cleanup.execute(deadline)
    while True:
        cleanup.observe_deadline(deadline)
        result = cleanup.inventory()
        cleanup.observe_deadline(deadline)
        result["cleanupDeadlineBreached"] = cleanup.deadline_breached
        result["recoveryContinuedAfterDeadline"] = cleanup.deadline_breached
        result["passEligible"] = result["allZero"] and not cleanup.deadline_breached
        write_json(args.output, result)
        if result["allZero"] or not args.execute:
            break
        time.sleep(max(1, args.poll_seconds))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passEligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

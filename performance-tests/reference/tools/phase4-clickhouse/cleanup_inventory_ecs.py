#!/usr/bin/env python3
"""Read-only service inventory before and after Phase 4 ECS cleanup."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from ecs_run_support import (
    AwsRun,
    RUN_ID_PATTERN,
    SESSION_ID_PATTERN,
    RunBundle,
    write_private,
)


RUNTIME_STACK_NAME = "LoopAdPerfPhase4ClickHouseEcsStack"
IMAGE_STACK_NAME = "LoopAdPerfPhase4ClickHouseEcsImageStack"


class Inventory:
    def __init__(self, bundle: RunBundle) -> None:
        self.bundle = bundle
        self.aws = AwsRun(bundle)

    def collect(self) -> dict[str, Any]:
        identity = self.aws.assert_identity()
        tagging_api_resources = self._tagging_api()
        resources = {
            "cloudFormationStacks": self._cloudformation(),
            **self._ecs(),
            "autoScalingGroups": self._autoscaling(),
            "launchTemplates": self._launch_templates(),
            "ec2Instances": self._ec2_instances(),
            "ebsVolumes": self._ec2_tagged("describe_volumes", "Volumes", "VolumeId"),
            "vpcs": self._ec2_tagged("describe_vpcs", "Vpcs", "VpcId"),
            "subnets": self._ec2_tagged("describe_subnets", "Subnets", "SubnetId"),
            "routeTables": self._ec2_tagged("describe_route_tables", "RouteTables", "RouteTableId"),
            "internetGateways": self._ec2_tagged("describe_internet_gateways", "InternetGateways", "InternetGatewayId"),
            "securityGroups": self._ec2_tagged("describe_security_groups", "SecurityGroups", "GroupId"),
            "networkInterfaces": self._ec2_tagged("describe_network_interfaces", "NetworkInterfaces", "NetworkInterfaceId"),
            "vpcEndpoints": self._ec2_tagged("describe_vpc_endpoints", "VpcEndpoints", "VpcEndpointId"),
            "kinesisStreams": self._kinesis(),
            "dynamoDbTables": self._dynamodb(),
            **self._ecr(),
            **self._s3(),
            "logGroups": self._logs(),
            "secrets": self._secrets(),
            "cloudWatchAlarms": self._alarms(),
            "iamRoles": self._iam_roles(),
            "iamInstanceProfiles": self._instance_profiles(),
        }
        counts = {name: len(items) for name, items in resources.items()}
        return {
            "schemaVersion": 1,
            "workload": "phase4-kinesis-ecs-ec2-clickhouse",
            "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "readOnly": True,
            "account": identity["account"],
            "region": "ap-northeast-2",
            "identity": {"arn": identity["arn"]},
            "ownership": {"runId": self.bundle.run_id, "sessionId": self.bundle.session_id},
            "counts": counts,
            "resources": resources,
            "eventuallyConsistentObservations": {
                "taggingApiResources": tagging_api_resources,
                "authoritative": False,
                "detail": (
                    "Resource Groups Tagging API can retain deleted ARNs after every "
                    "service-specific inventory has reached zero."
                ),
            },
            "allZero": all(count == 0 for count in counts.values()),
        }

    def _cloudformation(self) -> list[dict[str, str]]:
        client = self.aws.client("cloudformation")
        result: list[dict[str, str]] = []
        for name in [RUNTIME_STACK_NAME, IMAGE_STACK_NAME]:
            try:
                stack = client.describe_stacks(StackName=name)["Stacks"][0]
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") == "ValidationError":
                    continue
                raise
            if stack_owned(client, stack, self.bundle.run_id, self.bundle.session_id):
                result.append({"stackName": stack["StackName"], "status": stack["StackStatus"]})
        return result

    def _tagging_api(self) -> list[str]:
        paginator = self.aws.client("resourcegroupstaggingapi").get_paginator("get_resources")
        arns = [
            item["ResourceARN"]
            for page in paginator.paginate(TagFilters=self._tag_filters())
            for item in page["ResourceTagMappingList"]
        ]
        return sorted(set(arns))

    def _ecs(self) -> dict[str, list[str]]:
        ecs = self.aws.client("ecs")
        cluster_arn = self.bundle.outputs.get("ConsumerClusterName", "")
        clusters = ecs.describe_clusters(clusters=[cluster_arn], include=["TAGS"])["clusters"]
        owned_clusters = [
            cluster for cluster in clusters
            if cluster.get("status") == "ACTIVE"
            and self._owned(tag_map(cluster.get("tags", [])))
        ]
        if not owned_clusters:
            return {
                "ecsClusters": [],
                "ecsServices": [],
                "ecsTasks": [],
                "ecsContainerInstances": [],
                "ecsCapacityProviders": self._capacity_providers(),
            }
        service_arns = [
            arn
            for page in ecs.get_paginator("list_services").paginate(cluster=cluster_arn)
            for arn in page.get("serviceArns", [])
        ]
        services: list[str] = []
        for offset in range(0, len(service_arns), 10):
            services.extend(
                service["serviceArn"]
                for service in ecs.describe_services(
                    cluster=cluster_arn,
                    services=service_arns[offset:offset + 10],
                    include=["TAGS"],
                )["services"]
                if self._owned(tag_map(service.get("tags", [])))
            )
        task_arns = [
            arn
            for page in ecs.get_paginator("list_tasks").paginate(cluster=cluster_arn)
            for arn in page.get("taskArns", [])
        ]
        container_arns = [
            arn
            for page in ecs.get_paginator("list_container_instances").paginate(cluster=cluster_arn)
            for arn in page.get("containerInstanceArns", [])
        ]
        return {
            "ecsClusters": [cluster["clusterArn"] for cluster in owned_clusters],
            "ecsServices": sorted(services),
            "ecsTasks": sorted(task_arns),
            "ecsContainerInstances": sorted(container_arns),
            "ecsCapacityProviders": self._capacity_providers(),
        }

    def _capacity_providers(self) -> list[str]:
        name = self.bundle.outputs.get("ConsumerCapacityProviderName")
        client = self.aws.client("ecs")
        if name:
            responses = [client.describe_capacity_providers(
                capacityProviders=[name],
                include=["TAGS"],
            )]
        else:
            responses = []
            token: str | None = None
            while True:
                kwargs: dict[str, Any] = {"include": ["TAGS"], "maxResults": 100}
                if token:
                    kwargs["nextToken"] = token
                response = client.describe_capacity_providers(**kwargs)
                responses.append(response)
                token = response.get("nextToken")
                if not token:
                    break
        return sorted({
            item["name"]
            for response in responses
            for item in response.get("capacityProviders", [])
            if self._owned(tag_map(item.get("tags", [])))
        })

    def _autoscaling(self) -> list[str]:
        name = self.bundle.outputs.get("ConsumerAutoScalingGroupName")
        if not name:
            return []
        groups = self.aws.client("autoscaling").describe_auto_scaling_groups(
            AutoScalingGroupNames=[name],
        )["AutoScalingGroups"]
        return sorted(
            group["AutoScalingGroupName"]
            for group in groups
            if self._owned(tag_map(group.get("Tags", [])))
        )

    def _launch_templates(self) -> list[str]:
        paginator = self.aws.client("ec2").get_paginator("describe_launch_templates")
        return sorted(
            template["LaunchTemplateId"]
            for page in paginator.paginate(Filters=self._ec2_filters())
            for template in page.get("LaunchTemplates", [])
        )

    def _ec2_instances(self) -> list[str]:
        paginator = self.aws.client("ec2").get_paginator("describe_instances")
        return sorted(
            instance["InstanceId"]
            for page in paginator.paginate(Filters=self._ec2_filters())
            for reservation in page["Reservations"]
            for instance in reservation["Instances"]
            if instance["State"]["Name"] != "terminated"
        )

    def _ec2_tagged(self, operation: str, result_key: str, id_key: str) -> list[str]:
        client = self.aws.client("ec2")
        paginator = client.get_paginator(operation)
        return sorted(
            str(item[id_key])
            for page in paginator.paginate(Filters=self._ec2_filters())
            for item in page.get(result_key, [])
        )

    def _kinesis(self) -> list[str]:
        name = self.bundle.outputs.get("StreamName")
        client = self.aws.client("kinesis")
        names = [name] if name else [
            stream_name
            for page in client.get_paginator("list_streams").paginate()
            for stream_name in page.get("StreamNames", [])
        ]
        result: list[str] = []
        for stream_name in names:
            try:
                tags = tag_map(client.list_tags_for_stream(StreamName=stream_name)["Tags"])
            except client.exceptions.ResourceNotFoundException:
                continue
            if self._owned(tags):
                result.append(stream_name)
        return sorted(result)

    def _dynamodb(self) -> list[str]:
        client = self.aws.client("dynamodb")
        result: list[str] = []
        names = {
            self.bundle.outputs[key]
            for key in ["LeaseTableName", "WorkerMetricsTableName", "CoordinatorStateTableName"]
            if self.bundle.outputs.get(key)
        }
        if not names:
            names = {
                name
                for page in client.get_paginator("list_tables").paginate()
                for name in page.get("TableNames", [])
            }
        for name in sorted(names):
            try:
                table = client.describe_table(TableName=name)["Table"]
            except client.exceptions.ResourceNotFoundException:
                continue
            tags = tag_map(client.list_tags_of_resource(ResourceArn=table["TableArn"])["Tags"])
            if self._owned(tags):
                result.append(name)
        return sorted(result)

    def _ecr(self) -> dict[str, list[str]]:
        name = self.bundle.outputs.get(
            "ConsumerRepositoryName",
            f"loop-ad/perf-phase4-clickhouse/{self.bundle.run_id}",
        )
        client = self.aws.client("ecr")
        try:
            repository = client.describe_repositories(repositoryNames=[name])["repositories"][0]
        except client.exceptions.RepositoryNotFoundException:
            return {"ecrRepositories": [], "ecrImages": []}
        tags = tag_map(client.list_tags_for_resource(resourceArn=repository["repositoryArn"])["tags"])
        if not self._owned(tags):
            return {"ecrRepositories": [], "ecrImages": []}
        images = [
            json.dumps(image_id, sort_keys=True)
            for page in client.get_paginator("list_images").paginate(repositoryName=name)
            for image_id in page.get("imageIds", [])
        ]
        return {"ecrRepositories": [name], "ecrImages": sorted(images)}

    def _s3(self) -> dict[str, list[str]]:
        client = self.aws.client("s3")
        buckets: list[str] = []
        objects: list[str] = []
        names = {
            self.bundle.outputs[key]
            for key in ["FailureBucketName", "ArchiveBucketName"]
            if self.bundle.outputs.get(key)
        }
        if not names:
            names = {item["Name"] for item in client.list_buckets().get("Buckets", [])}
        for name in sorted(names):
            try:
                tags = tag_map(client.get_bucket_tagging(Bucket=name)["TagSet"])
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {"NoSuchBucket", "NoSuchTagSet"}:
                    continue
                raise
            if not self._owned(tags):
                continue
            buckets.append(name)
            objects.extend(
                f"{name}/{item['Key']}"
                for page in client.get_paginator("list_objects_v2").paginate(Bucket=name)
                for item in page.get("Contents", [])
            )
        return {"s3Buckets": sorted(buckets), "s3Objects": sorted(objects)}

    def _logs(self) -> list[str]:
        name = f"/loop-ad/perf-phase4/{self.bundle.run_id}/consumer"
        client = self.aws.client("logs")
        groups = [
            group
            for page in client.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=name)
            for group in page.get("logGroups", [])
            if group["logGroupName"] == name
        ]
        result: list[str] = []
        for group in groups:
            arn = (group.get("logGroupArn") or group.get("arn", "")).removesuffix(":*")
            if arn and self._owned(client.list_tags_for_resource(resourceArn=arn)["tags"]):
                result.append(name)
        return result

    def _secrets(self) -> list[str]:
        arn = self.bundle.outputs.get("ClickHouseSecretArn")
        client = self.aws.client("secretsmanager")
        if arn:
            try:
                secrets = [client.describe_secret(SecretId=arn)]
            except client.exceptions.ResourceNotFoundException:
                secrets = []
        else:
            secrets = [
                secret
                for page in client.get_paginator("list_secrets").paginate(IncludePlannedDeletion=True)
                for secret in page.get("SecretList", [])
            ]
        return sorted(
            str(secret["Name"])
            for secret in secrets
            if self._owned(tag_map(secret.get("Tags", [])))
        )

    def _alarms(self) -> list[str]:
        client = self.aws.client("cloudwatch")
        result: list[str] = []
        for page in client.get_paginator("describe_alarms").paginate(
            AlarmNamePrefix=f"LoopAdPerfPhase4ClickHouseEcsStack",
        ):
            for alarm in [*page.get("MetricAlarms", []), *page.get("CompositeAlarms", [])]:
                tags = tag_map(client.list_tags_for_resource(ResourceARN=alarm["AlarmArn"])["Tags"])
                if self._owned(tags):
                    result.append(alarm["AlarmName"])
        return sorted(result)

    def _iam_roles(self) -> list[str]:
        client = self.aws.client("iam")
        result: list[str] = []
        for role in client.get_paginator("list_roles").paginate().search("Roles[]"):
            tags = tag_map(client.list_role_tags(RoleName=role["RoleName"])["Tags"])
            if self._owned(tags):
                result.append(role["RoleName"])
        return sorted(result)

    def _instance_profiles(self) -> list[str]:
        client = self.aws.client("iam")
        result: list[str] = []
        for profile in client.get_paginator("list_instance_profiles").paginate().search("InstanceProfiles[]"):
            tags = tag_map(client.list_instance_profile_tags(
                InstanceProfileName=profile["InstanceProfileName"],
            )["Tags"])
            if self._owned(tags):
                result.append(profile["InstanceProfileName"])
        return sorted(result)

    def _tag_filters(self) -> list[dict[str, Any]]:
        return [
            {"Key": "RunId", "Values": [self.bundle.run_id]},
            {"Key": "SessionId", "Values": [self.bundle.session_id]},
        ]

    def _ec2_filters(self) -> list[dict[str, Any]]:
        return [
            {"Name": "tag:RunId", "Values": [self.bundle.run_id]},
            {"Name": "tag:SessionId", "Values": [self.bundle.session_id]},
        ]

    def _owned(self, tags: dict[str, str]) -> bool:
        return owned(tags, self.bundle.run_id, self.bundle.session_id)


def tag_map(tags: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(item.get("Key") or item.get("key")): str(item.get("Value") or item.get("value"))
        for item in tags
    }


def owned(tags: dict[str, str], run_id: str, session_id: str) -> bool:
    return tags.get("RunId") == run_id and tags.get("SessionId") == session_id


def stack_owned(
    cloudformation: Any,
    stack: dict[str, Any],
    run_id: str,
    session_id: str,
) -> bool:
    if owned(tag_map(stack.get("Tags", [])), run_id, session_id):
        return True
    template = cloudformation.get_template(StackName=stack["StackName"])["TemplateBody"]
    if isinstance(template, str):
        template = json.loads(template)
    if not isinstance(template, dict):
        return False
    outputs = template.get("Outputs", {})
    if (
        isinstance(outputs, dict)
        and outputs.get("RunId", {}).get("Value") == run_id
        and outputs.get("SessionId", {}).get("Value") == session_id
    ):
        return True
    resources = template.get("Resources", {})
    if not isinstance(resources, dict):
        return False
    for resource in resources.values():
        if not isinstance(resource, dict):
            continue
        properties = resource.get("Properties", {})
        tags = properties.get("Tags", []) if isinstance(properties, dict) else []
        if owned(tag_map(tags if isinstance(tags, list) else []), run_id, session_id):
            return True
    return False


def load_inventory_bundle(run_dir: Path) -> RunBundle:
    run_document = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_id = str(run_document.get("runId", ""))
    session_id = str(run_document.get("sessionId", ""))
    account = str(run_document.get("account") or run_document.get("expectedAccount") or "")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run.json has an invalid Phase 4 ECS runId")
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("run.json has an invalid Phase 4 ECS sessionId")
    if not account.isdigit() or len(account) != 12:
        raise ValueError("run.json has an invalid AWS account")
    outputs: dict[str, str] = {
        "RunId": run_id,
        "SessionId": session_id,
        "ConsumerClusterName": f"loopad-{run_id}-consumer",
        "ConsumerServiceName": f"loopad-{run_id}-consumer",
        "ConsumerAutoScalingGroupName": f"loopad-{run_id}-consumer",
        "ConsumerRepositoryName": f"loop-ad/perf-phase4-clickhouse/{run_id}",
    }
    runtime_outputs = run_dir / "cdk-outputs.json"
    if runtime_outputs.exists():
        document = json.loads(runtime_outputs.read_text(encoding="utf-8"))
        values = document.get(RUNTIME_STACK_NAME)
        if not isinstance(values, dict):
            raise ValueError(f"cdk-outputs.json has no {RUNTIME_STACK_NAME} object")
        observed_run = str(values.get("RunId", ""))
        observed_session = str(values.get("SessionId", ""))
        if observed_run != run_id or observed_session != session_id:
            raise ValueError("cdk outputs do not match run.json ownership")
        outputs.update({str(key): str(value) for key, value in values.items()})
    image_outputs = run_dir / "image-stack-outputs.json"
    if image_outputs.exists():
        document = json.loads(image_outputs.read_text(encoding="utf-8"))
        values = document.get(IMAGE_STACK_NAME)
        if not isinstance(values, dict):
            raise ValueError(f"image-stack-outputs.json has no {IMAGE_STACK_NAME} object")
        outputs.update({str(key): str(value) for key, value in values.items()})
    return RunBundle(run_dir, run_id, session_id, account, outputs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_inventory_bundle(args.run_dir)
    result = Inventory(bundle).collect()
    output = args.output or args.run_dir / "cleanup-inventory-ecs.json"
    write_private(output, result)
    print(json.dumps({"counts": result["counts"], "allZero": result["allZero"]}, indent=2))
    return 0 if result["allZero"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

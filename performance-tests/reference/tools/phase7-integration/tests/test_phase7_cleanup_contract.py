from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

from botocore.exceptions import ClientError


AWS_DIR = Path(__file__).resolve().parents[1] / "aws"
sys.path.insert(0, str(AWS_DIR))

from cleanup import Cleanup, inventory_result, waiter_attempts  # noqa: E402
from common import expected_tags  # noqa: E402


RUN_ID = "run_20260718_010203_phase7_integration"
SESSION_ID = "phase7-integration-20260718T010203Z"


class Paginator:
    def __init__(self, values):
        self.values = values
        self.requests = []

    def paginate(self, **kwargs):
        self.requests.append(kwargs)
        return self.values(kwargs)


class Phase7CleanupContractTest(unittest.TestCase):
    def cleanup_with(self, ecs):
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.client = mock.Mock(return_value=ecs)
        return cleanup

    def owned_tags(self):
        return [
            {"Key": key, "Value": value}
            for key, value in expected_tags(RUN_ID, SESSION_ID).items()
        ]

    def execution_cleanup(self):
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.assert_identity = mock.Mock(return_value={})
        cleanup.stop_archive_tasks = mock.Mock()
        cleanup.empty_bucket = mock.Mock()
        cleanup.empty_repository = mock.Mock()
        cleanup.quiesce_runtime_capacity = mock.Mock()
        cleanup.delete_stack = mock.Mock()
        cleanup.cleanup_terminal_residuals = mock.Mock()
        return cleanup

    def test_normal_runtime_keeps_exact_archive_stop_before_stack_delete(self) -> None:
        cleanup = self.execution_cleanup()
        outputs = {
            "ArchiveClusterName": "owned-cluster",
            "ArchiveTaskDefinitionArn": "owned-task-definition",
        }
        cleanup.stack = mock.Mock(side_effect=[
            {"status": "CREATE_COMPLETE", "outputs": outputs},
            None,
        ])
        ordered = mock.Mock()
        ordered.attach_mock(cleanup.stop_archive_tasks, "stop_archive")
        ordered.attach_mock(cleanup.quiesce_runtime_capacity, "quiesce")
        ordered.attach_mock(cleanup.delete_stack, "delete_stack")
        cleanup.execute()
        cleanup.stop_archive_tasks.assert_called_once_with(outputs, None)
        cleanup.quiesce_runtime_capacity.assert_called_once_with(
            "CREATE_COMPLETE", None
        )
        cleanup.delete_stack.assert_called_once_with(
            "LoopAdPerfPhase7IntegrationStack",
            None,
            already_deleting=False,
        )
        self.assertEqual(
            [
                mock.call.stop_archive(outputs, None),
                mock.call.quiesce("CREATE_COMPLETE", None),
                mock.call.delete_stack(
                    "LoopAdPerfPhase7IntegrationStack",
                    None,
                    already_deleting=False,
                ),
            ],
            ordered.mock_calls,
        )
        cleanup.cleanup_terminal_residuals.assert_called_once_with(None)

    def test_failed_runtime_without_outputs_skips_archive_stop_and_continues_image_cleanup(self) -> None:
        cleanup = self.execution_cleanup()
        image_outputs = {
            "CollectorRepositoryName": f"loop-ad/perf-phase7/{RUN_ID}/collector",
            "ConsumerRepositoryName": f"loop-ad/perf-phase7/{RUN_ID}/consumer",
            "ArchiveRepositoryName": f"loop-ad/perf-phase7/{RUN_ID}/archive",
        }
        cleanup.stack = mock.Mock(side_effect=[
            {"status": "ROLLBACK_COMPLETE", "outputs": {}},
            {"status": "CREATE_COMPLETE", "outputs": image_outputs},
        ])
        cleanup.execute()
        cleanup.stop_archive_tasks.assert_not_called()
        self.assertEqual(
            [
                mock.call(
                    "LoopAdPerfPhase7IntegrationStack",
                    None,
                    already_deleting=False,
                ),
                mock.call(
                    "LoopAdPerfPhase7IntegrationImageStack",
                    None,
                    already_deleting=False,
                ),
            ],
            cleanup.delete_stack.call_args_list,
        )
        self.assertEqual(3, cleanup.empty_repository.call_count)
        cleanup.quiesce_runtime_capacity.assert_called_once_with(
            "ROLLBACK_COMPLETE", None
        )
        cleanup.cleanup_terminal_residuals.assert_called_once_with(None)

    def test_runtime_capacity_cleanup_is_exact_owned_and_fail_closed(self) -> None:
        service_pairs = {
            "clickhouse-service": ("clickhouse-cluster", "tg-clickhouse"),
            "collector-service": ("collector-cluster", "tg-collector"),
            "consumer-service": ("consumer-cluster", None),
            "haproxy-service": ("haproxy-cluster", "tg-haproxy"),
        }
        ecs = mock.Mock()

        def describe_services(**request):
            service_arn = request["services"][0]
            service_name = service_arn.rsplit("/", 1)[-1]
            cluster_name, target_group = service_pairs[service_name]
            self.assertEqual(cluster_name, request["cluster"])
            if "include" not in request:
                return {"services": [], "failures": [{"reason": "MISSING"}]}
            return {"services": [{
                "serviceArn": service_arn,
                "serviceName": service_name,
                "status": "ACTIVE",
                "tags": self.owned_tags(),
                "loadBalancers": (
                    [{"targetGroupArn": target_group}]
                    if target_group else []
                ),
            }], "failures": []}

        ecs.describe_services.side_effect = describe_services

        def delete_service(**request):
            service_arn = request["service"]
            return {"service": {
                "serviceArn": service_arn,
                "status": "DRAINING",
            }}

        ecs.delete_service.side_effect = delete_service
        elbv2 = mock.Mock()
        elbv2.describe_tags.side_effect = lambda **request: {
            "TagDescriptions": [{
                "ResourceArn": request["ResourceArns"][0],
                "Tags": self.owned_tags(),
            }]
        }
        elbv2.modify_target_group_attributes.return_value = {
            "Attributes": [{
                "Key": "deregistration_delay.timeout_seconds",
                "Value": "0",
            }]
        }
        autoscaling = mock.Mock()
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{
                "AutoScalingGroupName": name,
                "Tags": self.owned_tags(),
            } for name in (
                "clickhouse-asg", "collector-asg", "consumer-asg",
                "haproxy-asg", "load-generator-asg",
            )]
        }
        service_arns = [
            "arn:aws:ecs:ap-northeast-2:742711170910:"
            f"service/{cluster}/{service}"
            for service, (cluster, _target_group) in service_pairs.items()
        ]
        target_group_arns = [
            "arn:aws:elasticloadbalancing:ap-northeast-2:742711170910:"
            f"targetgroup/{name}/0123456789abcdef"
            for name in ("tg-clickhouse", "tg-collector", "tg-haproxy")
        ]
        target_group_map = dict(zip(
            ("tg-clickhouse", "tg-collector", "tg-haproxy"),
            target_group_arns,
        ))
        for service_name, (cluster_name, target_group) in list(
            service_pairs.items()
        ):
            if target_group:
                service_pairs[service_name] = (
                    cluster_name,
                    target_group_map[target_group],
                )
        asg_names = [
            "clickhouse-asg", "collector-asg", "consumer-asg",
            "haproxy-asg", "load-generator-asg",
        ]
        summaries = [
            *[
                {"ResourceType": "AWS::ECS::Service", "PhysicalResourceId": arn}
                for arn in service_arns
            ],
            *[
                {
                    "ResourceType": "AWS::ElasticLoadBalancingV2::TargetGroup",
                    "PhysicalResourceId": arn,
                }
                for arn in target_group_arns
            ],
            *[
                {
                    "ResourceType": "AWS::AutoScaling::AutoScalingGroup",
                    "PhysicalResourceId": name,
                }
                for name in asg_names
            ],
        ]
        cloudformation = mock.Mock()
        cloudformation.get_paginator.return_value = Paginator(
            lambda _request: [{"StackResourceSummaries": summaries}]
        )
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(side_effect=lambda service: {
            "cloudformation": cloudformation,
            "ecs": ecs,
            "elbv2": elbv2,
            "autoscaling": autoscaling,
        }[service])

        cleanup.quiesce_runtime_capacity("CREATE_COMPLETE")

        self.assertEqual(4, ecs.delete_service.call_count)
        self.assertTrue(all(
            call.kwargs["force"] is True
            for call in ecs.delete_service.call_args_list
        ))
        self.assertEqual(3, elbv2.modify_target_group_attributes.call_count)
        self.assertEqual(5, autoscaling.update_auto_scaling_group.call_count)
        self.assertTrue(all(
            call.kwargs["MinSize"] == 0
            and call.kwargs["DesiredCapacity"] == 0
            for call in autoscaling.update_auto_scaling_group.call_args_list
        ))

    def test_runtime_capacity_cleanup_accepts_already_missing_resources(self) -> None:
        service_arn = (
            "arn:aws:ecs:ap-northeast-2:742711170910:"
            "service/clickhouse-cluster/clickhouse-service"
        )
        cloudformation = mock.Mock()
        cloudformation.get_paginator.return_value = Paginator(
            lambda _request: [{"StackResourceSummaries": [{
                "ResourceType": "AWS::ECS::Service",
                "PhysicalResourceId": service_arn,
            }, {
                "ResourceType": "AWS::AutoScaling::AutoScalingGroup",
                "PhysicalResourceId": "clickhouse-asg",
            }]}]
        )
        ecs = mock.Mock()
        ecs.describe_services.return_value = {
            "services": [],
            "failures": [{"reason": "MISSING"}],
        }
        autoscaling = mock.Mock()
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": []
        }
        elbv2 = mock.Mock()
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(side_effect=lambda service: {
            "cloudformation": cloudformation,
            "ecs": ecs,
            "elbv2": elbv2,
            "autoscaling": autoscaling,
        }[service])

        cleanup.quiesce_runtime_capacity("ROLLBACK_COMPLETE")

        ecs.delete_service.assert_not_called()
        elbv2.describe_tags.assert_not_called()
        autoscaling.update_auto_scaling_group.assert_not_called()

    def test_healthy_runtime_requires_exact_capacity_inventory(self) -> None:
        cloudformation = mock.Mock()
        cloudformation.get_paginator.return_value = Paginator(
            lambda _request: [{"StackResourceSummaries": [{
                "ResourceType": "AWS::AutoScaling::AutoScalingGroup",
                "PhysicalResourceId": "only-one-asg",
            }]}]
        )
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(return_value=cloudformation)

        with self.assertRaisesRegex(
            RuntimeError, "exact capacity inventory"
        ):
            cleanup.quiesce_runtime_capacity("CREATE_COMPLETE")

    def test_runtime_capacity_validates_all_ownership_before_mutation(self) -> None:
        service_arn = (
            "arn:aws:ecs:ap-northeast-2:742711170910:"
            "service/owned-cluster/owned-service"
        )
        target_group_arn = (
            "arn:aws:elasticloadbalancing:ap-northeast-2:742711170910:"
            "targetgroup/owned/0123456789abcdef"
        )
        summaries = [{
            "ResourceType": "AWS::ECS::Service",
            "PhysicalResourceId": service_arn,
        }, {
            "ResourceType": "AWS::ElasticLoadBalancingV2::TargetGroup",
            "PhysicalResourceId": target_group_arn,
        }, {
            "ResourceType": "AWS::AutoScaling::AutoScalingGroup",
            "PhysicalResourceId": "owned-asg",
        }]

        for bad_kind in ("service", "target-group", "asg"):
            with self.subTest(bad_kind=bad_kind):
                cloudformation = mock.Mock()
                cloudformation.get_paginator.return_value = Paginator(
                    lambda _request: [{"StackResourceSummaries": summaries}]
                )
                wrong_tags = [
                    {
                        "Key": item["Key"],
                        "Value": (
                            "wrong-run"
                            if item["Key"] == "RunId"
                            else item["Value"]
                        ),
                    }
                    for item in self.owned_tags()
                ]
                ecs = mock.Mock()
                ecs.describe_services.return_value = {
                    "services": [{
                        "serviceArn": service_arn,
                        "serviceName": "owned-service",
                        "status": "ACTIVE",
                        "tags": (
                            wrong_tags
                            if bad_kind == "service"
                            else self.owned_tags()
                        ),
                        "loadBalancers": [{
                            "targetGroupArn": target_group_arn,
                        }],
                    }],
                    "failures": [],
                }
                elbv2 = mock.Mock()
                elbv2.describe_tags.return_value = {
                    "TagDescriptions": [{
                        "ResourceArn": target_group_arn,
                        "Tags": (
                            wrong_tags
                            if bad_kind == "target-group"
                            else self.owned_tags()
                        ),
                    }]
                }
                autoscaling = mock.Mock()
                autoscaling.describe_auto_scaling_groups.return_value = {
                    "AutoScalingGroups": [{
                        "AutoScalingGroupName": "owned-asg",
                        "Tags": (
                            wrong_tags
                            if bad_kind == "asg"
                            else self.owned_tags()
                        ),
                    }]
                }
                cleanup = Cleanup.__new__(Cleanup)
                cleanup.run_id = RUN_ID
                cleanup.session_id = SESSION_ID
                cleanup.deadline_breached = False
                cleanup.client = mock.Mock(side_effect=lambda service: {
                    "cloudformation": cloudformation,
                    "ecs": ecs,
                    "elbv2": elbv2,
                    "autoscaling": autoscaling,
                }[service])

                with self.assertRaisesRegex(RuntimeError, "refusing non-owned"):
                    cleanup.quiesce_runtime_capacity("ROLLBACK_COMPLETE")

                ecs.delete_service.assert_not_called()
                elbv2.modify_target_group_attributes.assert_not_called()
                autoscaling.update_auto_scaling_group.assert_not_called()

    def test_runtime_capacity_rerun_does_not_redelete_draining_service(self) -> None:
        draining_arn = (
            "arn:aws:ecs:ap-northeast-2:742711170910:"
            "service/owned-cluster/draining-service"
        )
        inactive_arn = (
            "arn:aws:ecs:ap-northeast-2:742711170910:"
            "service/owned-cluster/inactive-service"
        )
        cloudformation = mock.Mock()
        cloudformation.get_paginator.return_value = Paginator(
            lambda _request: [{"StackResourceSummaries": [
                {
                    "ResourceType": "AWS::ECS::Service",
                    "PhysicalResourceId": draining_arn,
                },
                {
                    "ResourceType": "AWS::ECS::Service",
                    "PhysicalResourceId": inactive_arn,
                },
            ]}]
        )
        ecs = mock.Mock()

        def describe_services(**request):
            arn = request["services"][0]
            if "include" not in request:
                return {"services": [], "failures": [{"reason": "MISSING"}]}
            status = "DRAINING" if arn == draining_arn else "INACTIVE"
            return {
                "services": [{
                    "serviceArn": arn,
                    "serviceName": arn.rsplit("/", 1)[-1],
                    "status": status,
                    "tags": self.owned_tags(),
                    "loadBalancers": [],
                }],
                "failures": [],
            }

        ecs.describe_services.side_effect = describe_services
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(side_effect=lambda service: {
            "cloudformation": cloudformation,
            "ecs": ecs,
            "elbv2": mock.Mock(),
            "autoscaling": mock.Mock(),
        }[service])

        cleanup.quiesce_runtime_capacity("ROLLBACK_COMPLETE")

        ecs.delete_service.assert_not_called()

    def test_runtime_capacity_accepts_only_confirmed_mutation_races(self) -> None:
        service_arn = (
            "arn:aws:ecs:ap-northeast-2:742711170910:"
            "service/owned-cluster/owned-service"
        )
        target_group_arn = (
            "arn:aws:elasticloadbalancing:ap-northeast-2:742711170910:"
            "targetgroup/owned/0123456789abcdef"
        )
        cloudformation = mock.Mock()
        cloudformation.get_paginator.return_value = Paginator(
            lambda _request: [{"StackResourceSummaries": [{
                "ResourceType": "AWS::ECS::Service",
                "PhysicalResourceId": service_arn,
            }, {
                "ResourceType": "AWS::ElasticLoadBalancingV2::TargetGroup",
                "PhysicalResourceId": target_group_arn,
            }, {
                "ResourceType": "AWS::AutoScaling::AutoScalingGroup",
                "PhysicalResourceId": "owned-asg",
            }]}]
        )
        ecs = mock.Mock()
        ecs.describe_services.side_effect = [{
            "services": [{
                "serviceArn": service_arn,
                "serviceName": "owned-service",
                "status": "ACTIVE",
                "tags": self.owned_tags(),
                "loadBalancers": [{"targetGroupArn": target_group_arn}],
            }],
            "failures": [],
        }, {
            "services": [],
            "failures": [{"reason": "MISSING"}],
        }]
        ecs.delete_service.side_effect = ClientError(
            {"Error": {
                "Code": "ServiceNotFoundException",
                "Message": "already gone",
            }},
            "DeleteService",
        )
        elbv2 = mock.Mock()
        elbv2.describe_tags.return_value = {"TagDescriptions": [{
            "ResourceArn": target_group_arn,
            "Tags": self.owned_tags(),
        }]}
        elbv2.modify_target_group_attributes.side_effect = ClientError(
            {"Error": {
                "Code": "TargetGroupNotFound",
                "Message": "already gone",
            }},
            "ModifyTargetGroupAttributes",
        )
        elbv2.describe_target_groups.side_effect = ClientError(
            {"Error": {
                "Code": "TargetGroupNotFound",
                "Message": "already gone",
            }},
            "DescribeTargetGroups",
        )
        autoscaling = mock.Mock()
        autoscaling.describe_auto_scaling_groups.side_effect = [{
            "AutoScalingGroups": [{
                "AutoScalingGroupName": "owned-asg",
                "Tags": self.owned_tags(),
            }]
        }, {"AutoScalingGroups": []}]
        autoscaling.update_auto_scaling_group.side_effect = ClientError(
            {"Error": {
                "Code": "ValidationError",
                "Message": "already gone",
            }},
            "UpdateAutoScalingGroup",
        )
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(side_effect=lambda service: {
            "cloudformation": cloudformation,
            "ecs": ecs,
            "elbv2": elbv2,
            "autoscaling": autoscaling,
        }[service])

        cleanup.quiesce_runtime_capacity("DELETE_IN_PROGRESS")

        ecs.delete_service.assert_called_once()
        elbv2.describe_target_groups.assert_called_once_with(
            TargetGroupArns=[target_group_arn]
        )
        self.assertEqual(2, autoscaling.describe_auto_scaling_groups.call_count)

    def test_terminal_residual_cleanup_requires_exact_terminal_state(self) -> None:
        task_arn = (
            "arn:aws:ecs:ap-northeast-2:742711170910:task-definition/owned:1"
        )
        cluster_arn = (
            "arn:aws:ecs:ap-northeast-2:742711170910:cluster/owned"
        )
        nat_arn = (
            "arn:aws:ec2:ap-northeast-2:742711170910:natgateway/nat-owned"
        )
        endpoint_arn = (
            "arn:aws:ec2:ap-northeast-2:742711170910:"
            "vpc-endpoint/vpce-deleted"
        )
        mappings = [
            {"ResourceARN": arn, "Tags": self.owned_tags()}
            for arn in (task_arn, cluster_arn, nat_arn, endpoint_arn)
        ]
        tagging = mock.Mock()
        tagging.get_paginator.return_value = Paginator(
            lambda _request: [{"ResourceTagMappingList": mappings}]
        )
        tagging.untag_resources.return_value = {"FailedResourcesMap": {}}
        ecs = mock.Mock()
        ecs.describe_task_definition.return_value = {
            "taskDefinition": {
                "taskDefinitionArn": task_arn,
                "status": "INACTIVE",
            },
            "tags": self.owned_tags(),
        }
        ecs.delete_task_definitions.return_value = {
            "taskDefinitions": [{"taskDefinitionArn": task_arn}],
            "failures": [],
        }
        ecs.describe_clusters.return_value = {
            "clusters": [{
                "clusterArn": cluster_arn,
                "status": "INACTIVE",
                "runningTasksCount": 0,
                "pendingTasksCount": 0,
                "activeServicesCount": 0,
                "tags": self.owned_tags(),
            }]
        }
        ecs.create_cluster.return_value = {
            "cluster": {
                "clusterArn": cluster_arn,
                "status": "ACTIVE",
                "runningTasksCount": 0,
                "pendingTasksCount": 0,
                "activeServicesCount": 0,
                "registeredContainerInstancesCount": 0,
            }
        }
        ecs.list_tags_for_resource.side_effect = [
            {"tags": self.owned_tags()},
            {"tags": []},
        ]
        ecs.delete_cluster.return_value = {
            "cluster": {
                "clusterArn": cluster_arn,
                "status": "INACTIVE",
                "runningTasksCount": 0,
                "pendingTasksCount": 0,
                "activeServicesCount": 0,
                "registeredContainerInstancesCount": 0,
            }
        }
        ec2 = mock.Mock()
        ec2.describe_nat_gateways.return_value = {
            "NatGateways": [{
                "NatGatewayId": "nat-owned",
                "State": "deleted",
                "Tags": self.owned_tags(),
            }]
        }
        ec2.describe_vpc_endpoints.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidVpcEndpointId.NotFound",
                    "Message": "not found",
                }
            },
            "DescribeVpcEndpoints",
        )
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(
            side_effect=lambda service: {
                "resourcegroupstaggingapi": tagging,
                "ecs": ecs,
                "ec2": ec2,
            }[service]
        )

        cleanup.cleanup_terminal_residuals()

        ecs.delete_task_definitions.assert_called_once_with(
            taskDefinitions=[task_arn]
        )
        tagging.untag_resources.assert_called_once_with(
            ResourceARNList=sorted(
                [task_arn, nat_arn, endpoint_arn]
            ),
            TagKeys=list(expected_tags(RUN_ID, SESSION_ID)),
        )
        ecs.create_cluster.assert_called_once()
        ecs.untag_resource.assert_called_once_with(
            resourceArn=cluster_arn,
            tagKeys=list(expected_tags(RUN_ID, SESSION_ID)),
        )
        ecs.delete_cluster.assert_called_once_with(cluster=cluster_arn)

    def test_terminal_residual_cleanup_accepts_task_deletion_in_progress(self) -> None:
        task_arn = (
            "arn:aws:ecs:ap-northeast-2:742711170910:task-definition/owned:1"
        )
        tagging = mock.Mock()
        tagging.get_paginator.return_value = Paginator(
            lambda _request: [{"ResourceTagMappingList": [{
                "ResourceARN": task_arn,
                "Tags": self.owned_tags(),
            }]}]
        )
        tagging.untag_resources.return_value = {"FailedResourcesMap": {}}
        ecs = mock.Mock()
        ecs.describe_task_definition.return_value = {
            "taskDefinition": {
                "taskDefinitionArn": task_arn,
                "status": "DELETE_IN_PROGRESS",
            },
            "tags": self.owned_tags(),
        }
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(
            side_effect=lambda service: {
                "resourcegroupstaggingapi": tagging,
                "ecs": ecs,
                "ec2": mock.Mock(),
            }[service]
        )

        cleanup.cleanup_terminal_residuals()

        ecs.delete_task_definitions.assert_not_called()
        tagging.untag_resources.assert_called_once()

    def test_terminal_residual_cleanup_defers_stopped_task_tag_tombstone(self) -> None:
        task_arn = (
            "arn:aws:ecs:ap-northeast-2:742711170910:task/owned-cluster/task-id"
        )
        tagging = mock.Mock()
        tagging.get_paginator.return_value = Paginator(
            lambda _request: [{"ResourceTagMappingList": [{
                "ResourceARN": task_arn,
                "Tags": self.owned_tags(),
            }]}]
        )
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(
            side_effect=lambda service: {
                "resourcegroupstaggingapi": tagging,
                "ecs": mock.Mock(),
                "ec2": mock.Mock(),
            }[service]
        )

        cleanup.cleanup_terminal_residuals()

        tagging.untag_resources.assert_not_called()

    def test_terminal_residual_cleanup_accepts_terminated_instance_and_deleted_volume(self) -> None:
        instance_arn = (
            "arn:aws:ec2:ap-northeast-2:742711170910:instance/i-owned"
        )
        volume_arn = (
            "arn:aws:ec2:ap-northeast-2:742711170910:volume/vol-owned"
        )
        tagging = mock.Mock()
        tagging.get_paginator.return_value = Paginator(
            lambda _request: [{"ResourceTagMappingList": [
                {"ResourceARN": instance_arn, "Tags": self.owned_tags()},
                {"ResourceARN": volume_arn, "Tags": self.owned_tags()},
            ]}]
        )
        tagging.untag_resources.return_value = {"FailedResourcesMap": {}}
        ec2 = mock.Mock()
        ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{
                "InstanceId": "i-owned",
                "State": {"Name": "terminated"},
                "Tags": self.owned_tags(),
            }]}]
        }
        ec2.describe_volumes.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidVolume.NotFound",
                    "Message": "not found",
                }
            },
            "DescribeVolumes",
        )
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(
            side_effect=lambda service: {
                "resourcegroupstaggingapi": tagging,
                "ecs": mock.Mock(),
                "ec2": ec2,
            }[service]
        )

        cleanup.cleanup_terminal_residuals()

        tagging.untag_resources.assert_called_once_with(
            ResourceARNList=sorted([instance_arn, volume_arn]),
            TagKeys=list(expected_tags(RUN_ID, SESSION_ID)),
        )

    def test_terminal_residual_cleanup_accepts_missing_nat_gateway(self) -> None:
        nat_arn = (
            "arn:aws:ec2:ap-northeast-2:742711170910:natgateway/nat-owned"
        )
        tagging = mock.Mock()
        tagging.get_paginator.return_value = Paginator(
            lambda _request: [{"ResourceTagMappingList": [{
                "ResourceARN": nat_arn,
                "Tags": self.owned_tags(),
            }]}]
        )
        tagging.untag_resources.return_value = {"FailedResourcesMap": {}}
        ec2 = mock.Mock()
        ec2.describe_nat_gateways.side_effect = ClientError(
            {
                "Error": {
                    "Code": "NatGatewayNotFound",
                    "Message": "not found",
                }
            },
            "DescribeNatGateways",
        )
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(
            side_effect=lambda service: {
                "resourcegroupstaggingapi": tagging,
                "ecs": mock.Mock(),
                "ec2": ec2,
            }[service]
        )

        cleanup.cleanup_terminal_residuals()

        tagging.untag_resources.assert_called_once_with(
            ResourceARNList=[nat_arn],
            TagKeys=list(expected_tags(RUN_ID, SESSION_ID)),
        )

    def test_terminal_residual_cleanup_accepts_deleted_subnet(self) -> None:
        subnet_arn = (
            "arn:aws:ec2:ap-northeast-2:742711170910:subnet/subnet-owned"
        )
        tagging = mock.Mock()
        tagging.get_paginator.return_value = Paginator(
            lambda _request: [{"ResourceTagMappingList": [{
                "ResourceARN": subnet_arn,
                "Tags": self.owned_tags(),
            }]}]
        )
        tagging.untag_resources.return_value = {"FailedResourcesMap": {}}
        ec2 = mock.Mock()
        ec2.describe_subnets.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidSubnetID.NotFound",
                    "Message": "not found",
                }
            },
            "DescribeSubnets",
        )
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(
            side_effect=lambda service: {
                "resourcegroupstaggingapi": tagging,
                "ecs": mock.Mock(),
                "ec2": ec2,
            }[service]
        )

        cleanup.cleanup_terminal_residuals()

        tagging.untag_resources.assert_called_once_with(
            ResourceARNList=[subnet_arn],
            TagKeys=list(expected_tags(RUN_ID, SESSION_ID)),
        )

    def test_terminal_residual_cleanup_refuses_existing_subnet(self) -> None:
        subnet_arn = (
            "arn:aws:ec2:ap-northeast-2:742711170910:subnet/subnet-owned"
        )
        tagging = mock.Mock()
        tagging.get_paginator.return_value = Paginator(
            lambda _request: [{"ResourceTagMappingList": [{
                "ResourceARN": subnet_arn,
                "Tags": self.owned_tags(),
            }]}]
        )
        ec2 = mock.Mock()
        ec2.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-owned", "Tags": self.owned_tags()}]
        }
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(
            side_effect=lambda service: {
                "resourcegroupstaggingapi": tagging,
                "ecs": mock.Mock(),
                "ec2": ec2,
            }[service]
        )

        with self.assertRaisesRegex(RuntimeError, "subnet that still exists"):
            cleanup.cleanup_terminal_residuals()
        tagging.untag_resources.assert_not_called()

    def test_terminal_residual_cleanup_reactivates_only_missing_owned_services(self) -> None:
        cluster_name = "owned-cluster"
        service_name = "owned-service"
        service_arn = (
            "arn:aws:ecs:ap-northeast-2:742711170910:service/"
            f"{cluster_name}/{service_name}"
        )
        tagging = mock.Mock()
        tagging.get_paginator.return_value = Paginator(
            lambda _request: [{"ResourceTagMappingList": [{
                "ResourceARN": service_arn,
                "Tags": self.owned_tags(),
            }]}]
        )
        ecs = mock.Mock()
        ecs.describe_services.return_value = {
            "services": [],
            "failures": [{"arn": service_arn, "reason": "MISSING"}],
        }
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(
            side_effect=lambda service: {
                "resourcegroupstaggingapi": tagging,
                "ecs": ecs,
                "ec2": mock.Mock(),
            }[service]
        )
        cleanup.reactivate_missing_ecs_services = mock.Mock()

        cleanup.cleanup_terminal_residuals()

        cleanup.reactivate_missing_ecs_services.assert_called_once_with(
            [(service_arn, cluster_name, service_name)],
            None,
        )
        tagging.untag_resources.assert_not_called()

    def test_terminal_residual_cleanup_refuses_active_cluster(self) -> None:
        cluster_arn = (
            "arn:aws:ecs:ap-northeast-2:742711170910:cluster/owned"
        )
        tagging = mock.Mock()
        tagging.get_paginator.return_value = Paginator(
            lambda _request: [{"ResourceTagMappingList": [{
                "ResourceARN": cluster_arn,
                "Tags": self.owned_tags(),
            }]}]
        )
        ecs = mock.Mock()
        ecs.describe_clusters.return_value = {
            "clusters": [{
                "clusterArn": cluster_arn,
                "status": "ACTIVE",
                "runningTasksCount": 0,
                "pendingTasksCount": 0,
                "activeServicesCount": 0,
                "tags": self.owned_tags(),
            }]
        }
        cleanup = Cleanup.__new__(Cleanup)
        cleanup.run_id = RUN_ID
        cleanup.session_id = SESSION_ID
        cleanup.deadline_breached = False
        cleanup.client = mock.Mock(
            side_effect=lambda service: {
                "resourcegroupstaggingapi": tagging,
                "ecs": ecs,
                "ec2": mock.Mock(),
            }[service]
        )

        with self.assertRaisesRegex(RuntimeError, "non-terminal"):
            cleanup.cleanup_terminal_residuals()
        tagging.untag_resources.assert_not_called()

    def test_healthy_runtime_without_archive_outputs_fails_closed(self) -> None:
        cleanup = self.execution_cleanup()
        cleanup.stack = mock.Mock(return_value={
            "status": "CREATE_COMPLETE",
            "outputs": {},
        })
        with self.assertRaisesRegex(RuntimeError, "healthy runtime stack"):
            cleanup.execute()
        cleanup.delete_stack.assert_not_called()

    def test_cleanup_stops_only_tasks_from_the_exact_archive_definition_and_waits(self) -> None:
        task_arn = "arn:aws:ecs:ap-northeast-2:742711170910:task/archive/one"
        ecs = mock.Mock()
        paginator = Paginator(
            lambda request: [{"taskArns": [task_arn] if request["desiredStatus"] == "RUNNING" else []}]
        )
        ecs.get_paginator.return_value = paginator
        ecs.describe_tasks.side_effect = [
            {"tasks": [{
                "taskArn": task_arn,
                "taskDefinitionArn": "task-definition",
                "lastStatus": "RUNNING",
                "tags": self.owned_tags(),
            }]},
            {"tasks": [{
                "taskArn": task_arn,
                "taskDefinitionArn": "task-definition",
                "lastStatus": "STOPPED",
                "tags": self.owned_tags(),
            }]},
        ]
        ecs.describe_task_definition.return_value = {
            "taskDefinition": {"taskDefinitionArn": "task-definition", "family": "archive-family"}
        }
        waiter = mock.Mock()
        ecs.get_waiter.return_value = waiter
        ecs.list_tags_for_resource.return_value = {"tags": []}
        cleanup = self.cleanup_with(ecs)
        cleanup.stop_archive_tasks({
            "ArchiveClusterName": "owned-cluster",
            "ArchiveTaskDefinitionArn": "task-definition",
        })
        ecs.stop_task.assert_called_once_with(
            cluster="owned-cluster",
            task=task_arn,
            reason=f"Phase 7 exact-owned cleanup for {RUN_ID}",
        )
        waiter.wait.assert_called_once()
        ecs.untag_resource.assert_called_once()
        self.assertEqual(
            {"Project", "ManagedBy", "Phase", "ResourceScope", "RunId", "SessionId"},
            set(ecs.untag_resource.call_args.kwargs["tagKeys"]),
        )
        self.assertEqual(
            ["archive-family", "archive-family", "archive-family"],
            [request["family"] for request in paginator.requests],
        )

    def test_cleanup_tolerates_aws_rejecting_untag_for_a_stopped_archive_task(self) -> None:
        task_arn = "arn:aws:ecs:ap-northeast-2:742711170910:task/archive/stopped"
        ecs = mock.Mock()
        ecs.get_paginator.return_value = Paginator(
            lambda request: [{
                "taskArns": [task_arn] if request["desiredStatus"] == "STOPPED" else []
            }]
        )
        task = {
            "taskArn": task_arn,
            "taskDefinitionArn": "task-definition",
            "lastStatus": "STOPPED",
            "tags": self.owned_tags(),
        }
        ecs.describe_tasks.side_effect = [{"tasks": [task]}, {"tasks": [task]}]
        ecs.describe_task_definition.return_value = {
            "taskDefinition": {
                "taskDefinitionArn": "task-definition",
                "family": "archive-family",
            }
        }
        ecs.untag_resource.side_effect = ClientError({
            "Error": {
                "Code": "InvalidParameterException",
                "Message": "The specified task is stopped. Specify a running task and try again.",
            }
        }, "UntagResource")
        cleanup = self.cleanup_with(ecs)
        cleanup.stop_archive_tasks({
            "ArchiveClusterName": "owned-cluster",
            "ArchiveTaskDefinitionArn": "task-definition",
        })
        ecs.stop_task.assert_not_called()
        ecs.get_waiter.assert_not_called()
        ecs.untag_resource.assert_called_once_with(
            resourceArn=task_arn,
            tagKeys=list(expected_tags(RUN_ID, SESSION_ID)),
        )
        ecs.list_tags_for_resource.assert_not_called()

    def test_cleanup_refuses_a_task_from_another_definition(self) -> None:
        ecs = mock.Mock()
        ecs.get_paginator.return_value = Paginator(
            lambda request: [{"taskArns": ["other"] if request["desiredStatus"] == "RUNNING" else []}]
        )
        ecs.describe_tasks.return_value = {
            "tasks": [{
                "taskArn": "other",
                "taskDefinitionArn": "not-owned",
                "lastStatus": "RUNNING",
                "tags": self.owned_tags(),
            }]
        }
        ecs.describe_task_definition.return_value = {
            "taskDefinition": {"taskDefinitionArn": "task-definition", "family": "archive-family"}
        }
        with self.assertRaisesRegex(RuntimeError, "outside the exact archive ownership"):
            self.cleanup_with(ecs).stop_archive_tasks({
                "ArchiveClusterName": "owned-cluster",
                "ArchiveTaskDefinitionArn": "task-definition",
            })
        ecs.stop_task.assert_not_called()

    def test_each_new_service_class_residual_blocks_inventory_zero(self) -> None:
        classes = (
            "ecsTasks", "ecsContainerInstances", "ecsTaskDefinitions",
            "ecsCapacityProviders", "targetGroups", "listeners",
            "elasticIpAllocations", "ecrImages",
        )
        for resource_class in classes:
            with self.subTest(resource_class=resource_class):
                result = inventory_result(
                    {}, RUN_ID, SESSION_ID, {resource_class: ["owned-residual"]}, []
                )
                self.assertFalse(result["allZero"])

    def test_tagging_api_residual_blocks_inventory_zero(self) -> None:
        result = inventory_result(
            {}, RUN_ID, SESSION_ID, {"cloudFormationStacks": []}, ["owned-arn"]
        )
        self.assertTrue(result["serviceInventoryZero"])
        self.assertFalse(result["taggingApiResidualsZero"])
        self.assertFalse(result["allZero"])

    def test_kinesis_inventory_scans_every_stream_and_selects_by_exact_tags(self) -> None:
        kinesis = mock.Mock()
        kinesis.get_paginator.return_value = Paginator(
            lambda _request: [{
                "StreamNames": ["shared-stream", "arbitrary-owned-stream"]
            }]
        )

        def stream_tags(**request):
            if request["StreamName"] == "arbitrary-owned-stream":
                return {"Tags": self.owned_tags(), "HasMoreTags": False}
            return {
                "Tags": [{"Key": "RunId", "Value": "another-run"}],
                "HasMoreTags": False,
            }

        kinesis.list_tags_for_stream.side_effect = stream_tags
        cleanup = self.cleanup_with(kinesis)
        self.assertEqual(["arbitrary-owned-stream"], cleanup._kinesis_streams())
        self.assertEqual(
            {"shared-stream", "arbitrary-owned-stream"},
            {
                call.kwargs["StreamName"]
                for call in kinesis.list_tags_for_stream.call_args_list
            },
        )

    def test_kinesis_inventory_paginates_stream_tags(self) -> None:
        kinesis = mock.Mock()
        kinesis.get_paginator.return_value = Paginator(
            lambda _request: [{"StreamNames": ["owned-with-many-tags"]}]
        )
        tags = self.owned_tags()
        kinesis.list_tags_for_stream.side_effect = [
            {"Tags": tags[:3], "HasMoreTags": True},
            {"Tags": tags[3:], "HasMoreTags": False},
        ]
        cleanup = self.cleanup_with(kinesis)
        self.assertEqual(["owned-with-many-tags"], cleanup._kinesis_streams())
        self.assertEqual(
            tags[2]["Key"],
            kinesis.list_tags_for_stream.call_args_list[1].kwargs[
                "ExclusiveStartTagKey"
            ],
        )

    def test_s3_emptying_revalidates_exact_name_and_tags_before_deletion(self) -> None:
        s3 = mock.Mock()
        s3.get_bucket_tagging.return_value = {"TagSet": self.owned_tags()}
        s3.list_object_versions.return_value = {
            "Versions": [{"Key": "evidence", "VersionId": "v1"}],
            "IsTruncated": False,
        }
        # The next listing confirms the deletion completed.
        s3.list_object_versions.side_effect = [
            s3.list_object_versions.return_value,
            {"IsTruncated": False},
        ]
        cleanup = self.cleanup_with(s3)
        cleanup.empty_bucket("exact-bucket", expected_name="exact-bucket")
        s3.get_bucket_tagging.assert_called_once_with(Bucket="exact-bucket")
        s3.delete_objects.assert_called_once()

    def test_s3_emptying_refuses_name_or_tag_mismatch(self) -> None:
        s3 = mock.Mock()
        cleanup = self.cleanup_with(s3)
        with self.assertRaisesRegex(RuntimeError, "outside the exact stack output"):
            cleanup.empty_bucket("other", expected_name="expected")
        s3.get_bucket_tagging.assert_not_called()

        s3.get_bucket_tagging.return_value = {
            "TagSet": [{"Key": "RunId", "Value": "another-run"}]
        }
        with self.assertRaisesRegex(RuntimeError, "non-owned S3"):
            cleanup.empty_bucket("expected", expected_name="expected")
        s3.list_object_versions.assert_not_called()

    def test_ecr_emptying_revalidates_exact_name_and_tags_before_deletion(self) -> None:
        repository = f"loop-ad/perf-phase7/{RUN_ID}/collector"
        repository_arn = (
            "arn:aws:ecr:ap-northeast-2:742711170910:repository/" + repository
        )
        ecr = mock.Mock()
        ecr.describe_repositories.return_value = {
            "repositories": [{
                "repositoryName": repository,
                "repositoryArn": repository_arn,
            }]
        }
        ecr.list_tags_for_resource.return_value = {"tags": self.owned_tags()}
        ecr.get_paginator.return_value = Paginator(lambda _request: [{
            "imageDetails": [{"imageDigest": "sha256:abc"}]
        }])
        cleanup = self.cleanup_with(ecr)
        cleanup.empty_repository(repository, expected_name=repository)
        ecr.describe_repositories.assert_called_once_with(
            repositoryNames=[repository]
        )
        ecr.list_tags_for_resource.assert_called_once_with(
            resourceArn=repository_arn
        )
        ecr.batch_delete_image.assert_called_once()

    def test_ecr_emptying_refuses_wrong_exact_role_name(self) -> None:
        ecr = mock.Mock()
        cleanup = self.cleanup_with(ecr)
        expected = f"loop-ad/perf-phase7/{RUN_ID}/collector"
        with self.assertRaisesRegex(RuntimeError, "outside the exact role name"):
            cleanup.empty_repository("shared/repository", expected_name=expected)
        ecr.describe_repositories.assert_not_called()

    def test_ecr_emptying_refuses_tag_mismatch_before_listing_images(self) -> None:
        repository = f"loop-ad/perf-phase7/{RUN_ID}/collector"
        ecr = mock.Mock()
        ecr.describe_repositories.return_value = {
            "repositories": [{
                "repositoryName": repository,
                "repositoryArn": "repository-arn",
            }]
        }
        ecr.list_tags_for_resource.return_value = {
            "tags": [{"Key": "RunId", "Value": "another-run"}]
        }
        cleanup = self.cleanup_with(ecr)
        with self.assertRaisesRegex(RuntimeError, "non-owned ECR"):
            cleanup.empty_repository(repository, expected_name=repository)
        ecr.get_paginator.assert_not_called()

    def test_expired_cleanup_deadline_does_not_abandon_owned_recovery(self) -> None:
        s3 = mock.Mock()
        s3.get_bucket_tagging.return_value = {"TagSet": self.owned_tags()}
        s3.list_object_versions.return_value = {"IsTruncated": False}
        cleanup = self.cleanup_with(s3)
        cleanup.empty_bucket(
            "exact-bucket", deadline=0.0, expected_name="exact-bucket"
        )
        self.assertTrue(cleanup.deadline_breached)
        self.assertEqual(80, waiter_attempts(0.0, delay=15, maximum=80))


if __name__ == "__main__":
    unittest.main()

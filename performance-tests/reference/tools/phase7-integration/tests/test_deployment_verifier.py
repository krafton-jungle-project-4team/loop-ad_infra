from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile
import unittest


AWS_DIR = Path(__file__).resolve().parents[1] / "aws"
sys.path.insert(0, str(AWS_DIR))

from runtime_stages import (  # noqa: E402
    Bundle,
    EXPECTED_RUNTIME_OUTPUT_KEYS,
    deployment_contract,
    exact_group_edge,
    expected_protocol_load_balancer_name,
    expected_ecs_task_definitions,
    load_bundle,
    validate_task_definition,
    verify_cloudformation_outputs,
    verify_deployment,
    verify_protocol_path,
)
from validate_template import validate_template  # noqa: E402


RUN_ID = "run_20260718_200000_phase7_integration"
SESSION_ID = "phase7-integration-20260718T200000Z"
TAGS = [
    {"key": "Project", "value": "loop-ad"},
    {"key": "Phase", "value": "7"},
    {"key": "RunId", "value": RUN_ID},
    {"key": "SessionId", "value": SESSION_ID},
    {"key": "ResourceScope", "value": "run"},
    {"key": "ManagedBy", "value": "codex"},
]


def runtime_outputs() -> dict[str, str]:
    outputs = {key: f"exact-{key}" for key in EXPECTED_RUNTIME_OUTPUT_KEYS}
    outputs.update({
        "ProtocolEndpoint": "https://events.example.test",
        "ProtocolConnectDnsName": (
            "perf-p1-conn-proxy-60718200000-0123456789abcdef."
            "elb.ap-northeast-2.amazonaws.com"
        ),
        "StreamName": f"{RUN_ID}-stream",
        "ClickHouseEndpoint": "http://internal-clickhouse.example.test:8123",
        "ArchiveBucketName": f"{RUN_ID}-archive",
        "FailureBucketName": f"{RUN_ID}-failure",
        "LeaseTableName": f"{RUN_ID}-leases",
        "WorkerMetricsTableName": f"{RUN_ID}-worker-metrics",
        "CoordinatorStateTableName": f"{RUN_ID}-coordinator-state",
        "ClickHouseSecretArn": (
            "arn:aws:secretsmanager:ap-northeast-2:742711170910:secret:phase7-test"
        ),
    })
    return outputs


def runtime_images() -> dict[str, dict[str, str]]:
    return {
        role: {
            "digest": f"sha256:{digit * 64}",
            "exactImage": f"example.test/{role}@sha256:{digit * 64}",
        }
        for role, digit in (("collector", "1"), ("consumer", "2"), ("archive", "3"))
    }


class FakeIam:
    def list_role_tags(self, RoleName: str) -> dict[str, object]:
        return {"Tags": [{"Key": item["key"], "Value": item["value"]} for item in TAGS]}


class FakeAws:
    def __init__(self, run_dir: Path) -> None:
        self.bundle = Bundle(run_dir, RUN_ID, SESSION_ID, {})

    def client(self, service: str) -> FakeIam:
        if service != "iam":
            raise AssertionError(service)
        return FakeIam()


class FakeCloudFormation:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs

    def describe_stacks(self, StackName: str) -> dict[str, object]:
        self.stack_name = StackName
        return {"Stacks": [{
            "StackStatus": "CREATE_COMPLETE",
            "Tags": [
                {"Key": "RunId", "Value": RUN_ID},
                {"Key": "SessionId", "Value": SESSION_ID},
                {"Key": "ResourceScope", "Value": "run"},
            ],
            "Outputs": [
                {"OutputKey": key, "OutputValue": value}
                for key, value in self.outputs.items()
            ],
        }]}


class FakeDeploymentAws:
    def __init__(self, run_dir: Path, stack_outputs: dict[str, str]) -> None:
        self.bundle = Bundle(run_dir, RUN_ID, SESSION_ID, runtime_outputs())
        self.cloudformation = FakeCloudFormation(stack_outputs)

    def assert_identity(self) -> dict[str, str]:
        return {"account": "742711170910", "arn": "arn:aws:iam::742711170910:root"}

    def client(self, service: str) -> FakeCloudFormation:
        if service != "cloudformation":
            raise AssertionError(f"verification advanced past stack outputs to {service}")
        return self.cloudformation

    def wait_service(self, role: str) -> dict[str, object]:
        raise AssertionError(f"verification advanced past stack outputs to {role}")


class FakeLoadBalancerPaginator:
    def __init__(self, load_balancers: list[dict[str, object]]) -> None:
        self.load_balancers = load_balancers

    def paginate(self) -> list[dict[str, object]]:
        return [{"LoadBalancers": self.load_balancers}]


class FakeProtocolElbv2:
    def __init__(
        self,
        load_balancers: list[dict[str, object]],
        tags: list[dict[str, str]] = TAGS,
    ) -> None:
        self.load_balancers = load_balancers
        self.tags = tags

    def get_paginator(self, operation: str) -> FakeLoadBalancerPaginator:
        if operation != "describe_load_balancers":
            raise AssertionError(operation)
        return FakeLoadBalancerPaginator(self.load_balancers)

    def describe_tags(self, ResourceArns: list[str]) -> dict[str, object]:
        return {"TagDescriptions": [{
            "ResourceArn": ResourceArns[0],
            "Tags": [
                {"Key": item["key"], "Value": item["value"]}
                for item in self.tags
            ],
        }]}

    def describe_listeners(self, LoadBalancerArn: str) -> dict[str, object]:
        return {"Listeners": []}


class FakeProtocolAws:
    def __init__(
        self,
        load_balancers: list[dict[str, object]],
        tags: list[dict[str, str]] = TAGS,
    ) -> None:
        self.bundle = Bundle(Path("/unused"), RUN_ID, SESSION_ID, runtime_outputs())
        self.elbv2 = FakeProtocolElbv2(load_balancers, tags)

    def client(self, service: str) -> FakeProtocolElbv2:
        if service != "elbv2":
            raise AssertionError(f"protocol verification advanced to {service}")
        return self.elbv2


def protocol_load_balancer() -> dict[str, object]:
    return {
        "LoadBalancerArn": (
            "arn:aws:elasticloadbalancing:ap-northeast-2:742711170910:"
            "loadbalancer/net/perf-p1-conn-proxy-60718200000/0123456789abcdef"
        ),
        "LoadBalancerName": "perf-p1-conn-proxy-60718200000",
        "DNSName": runtime_outputs()["ProtocolConnectDnsName"],
        "Scheme": "internal",
        "Type": "network",
        "State": {"Code": "active"},
        "VpcId": "vpc-exact",
        "SecurityGroups": ["sg-protocol"],
    }


def protocol_instances() -> dict[str, object]:
    return {
        "vpcId": "vpc-exact",
        "roleSecurityGroups": {
            "protocolLoadBalancer": "sg-protocol",
            "loadGenerator": "sg-load-generator",
            "haproxy": "sg-haproxy",
        },
    }


def protocol_contract() -> dict[str, object]:
    return {"snapshot": {"certificate": {
        "domainName": "events.example.test",
        "arn": "arn:aws:acm:ap-northeast-2:742711170910:certificate/exact",
    }}}


class DeploymentVerifierTest(unittest.TestCase):
    def test_protocol_nlb_name_is_derived_exactly_from_session(self) -> None:
        self.assertEqual(
            "perf-p1-conn-proxy-60718200000",
            expected_protocol_load_balancer_name(SESSION_ID),
        )
        with self.assertRaisesRegex(ValueError, "invalid Phase 7 session ID"):
            expected_protocol_load_balancer_name("phase7-integration-invalid")

    def test_protocol_nlb_accepts_real_no_prefix_dns_and_reaches_listener_gate(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exactly one listener"):
            verify_protocol_path(
                FakeProtocolAws([protocol_load_balancer()]),
                protocol_instances(),
                protocol_contract(),
                Path("/unused-ca.pem"),
            )

    def test_protocol_nlb_requires_one_dns_inventory_match(self) -> None:
        unmatched = protocol_load_balancer()
        unmatched["DNSName"] = "another-load-balancer.elb.ap-northeast-2.amazonaws.com"
        with self.assertRaisesRegex(RuntimeError, "one load balancer inventory record"):
            verify_protocol_path(
                FakeProtocolAws([unmatched]),
                protocol_instances(),
                protocol_contract(),
                Path("/unused-ca.pem"),
            )

    def test_protocol_nlb_authoritative_inventory_is_fail_closed(self) -> None:
        mutations = {
            "name": {"LoadBalancerName": "perf-p1-conn-proxy-wrong"},
            "scheme": {"Scheme": "internet-facing"},
            "type": {"Type": "application"},
            "state": {"State": {"Code": "provisioning"}},
            "vpc": {"VpcId": "vpc-wrong"},
            "security-group": {"SecurityGroups": ["sg-wrong"]},
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                load_balancer = protocol_load_balancer()
                load_balancer.update(mutation)
                with self.assertRaisesRegex(RuntimeError, "state, ownership, VPC"):
                    verify_protocol_path(
                        FakeProtocolAws([load_balancer]),
                        protocol_instances(),
                        protocol_contract(),
                        Path("/unused-ca.pem"),
                    )

        wrong_tags = [
            dict(item, value="wrong-run") if item["key"] == "RunId" else item
            for item in TAGS
        ]
        with self.assertRaisesRegex(RuntimeError, "state, ownership, VPC"):
            verify_protocol_path(
                FakeProtocolAws([protocol_load_balancer()], wrong_tags),
                protocol_instances(),
                protocol_contract(),
                Path("/unused-ca.pem"),
            )

    def test_expected_output_keys_cover_every_runtime_stack_cfn_output(self) -> None:
        source = (
            Path(__file__).resolve().parents[3] / "src/perf-phase7-integration-stack.ts"
        ).read_text(encoding="utf-8")
        runtime_class = source.split(
            "export class LoopAdPerfPhase7IntegrationStack extends Stack",
            1,
        )[1]
        synthesized_output_ids = set(re.findall(
            r"new CfnOutput\(this, '([^']+)'",
            runtime_class,
        ))
        self.assertEqual(EXPECTED_RUNTIME_OUTPUT_KEYS, synthesized_output_ids)

    def test_bundle_requires_the_exact_complete_runtime_output_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "run.json").write_text(json.dumps({
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
            }), encoding="utf-8")
            outputs = runtime_outputs()
            output_path = run_dir / "cdk-outputs.json"
            output_path.write_text(json.dumps({
                "LoopAdPerfPhase7IntegrationStack": outputs,
            }), encoding="utf-8")
            self.assertEqual(outputs, load_bundle(run_dir).outputs)

            outputs.pop("OhaImage")
            outputs["UnexpectedOutput"] = "must-not-pass"
            output_path.write_text(json.dumps({
                "LoopAdPerfPhase7IntegrationStack": outputs,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exact expected key set"):
                load_bundle(run_dir)

    def test_cloudformation_outputs_must_exactly_match_local_outputs(self) -> None:
        expected = runtime_outputs()
        rows = [
            {"OutputKey": key, "OutputValue": value}
            for key, value in expected.items()
        ]
        self.assertEqual(expected, verify_cloudformation_outputs({"Outputs": rows}, expected))

        wrong_value = [dict(row) for row in rows]
        wrong_value[0]["OutputValue"] = "different-runtime-resource"
        with self.assertRaisesRegex(RuntimeError, "do not exactly match"):
            verify_cloudformation_outputs({"Outputs": wrong_value}, expected)

        with self.assertRaisesRegex(RuntimeError, "missing="):
            verify_cloudformation_outputs({"Outputs": rows[1:]}, expected)

        unexpected = [*rows, {"OutputKey": "UnexpectedOutput", "OutputValue": "no"}]
        with self.assertRaisesRegex(RuntimeError, "unexpected="):
            verify_cloudformation_outputs({"Outputs": unexpected}, expected)

        duplicate = [*rows, dict(rows[0])]
        with self.assertRaisesRegex(RuntimeError, "duplicated"):
            verify_cloudformation_outputs({"Outputs": duplicate}, expected)

    def test_verify_deployment_stops_on_live_stack_output_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            inputs = run_dir / "inputs"
            inputs.mkdir()
            (inputs / "preflight.json").write_text(json.dumps({
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "passed": True,
                "snapshot": {},
            }), encoding="utf-8")
            images = runtime_images()
            (inputs / "image-manifest.json").write_text(json.dumps({
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "runtimeDeployed": False,
                "images": [
                    {
                        "role": role,
                        "repository": f"loop-ad/perf-phase7/{RUN_ID}/{role}",
                        **image,
                    }
                    for role, image in images.items()
                ],
            }), encoding="utf-8")
            stack_outputs = runtime_outputs()
            stack_outputs["StreamName"] = "another-run-stream"
            aws = FakeDeploymentAws(run_dir, stack_outputs)
            with self.assertRaisesRegex(RuntimeError, "do not exactly match"):
                verify_deployment(aws, run_dir / "unused-ca.pem")
            self.assertEqual("LoopAdPerfPhase7IntegrationStack", aws.cloudformation.stack_name)

    def test_ecs_task_contract_binds_run_owned_data_paths_to_outputs(self) -> None:
        outputs = runtime_outputs()
        expected = expected_ecs_task_definitions(
            Bundle(Path("/unused"), RUN_ID, SESSION_ID, outputs),
            runtime_images(),
        )
        collector = expected["Collector"][1]["collector"]["environment"]
        self.assertEqual(outputs["StreamName"], collector["LOOPAD_KINESIS_STREAM_NAME"])

        consumer = expected["Consumer"][1]["consumer"]["environment"]
        self.assertEqual(outputs["StreamName"], consumer["KINESIS_STREAM_NAME"])
        self.assertEqual(
            f"arn:aws:kinesis:ap-northeast-2:742711170910:stream/{outputs['StreamName']}",
            consumer["KINESIS_STREAM_ARN"],
        )
        self.assertEqual(outputs["LeaseTableName"], consumer["KCL_LEASE_TABLE_NAME"])
        self.assertEqual(
            outputs["WorkerMetricsTableName"],
            consumer["KCL_WORKER_METRICS_TABLE_NAME"],
        )
        self.assertEqual(
            outputs["CoordinatorStateTableName"],
            consumer["KCL_COORDINATOR_STATE_TABLE_NAME"],
        )
        self.assertEqual(outputs["ClickHouseEndpoint"], consumer["CLICKHOUSE_HTTP_URL"])
        self.assertEqual(outputs["ClickHouseSecretArn"], consumer["CLICKHOUSE_SECRET_ARN"])
        self.assertEqual(outputs["FailureBucketName"], consumer["FAILURE_BUCKET"])

        archive = expected["Archive"][1]["archive"]["environment"]
        self.assertEqual(outputs["ClickHouseEndpoint"], archive["CLICKHOUSE_HTTP_URL"])
        self.assertEqual(outputs["ArchiveBucketName"], archive["ARCHIVE_BUCKET"])
        self.assertEqual(runtime_images()["archive"]["digest"], archive["ARCHIVE_IMAGE_DIGEST"])

    def test_synthesized_health_retry_validator_is_general_and_schema_guard_specific(self) -> None:
        template = {"Resources": {
            "One": {"Type": "AWS::ECS::TaskDefinition", "Properties": {"ContainerDefinitions": [
                {"Name": "collector", "HealthCheck": {"Retries": 1}},
                {"Name": "schema-guard", "HealthCheck": {"Retries": 10}},
            ]}},
            "Two": {"Type": "AWS::ECS::TaskDefinition", "Properties": {"ContainerDefinitions": [
                {"Name": "consumer"},
            ]}},
        }}
        result = validate_template(template)
        self.assertEqual(10, result["schemaGuardRetries"])
        template["Resources"]["One"]["Properties"]["ContainerDefinitions"][0]["HealthCheck"]["Retries"] = 11
        with self.assertRaisesRegex(ValueError, "1..10"):
            validate_template(template)

    def test_deployment_contract_binds_exact_images_and_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            inputs = run_dir / "inputs"
            inputs.mkdir()
            (inputs / "preflight.json").write_text(json.dumps({
                "runId": RUN_ID, "sessionId": SESSION_ID, "passed": True,
                "snapshot": {},
            }), encoding="utf-8")
            images = {
                "runId": RUN_ID, "sessionId": SESSION_ID, "runtimeDeployed": False,
                "images": [
                    {
                        "role": role,
                        "repository": f"loop-ad/perf-phase7/{RUN_ID}/{role}",
                        "digest": f"sha256:{digit * 64}",
                        "exactImage": f"742711170910.dkr.ecr.ap-northeast-2.amazonaws.com/loop-ad/perf-phase7/{RUN_ID}/{role}@sha256:{digit * 64}",
                    }
                    for role, digit in (("collector", "1"), ("consumer", "2"), ("archive", "3"))
                ],
            }
            (inputs / "image-manifest.json").write_text(json.dumps(images), encoding="utf-8")
            result = deployment_contract(Bundle(run_dir, RUN_ID, SESSION_ID, {}))
            self.assertEqual({"collector", "consumer", "archive"}, set(result["images"]))
            images["images"][0]["exactImage"] = "not-digest-pinned"
            (inputs / "image-manifest.json").write_text(json.dumps(images), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid for collector"):
                deployment_contract(Bundle(run_dir, RUN_ID, SESSION_ID, {}))

    def test_security_group_edge_accepts_only_one_group_to_one_port(self) -> None:
        exact = [{
            "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
            "UserIdGroupPairs": [{"GroupId": "sg-load"}],
            "IpRanges": [], "Ipv6Ranges": [], "PrefixListIds": [],
        }]
        self.assertTrue(exact_group_edge(exact, "sg-load", 443))
        cidr = [dict(exact[0], IpRanges=[{"CidrIp": "0.0.0.0/0"}])]
        self.assertFalse(exact_group_edge(cidr, "sg-load", 443))
        self.assertFalse(exact_group_edge(exact, "sg-other", 443))

    def test_task_definition_checks_platform_roles_images_resources_and_health_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aws = FakeAws(Path(directory))
            response = {
                "tags": TAGS,
                "taskDefinition": {
                    "taskDefinitionArn": "arn:aws:ecs:ap-northeast-2:742711170910:task-definition/test:1",
                    "networkMode": "awsvpc",
                    "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
                    "taskRoleArn": "arn:aws:iam::742711170910:role/task-role",
                    "executionRoleArn": "arn:aws:iam::742711170910:role/execution-role",
                    "containerDefinitions": [{
                        "name": "schema-guard", "image": "repo@sha256:" + "3" * 64,
                        "cpu": 128, "memory": 256, "essential": True,
                        "environment": [{
                            "name": "CLICKHOUSE_HTTP_URL",
                            "value": "http://127.0.0.1:8123",
                        }],
                        "logConfiguration": {"logDriver": "awslogs"},
                        "healthCheck": {"retries": 10},
                    }],
                },
            }
            summary = validate_task_definition(aws, response, "ClickHouse", "ARM64", {
                "schema-guard": {
                    "image": "repo@sha256:" + "3" * 64,
                    "cpu": 128,
                    "memory": 256,
                    "environment": {"CLICKHOUSE_HTTP_URL": "http://127.0.0.1:8123"},
                },
            })
            self.assertEqual(10, summary["containers"][0]["healthRetries"])
            self.assertEqual(
                {"CLICKHOUSE_HTTP_URL": "http://127.0.0.1:8123"},
                summary["containers"][0]["verifiedEnvironment"],
            )
            response["taskDefinition"]["containerDefinitions"][0]["environment"][0]["value"] = (
                "http://wrong-endpoint:8123"
            )
            with self.assertRaisesRegex(RuntimeError, "container contract mismatch"):
                validate_task_definition(aws, response, "ClickHouse", "ARM64", {
                    "schema-guard": {
                        "image": "repo@sha256:" + "3" * 64,
                        "cpu": 128,
                        "memory": 256,
                        "environment": {"CLICKHOUSE_HTTP_URL": "http://127.0.0.1:8123"},
                    },
                })
            response["taskDefinition"]["containerDefinitions"][0]["environment"][0]["value"] = (
                "http://127.0.0.1:8123"
            )
            response["taskDefinition"]["containerDefinitions"][0]["healthCheck"]["retries"] = 11
            with self.assertRaisesRegex(RuntimeError, "1..10"):
                validate_task_definition(aws, response, "ClickHouse", "ARM64", {
                    "schema-guard": {"image": "repo@sha256:" + "3" * 64, "cpu": 128, "memory": 256},
                })


if __name__ == "__main__":
    unittest.main()

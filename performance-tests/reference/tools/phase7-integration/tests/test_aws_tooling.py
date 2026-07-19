from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


TEST_DIR = Path(__file__).resolve().parent
AWS_DIR = TEST_DIR.parent / "aws"
sys.path.insert(0, str(TEST_DIR))
sys.path.insert(0, str(AWS_DIR))

from cleanup import inventory_result  # noqa: E402
from common import Check  # noqa: E402
from cost_model import build_cost_model  # noqa: E402
from evidence_assembler import assemble_evidence  # noqa: E402
from evaluator import evaluate  # noqa: E402
import image_prep  # noqa: E402
import preflight as preflight_module  # noqa: E402
from preflight import evaluate_preflight  # noqa: E402
from runner import STAGE_TIMEOUT_SECONDS, admission_reserve_seconds, stage_gate  # noqa: E402
from runtime_stages import (  # noqa: E402
    balanced_event_documents,
    correctness_curl_command,
    parse_correctness_http_result,
)
from test_evidence_assembler import write_fixture  # noqa: E402


RUN_ID = "run_20260717_180000_phase7_integration"
SESSION_ID = "phase7-integration-20260717T180000Z"
X86_AMI = "ami-0123456789abcdef0"
ARM_AMI = "ami-0fedcba9876543210"
DNS_NAME = "event.api.dev.loop-ad.org"


def prices(now: str = "2026-07-17T10:00:00Z") -> dict[str, object]:
    return {
        "region": "ap-northeast-2",
        "asOf": now,
        "prices": {
            "collectorC6iXlargeHour": 0.192,
            "haproxyC6inXlargeHour": 0.2562,
            "generatorC6inLargeHour": 0.1281,
            "consumerC7gLargeHour": 0.0816,
            "clickHouseR7g2xlargeHour": 0.5168,
            "kinesisProvisionedShardHour": 0.0185,
            "kinesisPutPayloadUnit": 0.0000000204,
            "nlbHour": 0.0225,
            "nlbLcuHour": 0.006,
            "natGatewayHour": 0.059,
            "natGatewayDataGb": 0.059,
            "publicIpv4Hour": 0.005,
            "ebsGp3GbMonth": 0.0912,
            "ebsGp3ThroughputGibpsMonth": 46.08,
            "secretsManagerSecretMonth": 0.4,
            "secretsManagerApiRequest": 0.000005,
            "cloudWatchMetricMonth": 0.3,
            "cloudWatchLogIngestGb": 0.76,
            "cloudWatchLogStorageGbMonth": 0.033,
            "cloudWatchGetMetricDataMetric": 0.00001,
            "ecrStorageGbMonth": 0.1,
            "dynamoDbReadRequestUnit": 0.000000125,
            "dynamoDbWriteRequestUnit": 0.000000625,
            "dynamoDbStorageGbMonth": 0.285,
            "s3StandardStorageGbMonth": 0.025,
            "s3Tier1Request": 0.000005,
            "s3Tier2Request": 0.0000004,
        },
    }


def snapshot(image_state: str = "absent") -> dict[str, object]:
    now = "2026-07-17T10:00:00Z"
    repositories = []
    image_stack = None
    owned = []
    if image_state == "prepared":
        image_stack = {
            "name": "LoopAdPerfPhase7IntegrationImageStack",
            "arn": "arn:aws:cloudformation:ap-northeast-2:742711170910:stack/images/1",
            "status": "CREATE_COMPLETE",
            "tags": {
                "Project": "loop-ad", "Phase": "7", "ResourceScope": "run", "ManagedBy": "codex",
                "RunId": RUN_ID, "SessionId": SESSION_ID,
            },
            "outputs": {},
        }
        for role in ("collector", "consumer", "archive"):
            name = f"loop-ad/perf-phase7/{RUN_ID}/{role}"
            digest = "sha256:" + {"collector": "1", "consumer": "2", "archive": "3"}[role] * 64
            arn = f"arn:aws:ecr:ap-northeast-2:742711170910:repository/{name}"
            repositories.append({"name": name, "arn": arn, "mutability": "IMMUTABLE", "scanOnPush": True,
                                 "images": [{"digest": digest, "tags": ["frozen"]}]})
            owned.append(arn)
    return {
        "capturedAt": now,
        "region": "ap-northeast-2",
        "identity": {"account": "742711170910", "arn": "arn:aws:iam::742711170910:root"},
        "cliIdentity": {"account": "742711170910", "arn": "arn:aws:iam::742711170910:root"},
        "stacks": {"runtime": None, "image": image_stack},
        "ecrRepositories": repositories,
        "ownedTaggedResources": owned,
        "quota": {"standardVcpus": 80, "currentStandardVcpus": 4, "kinesisShardLimit": 1000, "kinesisOpenShardCount": 0},
        "offerings": {instance: ["ap-northeast-2a", "ap-northeast-2c"] for instance in (
            "c6i.xlarge", "c6in.xlarge", "c6in.large", "c7g.large", "r7g.2xlarge"
        )},
        "bootstrapVersion": 32,
        "amis": {
            "x86": {"imageId": X86_AMI, "state": "available", "architecture": "x86_64", "rootDeviceType": "ebs"},
            "arm": {"imageId": ARM_AMI, "state": "available", "architecture": "arm64", "rootDeviceType": "ebs"},
        },
        "certificate": {"status": "ISSUED", "domainName": DNS_NAME, "arn": "certificate"},
    }


def passing_evidence() -> dict[str, object]:
    return {
        "performance": {"actualRps": 50_000, "correctedP95Ms": 250, "transportErrorRate": 0, "http429": 0, "http5xx": 0},
        "counts": {name: 15_000_000 for name in ("http202", "collectorFinalAck", "kinesisAccepted", "clickHouseAccounted", "clickHouseLiveUnique")},
        "failures": {name: 0 for name in ("kinesisThrottle", "collectorFinalFailure", "kclTerminalFailure", "failureObjects", "clickHouseInsertErrors", "archiveFailures", "unexpectedRestarts", "oomKills")},
        "drain": {"seconds": 1200, "iteratorAgeProgressed": True, "visibilityP50Ms": 10, "visibilityP95Ms": 50, "visibilityP99Ms": 100},
        "resources": {
            "collector": {"cpuP95Percent": 60, "memoryP95Percent": 60},
            "consumer": {"cpuP95Percent": 60, "memoryP95Percent": 60},
            "clickHouse": {"cpuP95Percent": 60, "memoryP95Percent": 60, "filesystemPeakPercent": 70},
        },
        "haproxy": {"configSha256": "a" * 64, "activeBackends": 6, "maxQueue": 0, "http4xx": 0, "http5xx": 0, "prometheusCollected": True},
        "archive": {
            "rows": 15_000_000, "objects": 3, "objectRows": [5_000_000] * 3,
            "preDropSourceMinusArchive": 0, "preDropArchiveMinusSource": 0,
            "committedSourceMinusArchive": 0, "committedArchiveMinusSource": 0,
            "postDropReferenceMinusArchive": 0, "postDropArchiveMinusReference": 0,
            "sourceRowsAfterDrop": 0, "liveRowsAfterDrop": 15_000_000, "committedReRead": True,
            "overlappedScoreWindow": True, "cycleSeconds": 1200,
        },
        "cost": {"accruedUpperBoundUsd": 30, "maximumIncludingCleanupUsd": 39, "cleanupReserveUsd": 5},
        "requiredArtifacts": {"metrics": True, "logs": True, "cloudTrail": True, "checksums": True},
        "cleanup": {"allZero": True},
    }


def passing_diagnostic_evidence() -> dict[str, object]:
    evidence = passing_evidence()
    processed = 14_900_000
    evidence["identityMode"] = "balanced-pool-sampled-with-replacement"
    evidence["identityContract"] = {
        "predeclaredBeforeDeploy": True,
        "userApproved": True,
        "selectionWithReplacement": True,
        "warmupScorePoolsSeparated": True,
        "balancedShardCount": 120,
    }
    evidence["counts"] = {
        name: processed
        for name in (
            "http202", "collectorFinalAck", "kinesisAccepted",
            "kclProcessed", "clickHouseInserted",
        )
    }
    evidence["counts"].update({"clickHouseLiveUnique": 480, "fixturePoolRows": 480})
    evidence["archive"]["liveRowsAfterDrop"] = 480
    return evidence


class AwsToolingTest(unittest.TestCase):
    def test_preflight_cross_checks_cli_identity_without_credential_environment(self) -> None:
        completed = mock.Mock(stdout=json.dumps({
            "Account": "742711170910", "Arn": "arn:aws:iam::742711170910:root",
        }))
        with mock.patch.dict(preflight_module.os.environ, {}, clear=True), \
             mock.patch.object(preflight_module.subprocess, "run", return_value=completed) as run:
            identity = preflight_module.collect_cli_identity()
        self.assertEqual({
            "account": "742711170910", "arn": "arn:aws:iam::742711170910:root",
        }, identity)
        self.assertIn("--region", run.call_args.args[0])
        with mock.patch.dict(preflight_module.os.environ, {"AWS_SECRET_ACCESS_KEY": "forbidden"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "fresh aws login"):
                preflight_module.collect_cli_identity()

    def test_preflight_rejects_credential_environment_before_creating_a_session(self) -> None:
        with mock.patch.dict(preflight_module.os.environ, {"AWS_ACCESS_KEY_ID": "forbidden"}, clear=True), \
             mock.patch.object(preflight_module.boto3, "Session") as session:
            with self.assertRaisesRegex(RuntimeError, "fresh aws login"):
                preflight_module.AwsSnapshot()
        session.assert_not_called()

    def test_runtime_correctness_fixture_covers_all_120_shards_without_credentials(self) -> None:
        prefix = "phase7-run-correctness-"
        documents = balanced_event_documents(prefix, 1_000, datetime(2026, 7, 17, tzinfo=UTC))
        self.assertEqual(1_000, len(documents))
        self.assertEqual(1_000, len({document["event_id"] for document in documents}))
        expected_http_keys = {
            "project_id",
            "write_key",
            "schema_version",
            "event_id",
            "event_name",
            "event_time",
            "source",
            "user_id",
            "session_id",
            "properties_json",
        }
        self.assertTrue(all(set(document) == expected_http_keys for document in documents))
        shards = {
            int.from_bytes(__import__("hashlib").md5(document["event_id"].encode(), usedforsecurity=False).digest(), "big") * 120 // (1 << 128)
            for document in documents
        }
        self.assertEqual(set(range(120)), shards)
        command = correctness_curl_command(
            "compressed-payload",
            "event.api.dev.loop-ad.org",
            "perf-p1-conn-proxy-run.elb.ap-northeast-2.amazonaws.com",
        )
        self.assertIn("--http2", command)
        self.assertIn("--connect-timeout 5", command)
        self.assertIn("--max-time 15", command)
        self.assertIn("xargs -0", command)
        self.assertIn("jq -cn", command)
        self.assertNotRegex(command, r"AWS_(?:ACCESS|SECRET|SESSION)")

    def test_runtime_correctness_parses_complete_multiline_ssm_json(self) -> None:
        output = '{\n  "http202": 0,\n  "non202": 1000,\n  "total": 1000\n}\n'
        self.assertEqual(
            {"http202": 0, "non202": 1000, "total": 1000},
            parse_correctness_http_result(output),
        )
        with self.assertRaisesRegex(RuntimeError, "do not add up"):
            parse_correctness_http_result('{"http202":999,"non202":0,"total":1000}')
        with self.assertRaisesRegex(RuntimeError, "one JSON document"):
            parse_correctness_http_result("}")

    def test_isolated_docker_config_preserves_buildx_plugin_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            docker_config = Path(parent) / "docker-config"
            plugin_directory = Path(parent) / "plugins"
            with mock.patch.object(image_prep, "docker_plugin_directories", return_value=[plugin_directory]):
                environment = image_prep.isolated_docker_environment(docker_config)
            self.assertEqual(str(docker_config), environment["DOCKER_CONFIG"])
            self.assertEqual(
                {"cliPluginsExtraDirs": [str(plugin_directory)]},
                json.loads((docker_config / "config.json").read_text(encoding="utf-8")),
            )

    def test_image_prep_checks_buildx_before_creating_aws_resources(self) -> None:
        with mock.patch.object(image_prep, "isolated_docker_environment", return_value={"DOCKER_CONFIG": "/tmp/config"}), \
             mock.patch.object(image_prep, "run") as run:
            image_prep.assert_docker_build_capability(Path("/infra"))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([
            ["docker", "buildx", "version"],
            ["docker", "buildx", "inspect", "--bootstrap"],
        ], commands)

    def test_image_prep_checks_local_collector_commit_before_paid_resources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as parent:
            repository = Path(parent) / "loop-ad_event_collector"
            repository.mkdir()
            with mock.patch.object(image_prep, "run") as run:
                image_prep.assert_collector_source_capability(
                    repository,
                    Path("/infra"),
                )
            self.assertEqual(
                [
                    "git",
                    "-C",
                    str(repository),
                    "cat-file",
                    "-e",
                    f"{image_prep.COLLECTOR_COMMIT}^{{commit}}",
                ],
                run.call_args.args[0],
            )
            self.assertIsNone(run.call_args.kwargs.get("deadline"))

        with self.assertRaisesRegex(FileNotFoundError, "local Git working tree"):
            image_prep.assert_collector_source_capability(
                Path("/does-not-exist"),
                Path("/infra"),
            )

    def test_cost_model_preserves_log_cap_and_cleanup_reserve(self) -> None:
        result = build_cost_model(prices())
        self.assertTrue(result["passed"], result)
        self.assertLess(Decimal(result["operationalMaximumUsd"]), Decimal("55"))
        self.assertLessEqual(Decimal(result["maximumIncludingCleanupUsd"]), Decimal("60"))
        public_ipv4 = next(
            component for component in result["components"]
            if component["name"] == "NAT gateway public IPv4 address"
        )
        self.assertEqual("3", public_ipv4["quantity"])
        self.assertEqual("0.005", public_ipv4["unitPriceUsd"])
        self.assertEqual("0.015000", public_ipv4["costUsd"])
        campaign = build_cost_model(
            prices(), accrued_upper_bound_usd=Decimal("19")
        )
        self.assertTrue(campaign["passed"], campaign)
        self.assertEqual("19.000000", campaign["accruedUpperBoundUsd"])
        self.assertLess(Decimal(campaign["operationalMaximumUsd"]), Decimal("55"))
        self.assertLessEqual(
            Decimal(campaign["maximumIncludingCleanupUsd"]), Decimal("60")
        )
        budget_exhausted = build_cost_model(
            prices(), accrued_upper_bound_usd=Decimal("22")
        )
        self.assertFalse(budget_exhausted["passed"])
        self.assertFalse(
            budget_exhausted["checks"]["operationalMaximumBelowNewLoadStop"]
        )
        blocked = build_cost_model(prices(), Decimal("5.000001"))
        self.assertFalse(blocked["checks"]["haproxyLogVolumeAtOrBelowFiveGiB"])

    def test_absent_and_prepared_preflight_modes_fail_closed(self) -> None:
        model = build_cost_model(prices())
        handoff = {"localRunPath": "/tmp/local", "implementationTreeSha256": "f" * 64}
        gate = [Check("handoff", True, True, True, "fixture")]
        absent = evaluate_preflight(snapshot(), handoff, gate, prices(), model, RUN_ID, SESSION_ID,
                                    "absent", X86_AMI, ARM_AMI, DNS_NAME)
        self.assertTrue(absent["passed"], absent["checks"])
        manifest = {"images": [
            {"role": role, "repository": f"loop-ad/perf-phase7/{RUN_ID}/{role}",
             "digest": "sha256:" + digit * 64,
             "architecture": "linux/amd64" if role == "collector" else "linux/arm64"}
            for role, digit in (("collector", "1"), ("consumer", "2"), ("archive", "3"))
        ]}
        prepared = evaluate_preflight(snapshot("prepared"), handoff, gate, prices(), model, RUN_ID,
                                      SESSION_ID, "prepared", X86_AMI, ARM_AMI, DNS_NAME, manifest)
        self.assertTrue(prepared["passed"], prepared["checks"])
        bad = snapshot("prepared")
        bad["stacks"]["runtime"] = {"status": "CREATE_COMPLETE"}
        self.assertFalse(evaluate_preflight(bad, handoff, gate, prices(), model, RUN_ID, SESSION_ID,
                                           "prepared", X86_AMI, ARM_AMI, DNS_NAME, manifest)["passed"])

    def test_runner_forces_cleanup_after_160_minutes_but_never_blocks_cleanup(self) -> None:
        start = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
        document = {"completedStages": ["deploy", "verify", "correctness", "seed", "warmup"],
                    "failedStage": None, "paidStartedAt": start.isoformat()}
        model = {"passed": True, "operationalMaximumUsd": "30", "maximumIncludingCleanupUsd": "39"}
        timeout = STAGE_TIMEOUT_SECONDS["score_archive"]
        reserve = admission_reserve_seconds("score_archive", timeout)
        latest = start + timedelta(seconds=160 * 60 - reserve)
        self.assertTrue(stage_gate(document, "score_archive", latest, model, timeout)["allowed"])
        blocked = stage_gate(document, "score_archive", latest + timedelta(seconds=1), model, timeout)
        self.assertFalse(blocked["allowed"])
        self.assertTrue(blocked["cleanupRequired"])
        self.assertTrue(stage_gate(document, "cleanup", start + timedelta(minutes=181), model)["allowed"])

    def test_evaluator_enforces_strict_resource_limit_and_exact_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            evidence = assemble_evidence(run_dir)
            self.assertEqual("passed", evaluate(evidence, run_dir)["verdict"])
            evidence["resources"]["collector"]["cpuP95Percent"] = 70
            failed = evaluate(evidence)
            self.assertEqual("failed", failed["verdict"])
            self.assertIn("hostCpuAndMemoryP95Below70", failed["failedChecks"])

    def test_evaluator_rejects_even_one_transport_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            evidence = assemble_evidence(run_dir)
            evidence["performance"]["transportErrors"] = 1
            evidence["performance"]["attemptedRequests"] += 1
            evidence["performance"]["transportErrorRate"] = 1 / evidence["performance"]["attemptedRequests"]
            failed = evaluate(evidence, run_dir)
            self.assertEqual("failed", failed["verdict"])
            self.assertIn("transportErrorsZero", failed["failedChecks"])
            self.assertTrue(failed["checks"]["transportErrorRateAtMostPoint001"])

    def test_evaluator_uses_current_55_60_campaign_cost_limits(self) -> None:
        evidence = passing_evidence()
        evidence["cost"] = {
            "accruedUpperBoundUsd": "54.999999",
            "maximumIncludingCleanupUsd": "60.000000",
            "cleanupReserveUsd": "5.000000",
        }
        self.assertTrue(evaluate(evidence)["checks"]["costWithinApprovedLimits"])

        evidence["cost"]["accruedUpperBoundUsd"] = "55.000000"
        self.assertFalse(evaluate(evidence)["checks"]["costWithinApprovedLimits"])

        evidence["cost"]["accruedUpperBoundUsd"] = "54.999999"
        evidence["cost"]["maximumIncludingCleanupUsd"] = "60.000001"
        self.assertFalse(evaluate(evidence)["checks"]["costWithinApprovedLimits"])

    def test_evaluator_accepts_only_predeclared_balanced_pool_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            evidence = assemble_evidence(run_dir)
            result = evaluate(evidence, run_dir)
            self.assertEqual("passed", result["verdict"], result)
            self.assertEqual("balanced-pool-sampled-with-replacement", result["identityMode"])
            evidence["counts"]["kclProcessed"] -= 1
            failed = evaluate(evidence)
            self.assertEqual("failed", failed["verdict"])
            self.assertIn("scoreCountInvariantExact", failed["failedChecks"])

    def test_evaluator_rejects_unapproved_sampling_relaxation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            evidence = assemble_evidence(run_dir)
            evidence["identityContract"]["userApproved"] = False
            failed = evaluate(evidence)
            self.assertEqual("failed", failed["verdict"])
            self.assertIn("identityContractValid", failed["failedChecks"])

    def test_cleanup_inventory_requires_service_counts_and_tagging_api_zero(self) -> None:
        resources = {"cloudFormationStacks": [], "ec2Instances": [], "ecrRepositories": []}
        result = inventory_result({"account": "742711170910"}, RUN_ID, SESSION_ID, resources, ["stale-tagging-arn"])
        self.assertFalse(result["allZero"])
        self.assertFalse(result["taggingApiAuthoritative"])
        resources["ecrRepositories"] = ["owned"]
        self.assertFalse(inventory_result({}, RUN_ID, SESSION_ID, resources, [])["allZero"])


if __name__ == "__main__":
    unittest.main()

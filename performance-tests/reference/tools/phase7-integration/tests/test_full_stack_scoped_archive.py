from __future__ import annotations

from decimal import Decimal
from argparse import Namespace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


TEST_DIR = Path(__file__).resolve().parent
AWS_DIR = TEST_DIR.parent / "aws"
sys.path.insert(0, str(TEST_DIR))
sys.path.insert(0, str(AWS_DIR))

import common as common_module  # noqa: E402
from common import (  # noqa: E402
    PHASE7_COLLECTOR_COMMIT,
    file_sha256,
    image_source_closure_sha256,
    read_json,
    reject_strict_paid_work_under_composite_policy,
    scoped_diagnostic_source_checks,
    validate_identifiers,
    write_json,
)
from preflight import evaluate_preflight  # noqa: E402
from full_stack_scoped_cost_model import (  # noqa: E402
    build_cost_model,
    canonical_sha256,
    validate_campaign_ledger,
    validate_cost_model,
)
from full_stack_scoped_archive import (  # noqa: E402
    ATTEMPT_TYPE,
    RUNTIME_STACK,
    STAGE_PLAN,
    ZERO_ATTEMPT_STAGES,
    archive_task_identity,
    collect_evidence,
    execute_callable_stage,
    finalize_cleaned_attempt_ledger,
    finalize_cleaned_early_failure_ledger,
    initialize,
    json_safe,
    list_exact_archive_tasks,
    source_drop_observation,
    stage_time_budget,
    terminal_campaign_ledger,
    validate_inputs,
)
from build_phase8_composite_handoff import (  # noqa: E402
    anchored_entry,
    archive_basis,
    performance_basis,
    scoped_source_anchor,
)
from build_full_stack_scoped_source import (  # noqa: E402
    EXTRA_IMPLEMENTATION_FILES,
    focused_gate_passes,
)
from evidence_assembler import CLEANUP_SERVICE_CLASSES  # noqa: E402
from runtime_stages import archive_evidence  # noqa: E402
from test_aws_tooling import (  # noqa: E402
    ARM_AMI,
    DNS_NAME,
    RUN_ID,
    SESSION_ID,
    X86_AMI,
    prices,
    snapshot,
)
from test_phase7_archive_runtime import FakeRuntime, FakeS3  # noqa: E402


ATTEMPT_21_FIX = (
    TEST_DIR.parent.parent
    / "phase7_2-stabilization"
    / "attempt-21-fix-verification.json"
)
ATTEMPT_22_FIX = (
    TEST_DIR.parent.parent
    / "phase7_2-stabilization"
    / "attempt-22-fix-verification.json"
)


def campaign_ledger(prior: str) -> dict[str, object]:
    entry = {
        "ordinal": 1,
        "previousEntrySha256": None,
        "runId": "run_20260717_000000_phase7_integration",
        "sessionId": "phase7-integration-20260717T000000Z",
        "verdict": "failed",
    }
    entry["entrySha256"] = canonical_sha256(entry)
    return {
        "schemaVersion": 1,
        "campaign": "phase7-2-stabilization",
        "status": "stabilizing",
        "attempts": [entry],
        "ledgerHeadSha256": entry["entrySha256"],
        "activeAttempt": None,
        "budget": {
            "activeEpochId": "test-active-epoch",
            "activeEpochAccruedUpperBoundUsd": prior,
            "hardCapUsd": "60.000000",
            "newPaidWorkStopUsd": "55.000000",
            "cleanupReserveUsd": "5.000000",
            "currentAttemptOrdinal": None,
            "currentAttemptPaidStartAt": None,
        },
        "budgetEpochs": [{
            "epochId": "test-active-epoch",
            "status": "active",
            "accruedUpperBoundUsd": prior,
        }],
    }


def diagnostic_cost(
    price_document: dict[str, object], prior: str
) -> tuple[dict[str, object], dict[str, object]]:
    ledger = campaign_ledger(prior)
    return build_cost_model(price_document, ledger, promotion_policy()), ledger


def promotion_policy() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "recordType": "phase7-2-composite-phase8-promotion-policy",
        "decision": "promote-after-minimal-smoke-and-archive-without-new-50k",
        "phase5": "skipped",
        "execution": {
            "new50kRpsAttempt": False,
            "newWarmupAttempt": False,
            "newScoreAttempt": False,
        },
        "phase8": {
            "paidAwsExperiment": False,
            "defaultAwsMutation": False,
        },
        "budget": {
            "activeEpochHardCapUsd": "60.000000",
            "cleanupReserveUsd": "5.000000",
        },
    }


class JsonSafeEvidenceTest(unittest.TestCase):
    def test_boto_datetime_is_recursively_normalized_before_json_write(self) -> None:
        observed = json_safe({
            "createdAt": datetime(2026, 7, 19, 13, 20, 8, tzinfo=UTC),
            "nested": [{"stoppedAt": datetime(2026, 7, 19, 13, 29, 23, tzinfo=UTC)}],
            "status": "STOPPED",
        })
        self.assertEqual("2026-07-19T13:20:08Z", observed["createdAt"])
        self.assertEqual(
            "2026-07-19T13:29:23Z", observed["nested"][0]["stoppedAt"]
        )
        self.assertEqual("STOPPED", observed["status"])
        json.dumps(observed)

    def test_cloudwatch_log_events_use_forward_token_not_a_missing_paginator(self) -> None:
        logs = mock.Mock()
        group_pages = mock.Mock()
        group_pages.paginate.return_value = [{
            "logGroups": [{"logGroupName": f"/loopad/perf/phase7/{RUN_ID}/ArchiveLogs"}]
        }]
        stream_pages = mock.Mock()
        stream_pages.paginate.return_value = [{
            "logStreams": [{"logStreamName": "archive/one"}]
        }]
        logs.get_paginator.side_effect = lambda name: {
            "describe_log_groups": group_pages,
            "describe_log_streams": stream_pages,
        }[name]
        logs.get_log_events.side_effect = [
            {"events": [{"message": "first"}], "nextForwardToken": "next"},
            {"events": [{"message": "second"}], "nextForwardToken": "next"},
        ]
        trail = mock.Mock()
        trail.lookup_events.return_value = {"Events": []}
        aws = SimpleNamespace(
            bundle=SimpleNamespace(run_id=RUN_ID, session_id=SESSION_ID),
            assert_identity=lambda: None,
            client=lambda service: {"logs": logs, "cloudtrail": trail}[service],
        )

        result = collect_evidence(aws, datetime(2026, 7, 19, tzinfo=UTC))

        self.assertTrue(result["passed"])
        self.assertEqual(2, result["cloudWatch"]["logGroups"][0]["eventCount"])
        self.assertEqual(2, logs.get_log_events.call_count)
        self.assertEqual("next", logs.get_log_events.call_args.kwargs["nextToken"])
        self.assertIsInstance(logs.get_log_events.call_args.kwargs["endTime"], int)


def add_scoped_provenance(
    root: Path, source: dict[str, object]
) -> mock._patch:
    baseline = root / common_module.SCOPED_BASELINE_PATH
    policy = root / common_module.SCOPED_POLICY_PATH
    promotion = root / common_module.SCOPED_PROMOTION_POLICY_PATH
    baseline.parent.mkdir(parents=True, exist_ok=True)
    policy.parent.mkdir(parents=True, exist_ok=True)
    promotion.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text('{"finalVerdict":"passed"}\n', encoding="utf-8")
    policy.write_text('{"decision":"full-stack"}\n', encoding="utf-8")
    promotion.write_text(json.dumps(promotion_policy()) + "\n", encoding="utf-8")
    focused = []
    for relative in sorted(common_module.SCOPED_FOCUSED_GATE_PATHS):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status":"passed"}\n', encoding="utf-8")
        focused.append({
            "path": relative,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "status": "passed",
        })
    baseline_sha = hashlib.sha256(baseline.read_bytes()).hexdigest()
    policy_sha = hashlib.sha256(policy.read_bytes()).hexdigest()
    promotion_sha = hashlib.sha256(promotion.read_bytes()).hexdigest()
    source.update({
        "attemptType": ATTEMPT_TYPE,
        "stackDefinitions": [
            "LoopAdPerfPhase7IntegrationImageStack",
            "LoopAdPerfPhase7IntegrationStack",
        ],
        "topologyBaseline": "Attempt 17",
        "baselineHandoff": {
            "path": common_module.SCOPED_BASELINE_PATH,
            "sha256": baseline_sha,
        },
        "policy": {
            "path": common_module.SCOPED_POLICY_PATH,
            "sha256": policy_sha,
        },
        "compositePromotionPolicy": {
            "path": common_module.SCOPED_PROMOTION_POLICY_PATH,
            "sha256": promotion_sha,
        },
        "focusedGates": focused,
        "stagePlan": list(STAGE_PLAN),
        "zeroAttemptStages": list(ZERO_ATTEMPT_STAGES),
    })
    return mock.patch.multiple(
        common_module,
        SCOPED_BASELINE_SHA256=baseline_sha,
        SCOPED_POLICY_SHA256=policy_sha,
        SCOPED_PROMOTION_POLICY_SHA256=promotion_sha,
    )


class RetainSourceRuntime(FakeRuntime):
    def clickhouse(self, _query: str, *, timeout: int = 600):
        self.clickhouse_timeout = timeout
        return [{"rows": 15_000_000}]


class FullStackScopedArchiveTest(unittest.TestCase):
    def test_attempt_22_fix_gate_requires_prepaid_local_collector_validation(
        self,
    ) -> None:
        document = read_json(ATTEMPT_22_FIX)

        self.assertTrue(
            focused_gate_passes(
                "performance-tests/phase7_2-stabilization/"
                "attempt-22-fix-verification.json",
                document,
            )
        )
        document["collectorSource"]["validatedBeforePaidBoundary"] = False
        self.assertFalse(
            focused_gate_passes(
                "performance-tests/phase7_2-stabilization/"
                "attempt-22-fix-verification.json",
                document,
            )
        )

    def test_attempt_21_fix_gate_accepts_operational_envelope_and_cleanup_zero(
        self,
    ) -> None:
        document = read_json(ATTEMPT_21_FIX)

        self.assertTrue(
            focused_gate_passes(
                "performance-tests/phase7_2-stabilization/"
                "attempt-21-fix-verification.json",
                document,
            )
        )
        document["changes"]["archiveQueryMemory"]["exactPointAcceptance"] = True
        self.assertFalse(
            focused_gate_passes(
                "performance-tests/phase7_2-stabilization/"
                "attempt-21-fix-verification.json",
                document,
            )
        )

    def test_current_composite_policy_rejects_strict_paid_work(self) -> None:
        root = TEST_DIR.parents[2]
        with self.assertRaisesRegex(RuntimeError, "strict paid work is disabled"):
            reject_strict_paid_work_under_composite_policy(root)

    def test_composite_entry_and_source_require_external_immutable_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            implementation = root / "implementation.txt"
            implementation.write_text("frozen\n", encoding="utf-8")
            digest = file_sha256(implementation)
            combined = hashlib.sha256()
            combined.update(b"implementation.txt\0")
            combined.update(digest.encode())
            combined.update(b"\n")
            tree = combined.hexdigest()
            source = {
                "recordType": "phase7-full-stack-scoped-diagnostic-source",
                "awsDiagnosticReady": True,
                "promotionEligible": False,
                "phase5": "skipped",
                "unresolvedFailures": [],
                "gitCommit": "c" * 40,
                "gitTree": "d" * 40,
                "implementationFiles": [
                    {"path": "implementation.txt", "sha256": digest}
                ],
                "implementationTreeSha256": tree,
            }
            provenance_patch = add_scoped_provenance(root, source)
            source_path = root / "source.json"
            write_json(source_path, source)
            entry = {
                "entrySha256": "e" * 64,
                "gitCommit": source["gitCommit"],
                "implementationGitTree": source["gitTree"],
                "implementationSourceClosureSha256": tree,
                "evidencePaths": {"scopedSource": "source.json"},
                "immutableInputHashes": {"scopedSource": file_sha256(source_path)},
                "imageSourceHashes": {
                    role: image_source_closure_sha256(role, tree)
                    for role in ("archive", "collector", "consumer")
                },
            }
            entry_path = root / "runtime/campaign-ledger-entry.json"
            write_json(entry_path, entry)
            anchor = anchored_entry(
                root,
                entry,
                "runtime/campaign-ledger-entry.json",
                label="test",
            )
            self.assertEqual(entry["entrySha256"], anchor["entrySha256"])
            with provenance_patch:
                source_binding = scoped_source_anchor(root, entry)
                self.assertEqual(tree, source_binding["implementationTreeSha256"])
                rewritten = json.loads(json.dumps(entry))
                rewritten["gitCommit"] = "f" * 40
                with self.assertRaisesRegex(RuntimeError, "not bound"):
                    scoped_source_anchor(root, rewritten)
            rewritten_entry = json.loads(json.dumps(entry))
            rewritten_entry["entrySha256"] = "a" * 64
            with self.assertRaisesRegex(RuntimeError, "anchor differs"):
                anchored_entry(
                    root,
                    rewritten_entry,
                    "runtime/campaign-ledger-entry.json",
                    label="test",
                )

    def test_stage_plan_uses_attempt17_stack_and_runs_no_load_or_drop(self) -> None:
        self.assertEqual("LoopAdPerfPhase7IntegrationStack", RUNTIME_STACK)
        self.assertEqual(
            ("deploy", "verify", "seed", "archive", "collect", "cleanup", "inventory"),
            STAGE_PLAN,
        )
        self.assertEqual(
            ("correctness", "replacement", "warmup", "score", "source-drop"),
            ZERO_ATTEMPT_STAGES,
        )
        self.assertEqual("aws-full-stack-scoped-diagnostic", ATTEMPT_TYPE)
        self.assertIn(
            "performance-tests/phase7-integration/archive/targeted_seed.py",
            EXTRA_IMPLEMENTATION_FILES,
        )

    def test_archive_task_identity_matches_existing_one_shot_contract(self) -> None:
        identity = archive_task_identity(RUN_ID, SESSION_ID)
        expected = hashlib.sha256(f"phase7-archive-v1\0{RUN_ID}".encode()).hexdigest()
        self.assertEqual(f"p7a-{expected[:60]}", identity["clientToken"])
        self.assertEqual("phase7-archive-20260717180000", identity["startedBy"])
        self.assertLessEqual(len(identity["clientToken"]), 64)

    def test_retain_source_archive_evidence_rejects_drop_and_preserves_15m(self) -> None:
        s3 = FakeS3()
        s3.worker.update({
            "diagnosticSourceRetention": True,
            "dropExecuted": False,
            "postDrop": None,
            "sourceRowsAfter": 15_000_000,
        })
        result = archive_evidence(
            RetainSourceRuntime(s3), retain_source_after_commit=True
        )
        self.assertTrue(result["diagnosticSourceRetention"])
        self.assertFalse(result["dropExecuted"])
        self.assertEqual(15_000_000, result["sourceRowsAfterArchive"])
        self.assertIsNone(result["sourceRowsAfterDrop"])
        self.assertIsNone(result["postDropReferenceMinusArchive"])

    def test_scoped_source_is_distinct_from_strict_handoff_and_rehashes_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            implementation = root / "implementation.txt"
            implementation.write_text("frozen\n", encoding="utf-8")
            file_sha = hashlib.sha256(implementation.read_bytes()).hexdigest()
            combined = hashlib.sha256()
            combined.update(b"implementation.txt\0")
            combined.update(file_sha.encode())
            combined.update(b"\n")
            source = {
                "recordType": "phase7-full-stack-scoped-diagnostic-source",
                "awsDiagnosticReady": True,
                "promotionEligible": False,
                "phase5": "skipped",
                "unresolvedFailures": [],
                "implementationFiles": [{"path": "implementation.txt", "sha256": file_sha}],
                "implementationTreeSha256": combined.hexdigest(),
            }
            provenance_patch = add_scoped_provenance(root, source)
            path = root / "source.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with provenance_patch:
                checks, observed = scoped_diagnostic_source_checks(root, path)
                self.assertTrue(all(check.passed for check in checks), checks)
                self.assertFalse(observed["promotionEligible"])
                implementation.write_text("changed\n", encoding="utf-8")
                checks, _ = scoped_diagnostic_source_checks(root, path)
                self.assertFalse(all(check.passed for check in checks))

    def test_one_operational_hour_and_cleanup_fit_active_60_cap_without_strict(self) -> None:
        model, _ = diagnostic_cost(prices(), "0.95")
        self.assertTrue(model["passed"], model)
        self.assertLessEqual(
            Decimal(model["projectedCampaignMaximumIncludingCleanupUsd"]),
            Decimal("60"),
        )
        self.assertEqual("0.000000", model["phase8PaidAwsExperimentOperationalUpperBoundUsd"])
        self.assertNotIn("reservedStrictCostModel", model)
        self.assertEqual("1", model["paidWallClockHours"])
        self.assertEqual("0", next(
            item["quantity"] for item in model["components"]
            if item["name"] == "Kinesis PUT payload units"
        ))

    def test_cost_model_rejects_forged_phase8_policy(self) -> None:
        price_document = prices()
        model, ledger = diagnostic_cost(price_document, "0.95")
        model["phase8PromotionPolicy"]["execution"]["new50kRpsAttempt"] = True
        self.assertFalse(validate_cost_model(price_document, ledger, model))

    def test_cost_model_accepts_a_lower_log_allowance_but_rejects_over_cap(self) -> None:
        price_document = prices()
        ledger = campaign_ledger("0.95")
        lower = build_cost_model(
            price_document, ledger, promotion_policy(), Decimal("0.25")
        )
        self.assertTrue(lower["passed"])
        with self.assertRaisesRegex(ValueError, "at most 0.5 GiB"):
            build_cost_model(
                price_document, ledger, promotion_policy(), Decimal("0.500001")
            )

    def test_attempt21_charge_plus_one_hour_retry_stays_inside_active_cap(self) -> None:
        ledger = campaign_ledger("19.902431")
        active = {
            "ordinal": 2,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "activeEpochId": "test-active-epoch",
        }
        entry = {
            "ordinal": 2,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "previousEntrySha256": ledger["ledgerHeadSha256"],
            "verdict": "failed",
            "cost": {"chargedUpperBoundUsd": "18.952431"},
        }
        entry["entrySha256"] = canonical_sha256(entry)
        provisional = terminal_campaign_ledger(
            ledger,
            active,
            entry,
            active_accrued=Decimal("38.854862"),
            next_scoped_charge=Decimal("0"),
            updated_at="2026-07-19T16:00:00Z",
        )
        next_model = build_cost_model(
            prices(), provisional, promotion_policy()
        )
        next_charge = Decimal(next_model["chargedOperationalUpperBoundUsd"])
        finalized = terminal_campaign_ledger(
            ledger,
            active,
            entry,
            active_accrued=Decimal("38.854862"),
            next_scoped_charge=next_charge,
            updated_at="2026-07-19T16:00:00Z",
        )

        self.assertTrue(next_model["passed"])
        self.assertLessEqual(
            Decimal(next_model["maximumIncludingCleanupUsd"]), Decimal("60")
        )
        self.assertEqual("stabilizing", finalized["status"])
        self.assertTrue(finalized["budget"]["newPaidWorkAuthorized"])

    def test_attempt17_performance_basis_is_inherited_without_rewriting_failure(self) -> None:
        root = TEST_DIR.parents[2]
        ledger = read_json(
            root / "performance-tests/phase7_2-stabilization/attempt-ledger.json"
        )
        policy = read_json(
            root
            / "performance-tests/phase7_2-stabilization/"
            "phase8-composite-promotion-policy-20260719.json"
        )
        result = performance_basis(root, ledger["attempts"][16], policy)
        self.assertEqual("failed", result["immutableVerdict"])
        self.assertFalse(result["verdictRewritten"])
        self.assertGreaterEqual(result["score"]["actualRps"], 49_500)
        self.assertEqual(15_000_000, result["score"]["completedRequests"])

    def test_archive_basis_requires_minimal_smoke_archive_and_cleanup_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = {
                "schemaVersion": 1,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "passed": True,
                "clickHouse": {"query_ok": 1, "schema_tables": 2},
                "protocolPath": {
                    "generatorReadiness": {"i-1": {"tlsHttp2Health": True}}
                },
            }
            checks = {
                name: True
                for name in (
                    "archiveTaskExitZero",
                    "retainSourceMode",
                    "rowsExact",
                    "threePartsExact",
                    "preDropEquivalent",
                    "committedEquivalent",
                    "commitRereadImmutable",
                    "sourceFingerprintRetained",
                    "code241Zero",
                    "sourceDropQueryZero",
                    "clickHouseTaskUnchanged",
                    "clickHouseNoNewStoppedServiceTask",
                )
            }
            archive = {
                "schemaVersion": 1,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "passed": True,
                "checks": checks,
                "queryLog": {"code241Exceptions": 0, "sourceDropQueries": 0},
                "archive": {
                    "rows": 15_000_000,
                    "sourceRowsAfterArchive": 15_000_000,
                    "objects": 3,
                    "objectRows": [5_000_000, 5_000_000, 5_000_000],
                    "diagnosticSourceRetention": True,
                    "dropExecuted": False,
                    "preDropSourceMinusArchive": 0,
                    "preDropArchiveMinusSource": 0,
                    "committedSourceMinusArchive": 0,
                    "committedArchiveMinusSource": 0,
                },
            }
            cleanup = {
                "schemaVersion": 1,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "allZero": True,
                "serviceInventoryZero": True,
                "taggingApiResidualsZero": True,
            }
            references = {}
            for key, value in (
                ("deploymentVerification", deployment),
                ("archive", archive),
                ("cleanup", cleanup),
            ):
                path = root / f"{key}.json"
                write_json(path, value)
                references[key] = {"path": path.name, "sha256": file_sha256(path)}
            entry = {
                "ordinal": 20,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "attemptType": ATTEMPT_TYPE,
                "promotionEligible": False,
                "phase5": "skipped",
                "verdict": "passed",
                "entrySha256": "e" * 64,
                "gitCommit": "c" * 40,
                "implementationSourceClosureSha256": "f" * 64,
                "imageSourceHashes": {
                    role: digit * 64
                    for role, digit in (
                        ("archive", "1"),
                        ("collector", "2"),
                        ("consumer", "3"),
                    )
                },
                "imageDigests": {
                    role: f"sha256:{digit * 64}"
                    for role, digit in (
                        ("archive", "4"),
                        ("collector", "5"),
                        ("consumer", "6"),
                    )
                },
                "stageAttemptCounts": {
                    **{
                        stage: 1
                        for stage in (
                            "imagePreparation",
                            "imageStackDeploy",
                            "runtimeDeploy",
                            "verify",
                            "seed15M",
                            "archive",
                            "collect",
                            "cleanup",
                            "inventory",
                        )
                    },
                    **{
                        stage: 0
                        for stage in (
                            "correctness",
                            "replacement",
                            "warmup",
                            "score",
                            "source-drop",
                        )
                    },
                },
                "cleanup": {
                    "finalAuthoritativeInventory": {
                        "allZero": True,
                        "serviceInventoryZero": True,
                        "taggingApiResidualsZero": True,
                        "taggingApiResiduals": [],
                    }
                },
                "terminalEvidenceHashes": references,
            }
            result = archive_basis(root, entry)
            self.assertTrue(result["minimalSmoke"]["passed"])
            self.assertTrue(result["archive"]["passed"])
            self.assertFalse(result["attemptPromotionEligible"])

    def test_terminal_pass_disables_paid_work_and_prepares_composite_handoff(self) -> None:
        ledger = campaign_ledger("0.950000")
        active = {
            "ordinal": 2,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "activeEpochId": "test-active-epoch",
        }
        entry = {
            "ordinal": 2,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "previousEntrySha256": ledger["ledgerHeadSha256"],
            "verdict": "passed",
            "cost": {"chargedUpperBoundUsd": "10.000000"},
        }
        entry["entrySha256"] = canonical_sha256(entry)
        finalized = terminal_campaign_ledger(
            ledger,
            active,
            entry,
            active_accrued=Decimal("10.950000"),
            next_scoped_charge=Decimal("10.000000"),
            updated_at="2026-07-19T12:00:00Z",
        )
        self.assertEqual("stabilizing", finalized["status"])
        self.assertFalse(finalized["budget"]["newPaidWorkAuthorized"])
        self.assertEqual(
            "ready-for-phase8-handoff",
            finalized["promotionCandidate"]["status"],
        )

    def test_terminal_failure_budget_exhaustion_is_revalidatable(self) -> None:
        ledger = campaign_ledger("0.950000")
        active = {
            "ordinal": 2,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "activeEpochId": "test-active-epoch",
        }
        entry = {
            "ordinal": 2,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "previousEntrySha256": ledger["ledgerHeadSha256"],
            "verdict": "failed",
            "cost": {"chargedUpperBoundUsd": "53.050000"},
        }
        entry["entrySha256"] = canonical_sha256(entry)
        finalized = terminal_campaign_ledger(
            ledger,
            active,
            entry,
            active_accrued=Decimal("54.000000"),
            next_scoped_charge=Decimal("2.000000"),
            updated_at="2026-07-19T12:00:00Z",
        )
        self.assertEqual("budget-exhausted", finalized["status"])
        self.assertFalse(finalized["budget"]["newPaidWorkAuthorized"])
        with self.assertRaises(ValueError):
            validate_campaign_ledger(finalized)
        validate_campaign_ledger(finalized, allow_terminal_status=True)

    def test_archive_task_inventory_uses_legal_ecs_filter_combinations(self) -> None:
        calls = []

        class Paginator:
            def paginate(self, **kwargs):
                calls.append(kwargs)
                return [{"taskArns": [f"arn:{kwargs['desiredStatus'].lower()}"]}]

        class Ecs:
            def get_paginator(self, name):
                self.assert_name = name
                return Paginator()

            def describe_tasks(self, *, cluster, tasks):
                status = tasks[0].split(":")[-1].upper()
                return {
                    "failures": [],
                    "tasks": [{
                        "taskArn": tasks[0],
                        "startedBy": "phase7-archive-test",
                        "lastStatus": status,
                    }],
                }

        runtime = SimpleNamespace(
            bundle=SimpleNamespace(outputs={"ArchiveClusterName": "owned-cluster"}),
            client=lambda service: Ecs(),
        )
        self.assertEqual(
            ["arn:running"],
            list_exact_archive_tasks(
                runtime, "phase7-archive-test", "RUNNING"
            ),
        )
        self.assertEqual(
            [{"cluster": "owned-cluster", "desiredStatus": "RUNNING"}],
            calls,
        )
        self.assertNotIn("startedBy", calls[0])

    def test_false_collect_result_is_preserved_and_fails_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
            (run_dir / "run.json").write_text(json.dumps({
                "cleanupStartDeadline": future,
                "hardDeadline": future,
                "stageAttempts": [],
                "completedStages": [],
            }), encoding="utf-8")
            args = Namespace(runtime_dir=run_dir)
            with self.assertRaisesRegex(RuntimeError, "collect returned passed=false"):
                execute_callable_stage(
                    args, "collect", lambda: {"passed": False}, "metrics-summary.json"
                )
            self.assertFalse(read_json(run_dir / "run.json")["stageAttempts"][0]["passed"])
            self.assertFalse(read_json(run_dir / "metrics-summary.json")["passed"])

    def test_noncleanup_stage_cannot_cross_cleanup_start_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            future = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
            (run_dir / "run.json").write_text(json.dumps({
                "cleanupStartDeadline": past,
                "hardDeadline": future,
            }), encoding="utf-8")
            with self.assertRaises(TimeoutError):
                stage_time_budget(Namespace(runtime_dir=run_dir), "archive", 60)

    def test_source_drop_is_unknown_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            self.assertIsNone(source_drop_observation(run_dir))
            (run_dir / "failure-evidence.json").write_text(json.dumps({
                "queryLog": {"sourceDropQueries": 0},
            }), encoding="utf-8")
            self.assertFalse(source_drop_observation(run_dir))

    def test_scoped_preflight_accepts_only_scoped_cost_workload(self) -> None:
        price_document = prices()
        model, ledger = diagnostic_cost(price_document, "0.95")
        source = {"implementationTreeSha256": "f" * 64}
        gate = []
        result = evaluate_preflight(
            snapshot(),
            source,
            gate,
            price_document,
            model,
            RUN_ID,
            SESSION_ID,
            "absent",
            X86_AMI,
            ARM_AMI,
            DNS_NAME,
            source_kind="full-stack-scoped-diagnostic-source",
            source_path="/tmp/source.json",
            campaign_ledger=ledger,
        )
        self.assertTrue(result["passed"], result["checks"])
        self.assertFalse(result["promotionEligible"])
        self.assertEqual(ATTEMPT_TYPE, result["attemptType"])

    def test_initializer_seals_only_existing_full_stack_with_dynamic_prior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            implementation = root / "implementation.txt"
            implementation.write_text("frozen\n", encoding="utf-8")
            file_sha = hashlib.sha256(implementation.read_bytes()).hexdigest()
            combined = hashlib.sha256()
            combined.update(b"implementation.txt\0")
            combined.update(file_sha.encode())
            combined.update(b"\n")
            tree = combined.hexdigest()
            source = {
                "recordType": "phase7-full-stack-scoped-diagnostic-source",
                "awsDiagnosticReady": True,
                "promotionEligible": False,
                "phase5": "skipped",
                "unresolvedFailures": [],
                "implementationFiles": [{"path": "implementation.txt", "sha256": file_sha}],
                "implementationTreeSha256": tree,
            }
            provenance_patch = add_scoped_provenance(root, source)
            source_path = root / "source.json"
            source_path.write_text(json.dumps(source), encoding="utf-8")
            paid_started_at = datetime.now(UTC).isoformat()
            marker = root / "images-paid-start.json"
            marker.write_text(json.dumps({
                "schemaVersion": 1,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "paidStartedAt": paid_started_at,
                "stage": "image-preparation",
                "cleanupRequiredOnFailure": True,
            }), encoding="utf-8")
            images = {
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "attemptType": ATTEMPT_TYPE,
                "promotionEligible": False,
                "runtimeDeployed": False,
                "implementationTreeSha256": tree,
                "collectorCommit": PHASE7_COLLECTOR_COMMIT,
                "paidStartedAt": paid_started_at,
                "preparedAt": datetime.now(UTC).isoformat(),
                "paidStartEvidencePath": str(marker),
                "paidStartEvidenceSha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
                "images": [
                    {
                        "role": role,
                        "architecture": "linux/amd64" if role == "collector" else "linux/arm64",
                        "repository": f"loop-ad/perf-phase7/{RUN_ID}/{role}",
                        "digest": f"sha256:{digit * 64}",
                        "sourceClosureSha256": image_source_closure_sha256(
                            role, tree
                        ),
                    }
                    for role, digit in (("collector", "1"), ("consumer", "2"), ("archive", "3"))
                ],
            }
            price_document = prices(datetime.now(UTC).isoformat())
            cost, ledger = diagnostic_cost(price_document, "1.25")
            cost_authorization = {
                "campaignLedgerSha256": cost["campaignLedgerSha256"],
                "priceDocumentSha256": cost["priceDocumentSha256"],
                "phase8PromotionPolicySha256": cost[
                    "phase8PromotionPolicySha256"
                ],
            }
            preflight = {
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "passed": True,
                "imageState": "prepared",
                "attemptType": ATTEMPT_TYPE,
                "promotionEligible": False,
                "sourceAuthorization": {"implementationTreeSha256": tree},
                "costAuthorization": cost_authorization,
                "imageAuthorization": {
                    "imageManifestSha256": canonical_sha256(images),
                    "digests": {
                        item["role"]: item["digest"] for item in images["images"]
                    },
                },
                "snapshot": {
                    "amis": {
                        "x86": {"imageId": X86_AMI},
                        "arm": {"imageId": ARM_AMI},
                    },
                    "certificate": {
                        "arn": "arn:aws:acm:ap-northeast-2:742711170910:certificate/00000000-0000-4000-8000-000000000001",
                        "domainName": DNS_NAME,
                    },
                },
            }
            readiness = root / "readiness"
            readiness.mkdir()
            ca = readiness / "ca.pem"
            ca.write_text("not-a-real-certificate\n", encoding="utf-8")
            cdk = root / "node_modules/.bin/cdk"
            cdk.parent.mkdir(parents=True)
            cdk.write_text("#!/bin/sh\n", encoding="utf-8")
            ledger_path = (
                root
                / "performance-tests/phase7_2-stabilization/attempt-ledger.json"
            )
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            paths = {}
            for name, value in (
                ("preflight.json", preflight),
                ("images.json", images),
                ("cost.json", cost),
                ("prices.json", price_document),
            ):
                path = root / name
                path.write_text(json.dumps(value), encoding="utf-8")
                paths[name] = path
            ledger["activeAttempt"] = {
                "ordinal": 2,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "attemptType": ATTEMPT_TYPE,
                "promotionEligible": False,
                "state": "images-prepared",
                "activeEpochId": "test-active-epoch",
                "activeEpochPriorUpperBoundUsd": "1.250000",
                "paidStartedAt": paid_started_at,
                "admissionLedgerSha256": cost["campaignLedgerSha256"],
                "chargedOperationalUpperBoundUsd": cost[
                    "chargedOperationalUpperBoundUsd"
                ],
                "maximumIncludingCleanupUsd": cost["maximumIncludingCleanupUsd"],
                "costAuthorization": cost_authorization,
                "immutableInputs": {
                    "absentPreflightSha256": "a" * 64,
                },
                "paidStartEvidence": {
                    "sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
                },
                "imageManifest": {
                    "sha256": hashlib.sha256(
                        paths["images.json"].read_bytes()
                    ).hexdigest(),
                },
                "imagePreparationAttempts": 1,
                "imageStackDeployAttempts": 1,
                "stageMaximumAttempts": {
                    "imagePreparation": 1,
                    "imageStackDeploy": 1,
                },
            }
            ledger["budget"]["currentAttemptOrdinal"] = 2
            ledger["budget"]["currentAttemptPaidStartAt"] = paid_started_at
            ledger["budget"]["currentAttemptReservedOperationalUpperBoundUsd"] = cost[
                "chargedOperationalUpperBoundUsd"
            ]
            ledger["budget"]["currentAttemptMaximumIncludingCleanupUsd"] = cost[
                "maximumIncludingCleanupUsd"
            ]
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            args = Namespace(
                infra_root=root,
                run_id=RUN_ID,
                session_id=SESSION_ID,
                scoped_diagnostic_source=source_path,
                prepared_preflight=paths["preflight.json"],
                image_manifest=paths["images.json"],
                cost_model=paths["cost.json"],
                prices=paths["prices.json"],
                attempt_ledger=ledger_path,
                readiness_dir=readiness,
                runtime_dir=root / "runtime",
                ca_certificate=ca,
            )
            with provenance_patch:
                inputs = validate_inputs(args)
            command_set = initialize(args, inputs)
            self.assertEqual(["LoopAdPerfPhase7IntegrationStack"], [
                item for item in command_set["deployment"]["argv"]
                if item.endswith("Stack")
            ])
            self.assertNotIn("ArchiveDiagnostic", json.dumps(command_set))
            self.assertEqual("1.250000", command_set["activeEpochPriorUpperBoundUsd"])
            self.assertFalse(read_json(root / "runtime/run.json")["sourceDropAuthorized"])

            cleanup_path = root / "runtime/cleanup-verification.json"
            cleanup = {
                "schemaVersion": 1,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "counts": {name: 0 for name in CLEANUP_SERVICE_CLASSES},
                "resources": {name: [] for name in CLEANUP_SERVICE_CLASSES},
                "taggingApiResiduals": [],
                "taggingApiAuthoritative": False,
                "serviceInventoryZero": True,
                "taggingApiResidualsZero": True,
                "allZero": True,
            }
            write_json(cleanup_path, cleanup)
            run_path = root / "runtime/run.json"
            run = read_json(run_path)
            run.update({
                "status": "finalized",
                "verdict": "failed",
                "finalizedAt": datetime.now(UTC).isoformat(),
                "failedStage": "identity-before-deploy",
                "failure": {
                    "errorType": "RuntimeError",
                    "error": "identity check failed",
                },
                "cleanupInventoryZero": True,
                "finalCleanupVerificationPath": "cleanup-verification.json",
                "stageAttempts": [
                    {"stage": "cleanup", "attempt": 1, "passed": True},
                    {"stage": "inventory", "attempt": 1, "passed": True},
                ],
                "completedStages": ["cleanup", "inventory"],
            })
            write_json(run_path, run)
            ledger = read_json(ledger_path)
            active = dict(ledger["activeAttempt"])
            active.update({
                "state": "terminal-cleaned-awaiting-ledger-entry",
                "terminalVerdict": "failed",
                "terminalAt": run["finalizedAt"],
            })
            ledger["activeAttempt"] = active
            write_json(ledger_path, ledger)
            execution_summary = {
                "schemaVersion": 1,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "verdict": "failed",
            }
            write_json(root / "runtime/execution-summary.json", execution_summary)
            write_json(
                root
                / "performance-tests/phase7_2-stabilization/"
                "attempt-2-fix-verification.json",
                {
                    "attemptOrdinal": 2,
                    "sourceRunId": RUN_ID,
                    "sourceSessionId": SESSION_ID,
                    "status": "passed",
                    "fixCommit": "f" * 40,
                    "summary": "focused fix passed",
                },
            )
            entry = finalize_cleaned_attempt_ledger(
                args,
                inputs,
                {
                    **execution_summary,
                    "cleanupInventoryZero": True,
                    "firstFailingGate": "identity-before-deploy",
                    "sourceDropExecuted": False,
                },
            )
            finalized_ledger = read_json(ledger_path)
            self.assertEqual(2, len(finalized_ledger["attempts"]))
            self.assertEqual(entry["entrySha256"], finalized_ledger["ledgerHeadSha256"])
            self.assertEqual(
                build_cost_model(
                    price_document,
                    finalized_ledger,
                    promotion_policy(),
                    allow_active_ledger=False,
                )["chargedOperationalUpperBoundUsd"],
                entry["cost"]["nextRetryOperationalUpperBoundUsd"],
            )
            self.assertIsNone(finalized_ledger["activeAttempt"])
            self.assertEqual(
                cost["operationalMaximumUsd"],
                finalized_ledger["budget"]["activeEpochAccruedUpperBoundUsd"],
            )
            self.assertTrue(
                (root / "performance-tests/phase7_2-stabilization/resume.md").is_file()
            )

    def test_cost_caps_and_identifier_timestamp_binding_fail_closed(self) -> None:
        price_document = prices()
        model, ledger = diagnostic_cost(price_document, "0.95")
        lowered_diagnostic = json.loads(json.dumps(model))
        lowered_diagnostic["logPolicy"]["ingestUpperBoundGiB"] = "0.4"
        self.assertFalse(
            validate_cost_model(price_document, ledger, lowered_diagnostic)
        )
        enabled_50k = json.loads(json.dumps(model))
        enabled_50k["phase8PromotionPolicy"]["execution"][
            "new50kRpsAttempt"
        ] = True
        self.assertFalse(validate_cost_model(price_document, ledger, enabled_50k))
        with self.assertRaisesRegex(ValueError, "timestamps must match exactly"):
            validate_identifiers(
                RUN_ID,
                "phase7-integration-20260717T000001Z",
            )

    def test_paid_early_failure_auto_appends_and_charges_cleanup_overrun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            implementation = root / "implementation.txt"
            implementation.write_text("frozen\n", encoding="utf-8")
            file_sha = hashlib.sha256(implementation.read_bytes()).hexdigest()
            combined = hashlib.sha256()
            combined.update(b"implementation.txt\0")
            combined.update(file_sha.encode())
            combined.update(b"\n")
            tree = combined.hexdigest()
            source = {
                "recordType": "phase7-full-stack-scoped-diagnostic-source",
                "awsDiagnosticReady": True,
                "promotionEligible": False,
                "phase5": "skipped",
                "unresolvedFailures": [],
                "gitCommit": "c" * 40,
                "gitTree": "d" * 40,
                "implementationFiles": [
                    {"path": "implementation.txt", "sha256": file_sha}
                ],
                "implementationTreeSha256": tree,
            }
            provenance_patch = add_scoped_provenance(root, source)
            source_path = root / "source.json"
            source_path.write_text(json.dumps(source), encoding="utf-8")
            price_document = prices(datetime.now(UTC).isoformat())
            cost, ledger = diagnostic_cost(price_document, "1.25")
            cost_path = root / "cost.json"
            write_json(cost_path, cost)
            paid_at = datetime.now(UTC) - timedelta(minutes=121)
            active = {
                "ordinal": 2,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "attemptType": ATTEMPT_TYPE,
                "promotionEligible": False,
                "state": "image-preparation-failed-cleaned",
                "activeEpochId": "test-active-epoch",
                "activeEpochPriorUpperBoundUsd": "1.250000",
                "chargedOperationalUpperBoundUsd": cost[
                    "chargedOperationalUpperBoundUsd"
                ],
                "maximumIncludingCleanupUsd": cost[
                    "maximumIncludingCleanupUsd"
                ],
                "admissionLedgerSha256": cost["campaignLedgerSha256"],
                "paidStartedAt": paid_at.isoformat(),
                "imagePreparationAttempts": 1,
                "imageStackDeployAttempts": 1,
                "runtimeDeployAttempts": 0,
                "stageMaximumAttempts": {
                    "imagePreparation": 1,
                    "imageStackDeploy": 1,
                },
                "immutableInputs": {
                    "sourceSha256": hashlib.sha256(
                        source_path.read_bytes()
                    ).hexdigest(),
                    "costModelSha256": hashlib.sha256(
                        cost_path.read_bytes()
                    ).hexdigest(),
                    "pricesSha256": "b" * 64,
                    "absentPreflightSha256": "a" * 64,
                },
            }
            ledger["activeAttempt"] = active
            ledger["budget"].update({
                "currentAttemptOrdinal": 2,
                "currentAttemptPaidStartAt": paid_at.isoformat(),
                "currentAttemptReservedOperationalUpperBoundUsd": cost[
                    "chargedOperationalUpperBoundUsd"
                ],
                "currentAttemptMaximumIncludingCleanupUsd": cost[
                    "maximumIncludingCleanupUsd"
                ],
            })
            ledger_path = (
                root
                / "performance-tests/phase7_2-stabilization/attempt-ledger.json"
            )
            write_json(ledger_path, ledger)
            readiness = root / "readiness"
            readiness.mkdir()
            cleanup_path = readiness / "cleanup.json"
            write_json(cleanup_path, {
                "schemaVersion": 1,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "counts": {name: 0 for name in CLEANUP_SERVICE_CLASSES},
                "resources": {name: [] for name in CLEANUP_SERVICE_CLASSES},
                "taggingApiResiduals": [],
                "taggingApiAuthoritative": False,
                "serviceInventoryZero": True,
                "taggingApiResidualsZero": True,
                "allZero": True,
                "cleanupDeadlineBreached": True,
            })
            failure_path = readiness / "failure.json"
            write_json(failure_path, {
                "schemaVersion": 1,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "failedAt": datetime.now(UTC).isoformat(),
                "errorType": "RuntimeError",
                "error": "image build failed",
            })
            args = Namespace(
                infra_root=root,
                run_id=RUN_ID,
                session_id=SESSION_ID,
                scoped_diagnostic_source=source_path,
                cost_model=cost_path,
                attempt_ledger=ledger_path,
                output=readiness / "images.json",
            )
            with provenance_patch:
                entry = finalize_cleaned_early_failure_ledger(
                    args,
                    failure_stage="image-preparation",
                    error=RuntimeError("image build failed"),
                    cleanup_path=cleanup_path,
                    failure_path=failure_path,
                    evidence_dir=readiness,
                )
            finalized = read_json(ledger_path)
            self.assertEqual("5.000000", entry["cost"]["cleanupOverrunUpperBoundUsd"])
            self.assertIsNone(finalized["activeAttempt"])
            self.assertTrue(finalized["budget"]["newPaidWorkAuthorized"])
            self.assertEqual("stabilizing", finalized["status"])
            self.assertIsNone(
                finalized["budget"]["nextFullAttemptOperationalUpperBoundUsd"]
            )
            self.assertEqual(entry["entrySha256"], finalized["ledgerHeadSha256"])


if __name__ == "__main__":
    unittest.main()

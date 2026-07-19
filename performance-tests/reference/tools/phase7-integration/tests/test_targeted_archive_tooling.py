from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PHASE7 = Path(__file__).resolve().parents[1]
AWS_DIR = PHASE7 / "aws"
PHASE6 = PHASE7.parent / "phase6-archive"
sys.path.insert(0, str(AWS_DIR))
sys.path.insert(0, str(PHASE6))

import targeted_archive_common as common  # noqa: E402
import targeted_archive_cleanup as cleanup  # noqa: E402
import targeted_archive_cost_model as cost_model  # noqa: E402
import targeted_archive_runtime as runtime  # noqa: E402


def load_targeted_seed():
    path = PHASE7 / "archive" / "targeted_seed.py"
    spec = importlib.util.spec_from_file_location("phase7_targeted_seed", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load targeted seed entrypoint")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TargetedArchiveToolingTest(unittest.TestCase):
    def test_targeted_implementation_includes_shared_readiness_source_and_test(self) -> None:
        self.assertIn(
            "src/perf-phase7-integration-stack.ts",
            common.TARGETED_IMPLEMENTATION,
        )
        self.assertIn(
            "test/perf-phase7-integration.test.ts",
            common.TARGETED_IMPLEMENTATION,
        )

    def test_targeted_identity_is_fresh_and_timestamp_locked(self) -> None:
        run_id = "run_20260719_120000_phase7_archive_diagnostic"
        session_id = "phase7-archive-diagnostic-20260719T120000Z"
        common.validate_identifiers(run_id, session_id)
        self.assertEqual(
            "loop-ad/perf-phase7-targeted/"
            "run_20260719_120000_phase7_archive_diagnostic/archive",
            common.repository_name(run_id),
        )
        with self.assertRaisesRegex(ValueError, "timestamps"):
            common.validate_identifiers(
                run_id,
                "phase7-archive-diagnostic-20260719T120001Z",
            )

    def test_cost_model_charges_the_ceiling_rounded_model_and_preserves_strict_retry(self) -> None:
        prices = {name: 0 for name in cost_model.REQUIRED_PRICES}
        result = cost_model.build({
            "source": "AWS Price List GetProducts, public On-Demand USD",
            "asOf": "2026-07-19T12:00:00Z",
            "region": "ap-northeast-2",
            "prices": prices,
        })
        self.assertTrue(result["passed"])
        self.assertEqual("0.750000", result["chargedOperationalUpperBoundUsd"])
        self.assertEqual("39.620718", result["maximumIncludingTargetedStrictAndCleanupUsd"])

    def test_cost_model_fails_when_targeted_and_strict_no_longer_fit(self) -> None:
        prices = {name: 0 for name in cost_model.REQUIRED_PRICES}
        prices["clickHouseR7g2xlargeHour"] = 10
        result = cost_model.build({
            "source": "AWS Price List GetProducts, public On-Demand USD",
            "asOf": "2026-07-19T12:00:00Z",
            "region": "ap-northeast-2",
            "prices": prices,
        })
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["strictRetryAndCleanupStillFit"])

    def test_terminal_cleanup_refuses_unrecognized_owned_tombstone(self) -> None:
        run_id = "run_20260719_120000_phase7_archive_diagnostic"
        session_id = "phase7-archive-diagnostic-20260719T120000Z"
        tagging = mock.Mock()
        tagging.get_paginator.return_value.paginate.return_value = [{
            "ResourceTagMappingList": [{
                "ResourceARN": (
                    "arn:aws:sns:ap-northeast-2:742711170910:"
                    "unexpected-targeted-residual"
                ),
                "Tags": [
                    {"Key": key, "Value": value}
                    for key, value in common.expected_tags(
                        run_id, session_id
                    ).items()
                ],
            }],
        }]
        session = mock.Mock()
        session.client.return_value = tagging
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            cleanup.cleanup_terminal_residuals(session, run_id, session_id)

    def test_equivalence_requires_three_zero_bidirectional_differences(self) -> None:
        evidence = {
            "passed": True,
            "expectedMetrics": {"rows": 15_000_000},
            "archiveMetrics": {"rows": 15_000_000},
            "twoWayDifferences": [
                {"leftMinusRight": 0, "rightMinusLeft": 0}
                for _ in range(3)
            ],
        }
        self.assertTrue(runtime.equivalence_passed(evidence))
        evidence["twoWayDifferences"][1]["rightMinusLeft"] = 1
        self.assertFalse(runtime.equivalence_passed(evidence))

    def test_post_archive_verifier_requires_source_retention_and_no_code241_or_drop(self) -> None:
        targeted_seed = load_targeted_seed()
        calls = []
        with mock.patch.object(
            targeted_seed,
            "execute",
            side_effect=["", "15000000\t15000000", "0\t0"],
        ) as execute, mock.patch.object(
            targeted_seed,
            "put_result",
            side_effect=lambda _run, _name, result: calls.append(result),
        ):
            targeted_seed.verify(
                "run_20260719_120000_phase7_archive_diagnostic",
                date(2026, 7, 11),
                date(2026, 7, 19),
            )
        self.assertEqual(15_000_000, calls[0]["sourceRowsAfter"])
        self.assertEqual(0, calls[0]["code241Exceptions"])
        self.assertEqual(0, calls[0]["sourceDropQueries"])
        query_log_sql = execute.call_args_list[2].args[0]
        self.assertIn("query_kind = 'Alter'", query_log_sql)
        self.assertIn("DROP PARTITION", query_log_sql)

    def test_post_archive_verifier_publishes_failure_before_exit(self) -> None:
        targeted_seed = load_targeted_seed()
        published = []
        with mock.patch.object(
            targeted_seed,
            "execute",
            side_effect=["", "15000000\t15000000", "1\t0"],
        ), mock.patch.object(
            targeted_seed,
            "put_result",
            side_effect=lambda _run, _name, result: published.append(result),
        ):
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                targeted_seed.verify(
                    "run_20260719_120000_phase7_archive_diagnostic",
                    date(2026, 7, 11),
                    date(2026, 7, 19),
                )
        self.assertEqual("failed", published[0]["status"])
        self.assertEqual(1, published[0]["code241Exceptions"])

    def test_runtime_interrupt_is_recorded_and_cleanup_still_reports(self) -> None:
        run_id = "run_20260719_120000_phase7_archive_diagnostic"
        session_id = "phase7-archive-diagnostic-20260719T120000Z"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_dir = root / "runtime"
            readiness_dir = root / "readiness"
            runtime_dir.mkdir()
            readiness_dir.mkdir()
            image_manifest = root / "image-manifest.json"
            cost = root / "cost-model.json"
            image_manifest.write_text("{}\n", encoding="utf-8")
            cost.write_text(
                json.dumps({"chargedOperationalUpperBoundUsd": "0.950000"}),
                encoding="utf-8",
            )
            argv = [
                "targeted_archive_runtime.py",
                "--infra-root", str(root),
                "--run-id", run_id,
                "--session-id", session_id,
                "--arm-ami", "ami-0123456789abcdef0",
                "--image-manifest", str(image_manifest),
                "--cost-model", str(cost),
                "--readiness-dir", str(readiness_dir),
                "--runtime-dir", str(runtime_dir),
            ]
            cleanup_result = {"schemaVersion": 1, "passed": True}
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                runtime, "execute", side_effect=KeyboardInterrupt,
            ), mock.patch.object(
                runtime, "locked_session", return_value=mock.Mock(),
            ), mock.patch.object(
                runtime, "cleanup", return_value=cleanup_result,
            ):
                self.assertEqual(2, runtime.main())

            failure = json.loads((runtime_dir / "failure.json").read_text())
            report = json.loads((runtime_dir / "report.json").read_text())
            self.assertEqual("KeyboardInterrupt", failure["errorType"])
            self.assertTrue(failure["interrupted"])
            self.assertEqual("failed", report["verdict"])
            self.assertTrue(report["cleanupPassed"])


if __name__ == "__main__":
    unittest.main()

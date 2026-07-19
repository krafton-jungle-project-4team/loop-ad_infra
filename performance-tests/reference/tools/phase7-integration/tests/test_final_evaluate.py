from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


TEST_DIR = Path(__file__).resolve().parent
AWS_DIR = TEST_DIR.parent / "aws"
sys.path.insert(0, str(TEST_DIR))
sys.path.insert(0, str(AWS_DIR))

from final_evaluate import final_evaluate  # noqa: E402
from evaluator import numeric  # noqa: E402
from evidence_assembler import required_artifacts_are_valid  # noqa: E402
from runner import ProcessOutcome, finish_attempt  # noqa: E402
from test_evidence_assembler import (  # noqa: E402
    attempt_control_document,
    json_bytes,
    iso,
    stage_attempt,
    write_fixture,
    write_runner_control,
)


class FinalEvaluateTest(unittest.TestCase):
    def test_numeric_evidence_rejects_all_nonfinite_values(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity", float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(123.0, numeric(value, 123.0))

    def test_finalizer_assembles_then_evaluates_and_atomically_finalizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            result = final_evaluate(run_dir)

            self.assertEqual("passed", result["verdict"])
            self.assertTrue((run_dir / "final-evidence.json").is_file())
            self.assertTrue((run_dir / "final-evaluation.json").is_file())
            finalized = __import__("json").loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("completed", finalized["status"])
            self.assertEqual("passed", finalized["verdict"])
            self.assertEqual("skipped", finalized["phase5"])

    def test_final_evidence_revalidates_after_parent_runner_finishes_evaluate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)

            result = final_evaluate(run_dir)
            self.assertEqual("passed", result["verdict"])
            final_evidence = json.loads(
                (run_dir / "final-evidence.json").read_text(encoding="utf-8")
            )

            # Simulate execute_stage's post-subprocess state transition and
            # numbered evaluate attempt control file.
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            evaluate_attempt = run["stageAttempts"][-1]
            finish_attempt(
                run,
                int(evaluate_attempt["ordinal"]),
                ProcessOutcome(0, "", ""),
                iso(136),
                None,
            )
            run["inProgressStage"] = None
            run["completedStages"].append("evaluate")
            run["status"] = "finalized"
            run["finalizedAt"] = iso(136)
            (run_dir / "evidence/control/evaluate.attempt-1.json").write_bytes(
                json_bytes(attempt_control_document(run["stageAttempts"][-1]))
            )
            (run_dir / "run.json").write_bytes(json_bytes(run))

            self.assertTrue(required_artifacts_are_valid(final_evidence, run_dir))

    def test_missing_required_artifact_finalizes_as_inconclusive_without_fabrication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            (run_dir / "archive-validation.json").unlink()

            result = final_evaluate(run_dir)

            self.assertEqual("inconclusive", result["verdict"])
            self.assertEqual("required-evidence-incomplete", result["verdictBasis"])
            self.assertFalse((run_dir / "final-evidence.json").exists())
            status = json.loads((run_dir / "final-evidence-status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["complete"])
            self.assertFalse(status["artifacts"]["archiveValidation"]["present"])
            finalized = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("inconclusive", finalized["verdict"])

    def test_known_hard_stop_with_zero_cleanup_finalizes_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            run["failedStage"] = "verify"
            run["cleanupOnly"] = True
            run["failureDisposition"] = "hard-stop"
            write_runner_control(run_dir, run)
            (run_dir / "archive-validation.json").unlink()

            result = final_evaluate(run_dir)

            self.assertEqual("failed", result["verdict"])
            self.assertEqual("known-hard-stop-with-partial-evidence", result["verdictBasis"])

    def test_complete_evidence_with_nonzero_cleanup_finalizes_as_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            cleanup_path = run_dir / "cleanup-inventory.json"
            cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
            cleanup["counts"]["ec2Instances"] = 1
            cleanup["resources"]["ec2Instances"] = ["i-residual"]
            cleanup["serviceInventoryZero"] = False
            cleanup["allZero"] = False
            cleanup_path.write_text(json.dumps(cleanup) + "\n", encoding="utf-8")
            run_path = run_dir / "run.json"
            run = json.loads(run_path.read_text(encoding="utf-8"))
            attempts = run["stageAttempts"][:8]
            ordinal = 9
            for retry, minute in enumerate((120, 124, 128), start=1):
                cleanup_attempt = stage_attempt(ordinal, "cleanup", minute, minute + 3)
                cleanup_attempt["attempt"] = retry
                attempts.append(cleanup_attempt)
                ordinal += 1
                inventory_attempt = stage_attempt(
                    ordinal, "inventory", minute + 3, minute + 4, exit_code=2
                )
                inventory_attempt["attempt"] = retry
                inventory_attempt["failureDisposition"] = "hard-stop"
                attempts.append(inventory_attempt)
                ordinal += 1
            evaluation_attempt = stage_attempt(
                ordinal, "evaluate", 135, None, exit_code=None
            )
            attempts.append(evaluation_attempt)
            run["stageAttempts"] = attempts
            run["attemptedStages"] = [attempt["stage"] for attempt in attempts]
            run["completedStages"] = [
                "deploy", "verify", "correctness", "seed", "warmup",
                "score_archive", "drain_validate", "collect", "cleanup",
            ]
            run["inProgressStage"] = {
                key: evaluation_attempt[key]
                for key in (
                    "ordinal", "attempt", "stage", "commandSha256",
                    "startedAt", "timeoutSeconds",
                )
            }
            run["cleanupOnly"] = True
            run["failureDisposition"] = "hard-stop"
            write_runner_control(run_dir, run)

            result = final_evaluate(run_dir)

            self.assertEqual("blocked", result["verdict"])
            self.assertEqual("cleanup-not-authoritatively-zero", result["verdictBasis"])
            self.assertTrue((run_dir / "final-evidence.json").is_file())
            finalized = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("blocked", finalized["verdict"])

    def test_complete_evidence_with_other_acceptance_failure_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            score_path = run_dir / "evidence/score/stage-summary.json"
            score = json.loads(score_path.read_text(encoding="utf-8"))
            score["aggregate"]["actualRps"] = 40_000
            score_path.write_text(json.dumps(score) + "\n", encoding="utf-8")

            result = final_evaluate(run_dir)

            self.assertEqual("failed", result["verdict"])
            self.assertEqual("strict-acceptance", result["verdictBasis"])
            self.assertIn("actualRpsAtLeast49500", result["failedChecks"])


if __name__ == "__main__":
    unittest.main()

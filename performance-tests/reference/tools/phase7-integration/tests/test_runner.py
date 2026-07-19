from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


AWS_DIR = Path(__file__).resolve().parents[1] / "aws"
sys.path.insert(0, str(AWS_DIR))

from common import read_json, write_json  # noqa: E402
import runner  # noqa: E402


START = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
COST_MODEL = {
    "passed": True,
    "operationalMaximumUsd": "30",
    "maximumIncludingCleanupUsd": "39",
}


class Clock:
    def __init__(self, *values: datetime):
        self.values = list(values)

    def __call__(self) -> datetime:
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy_guard = mock.patch.object(
            runner,
            "reject_strict_paid_work_under_composite_policy",
        )
        self.policy_guard_mock = self.policy_guard.start()

    def tearDown(self) -> None:
        self.policy_guard.stop()

    def test_current_policy_blocks_new_work_independent_of_preflight_but_not_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self.run_directory(Path(temporary))
            write_json(
                run_dir / "inputs" / "preflight.json",
                {"attemptType": "missing-or-attacker-controlled"},
            )
            process = mock.Mock()
            self.policy_guard_mock.side_effect = RuntimeError(
                "strict paid work is disabled"
            )
            with self.assertRaisesRegex(RuntimeError, "strict paid work is disabled"):
                runner.execute_stage(
                    run_dir,
                    "deploy",
                    self.command(timeout=10),
                    now_provider=Clock(START),
                    process_runner=process,
                )
            process.assert_not_called()
            self.policy_guard_mock.side_effect = None
            cleanup = runner.execute_stage(
                run_dir,
                "cleanup",
                self.command(timeout=10),
                now_provider=Clock(START, START + timedelta(seconds=1)),
                process_runner=lambda *_args: runner.ProcessOutcome(0, "clean", ""),
            )
            self.assertTrue(cleanup["passed"])

    def test_inherited_aws_credentials_are_rejected_before_attempt_state_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self.run_directory(Path(temporary))
            before = (run_dir / "run.json").read_bytes()
            process = mock.Mock()
            with mock.patch.dict(runner.os.environ, {"AWS_ACCESS_KEY_ID": "forbidden"}):
                with self.assertRaisesRegex(RuntimeError, "fresh aws login"):
                    runner.execute_stage(
                        run_dir,
                        "deploy",
                        self.command(timeout=10),
                        now_provider=Clock(START),
                        process_runner=process,
                    )
            process.assert_not_called()
            self.assertEqual(before, (run_dir / "run.json").read_bytes())

    def test_verify_is_mandatory_immediately_after_deploy(self) -> None:
        document = self.document(completed=["deploy"])
        self.assertTrue(runner.stage_gate(document, "verify", START, COST_MODEL)["allowed"])
        correctness = runner.stage_gate(document, "correctness", START, COST_MODEL)
        self.assertFalse(correctness["allowed"])
        self.assertFalse(correctness["checks"]["stageSequenceValid"])
        document["completedStages"].append("verify")
        self.assertTrue(runner.stage_gate(document, "correctness", START, COST_MODEL)["allowed"])

    def test_stage_ceiling_must_fit_entirely_before_cleanup_transition(self) -> None:
        document = self.document(
            completed=["deploy", "verify", "correctness", "seed", "warmup"],
            paid_started=START,
        )
        timeout = runner.STAGE_TIMEOUT_SECONDS["score_archive"]
        reserve = runner.admission_reserve_seconds("score_archive", timeout)
        latest_start = START + timedelta(
            seconds=runner.CLEANUP_START_MINUTES * 60 - reserve
        )
        at_boundary = runner.stage_gate(
            document,
            "score_archive",
            latest_start,
            COST_MODEL,
            timeout,
        )
        self.assertTrue(at_boundary["allowed"], at_boundary)
        after_boundary = runner.stage_gate(
            document,
            "score_archive",
            latest_start + timedelta(seconds=1),
            COST_MODEL,
            timeout,
        )
        self.assertFalse(after_boundary["allowed"])
        self.assertFalse(after_boundary["checks"]["stageWorstCaseFitsCleanupWindow"])
        self.assertTrue(after_boundary["cleanupRequired"])

    def test_initial_deploy_is_admitted_with_separate_watchdog_and_reserve_budgets(self) -> None:
        timeout = runner.STAGE_TIMEOUT_SECONDS["deploy"]
        gate = runner.stage_gate(self.document(), "deploy", START, COST_MODEL, timeout)
        self.assertTrue(gate["allowed"], gate)
        self.assertEqual(150 * 60, gate["remainingPreCleanupReserveSeconds"])
        self.assertLessEqual(
            gate["remainingPreCleanupReserveSeconds"],
            runner.CLEANUP_START_MINUTES * 60,
        )
        self.assertGreaterEqual(
            runner.STAGE_TIMEOUT_SECONDS["score_archive"], 38 * 60
        )

    def test_campaign_cost_gate_uses_55_60_contract_and_preserves_cleanup_reserve(self) -> None:
        timeout = runner.STAGE_TIMEOUT_SECONDS["deploy"]
        admitted = {
            "passed": True,
            "operationalMaximumUsd": "52.855718",
            "maximumIncludingCleanupUsd": "57.855718",
        }
        self.assertTrue(
            runner.stage_gate(self.document(), "deploy", START, admitted, timeout)["allowed"]
        )

        for blocked in (
            {**admitted, "operationalMaximumUsd": "55.000000", "maximumIncludingCleanupUsd": "60.000000"},
            {**admitted, "maximumIncludingCleanupUsd": "60.000001"},
            {**admitted, "maximumIncludingCleanupUsd": "57.855717"},
        ):
            gate = runner.stage_gate(self.document(), "deploy", START, blocked, timeout)
            self.assertFalse(gate["allowed"], gate)
            self.assertFalse(gate["checks"]["costGatePassedForNewWork"])
            self.assertTrue(gate["cleanupRequired"])

    def test_drain_timeout_uses_actual_score_end_absolute_window(self) -> None:
        document = self.drain_document(
            score_attempt_start=START + timedelta(minutes=90),
            score_end=START + timedelta(minutes=100),
        )
        now = START + timedelta(minutes=120)
        timeout = runner.effective_stage_timeout(
            document,
            "drain_validate",
            now,
            runner.STAGE_TIMEOUT_SECONDS["drain_validate"],
        )
        self.assertEqual(25 * 60, timeout)
        gate = runner.stage_gate(
            document, "drain_validate", now, COST_MODEL, timeout
        )
        self.assertTrue(gate["allowed"], gate)
        self.assertEqual(
            runner.timestamp(START + timedelta(minutes=145)),
            gate["dynamicDrainDeadline"],
        )
        at_deadline = runner.stage_gate(
            document,
            "drain_validate",
            START + timedelta(minutes=145),
            COST_MODEL,
            1,
        )
        self.assertFalse(at_deadline["allowed"])
        self.assertFalse(at_deadline["checks"]["dynamicDrainDeadlineOpen"])

    def test_paid_cleanup_boundary_shortens_drain_and_preserves_collect_reserve(self) -> None:
        document = self.drain_document(
            score_attempt_start=START + timedelta(minutes=120),
            score_end=START + timedelta(minutes=130),
        )
        now = START + timedelta(minutes=149)
        timeout = runner.effective_stage_timeout(
            document,
            "drain_validate",
            now,
            runner.STAGE_TIMEOUT_SECONDS["drain_validate"],
        )
        self.assertEqual(60, timeout)
        gate = runner.stage_gate(
            document, "drain_validate", now, COST_MODEL, timeout
        )
        self.assertTrue(gate["allowed"], gate)
        self.assertEqual(
            runner.timestamp(START + timedelta(minutes=160)),
            gate["dynamicDrainDeadline"],
        )
        self.assertEqual(
            runner.timestamp(START + timedelta(minutes=150)),
            gate["collectionSafeDrainDeadline"],
        )
        late = START + timedelta(minutes=150)
        late_timeout = runner.effective_stage_timeout(
            document,
            "drain_validate",
            late,
            runner.STAGE_TIMEOUT_SECONDS["drain_validate"],
        )
        late_gate = runner.stage_gate(
            document, "drain_validate", late, COST_MODEL, late_timeout
        )
        self.assertFalse(late_gate["allowed"])
        self.assertFalse(late_gate["checks"]["stageWorstCaseFitsCleanupWindow"])
        self.assertTrue(late_gate["cleanupRequired"])

    def test_unconfirmed_deploy_is_persisted_before_spawn_and_can_only_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self.run_directory(Path(temporary))
            command = self.command(timeout=10)

            def crash_after_observing_state(*_args: object) -> runner.ProcessOutcome:
                current = read_json(run_dir / "run.json")
                self.assertEqual("deploy", current["inProgressStage"]["stage"])
                self.assertEqual(["deploy"], current["attemptedStages"])
                expected = hashlib.sha256(
                    json.dumps(command, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                self.assertEqual(expected, current["inProgressStage"]["commandSha256"])
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                runner.execute_stage(
                    run_dir,
                    "deploy",
                    command,
                    now_provider=Clock(START),
                    process_runner=crash_after_observing_state,
                )

            crashed = read_json(run_dir / "run.json")
            self.assertEqual("deploy", crashed["inProgressStage"]["stage"])
            self.assertFalse(runner.stage_gate(crashed, "deploy", START, COST_MODEL, 10)["allowed"])

            cleanup = runner.execute_stage(
                run_dir,
                "cleanup",
                self.command(timeout=10),
                now_provider=Clock(START + timedelta(seconds=1), START + timedelta(seconds=2)),
                process_runner=lambda *_args: runner.ProcessOutcome(0, "clean", ""),
            )
            self.assertTrue(cleanup["passed"])
            recovered = read_json(run_dir / "run.json")
            self.assertEqual("deploy", recovered["failedStage"])
            self.assertTrue(recovered["cleanupOnly"])
            self.assertEqual("interrupted-unconfirmed", recovered["stageAttempts"][0]["status"])
            self.assertEqual(1, sum(stage == "deploy" for stage in recovered["attemptedStages"]))

    def test_timeout_is_a_hard_stop_and_preserves_watchdog_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self.run_directory(Path(temporary))
            document = self.document(
                completed=["deploy", "verify", "correctness", "seed"],
                paid_started=START,
            )
            write_json(run_dir / "run.json", document)
            observed_timeout: list[int] = []

            def timed_out(_argv: list[str], _cwd: str, _env: dict[str, str], timeout: int) -> runner.ProcessOutcome:
                observed_timeout.append(timeout)
                return runner.ProcessOutcome(124, "partial", "watchdog", timed_out=True)

            evidence = runner.execute_stage(
                run_dir,
                "warmup",
                self.command(timeout=7),
                now_provider=Clock(START + timedelta(minutes=30), START + timedelta(minutes=30, seconds=7)),
                process_runner=timed_out,
            )
            self.assertEqual([7], observed_timeout)
            self.assertTrue(evidence["timedOut"])
            self.assertEqual("hard-stop", evidence["failureDisposition"])
            state = read_json(run_dir / "run.json")
            self.assertEqual("warmup", state["failedStage"])
            self.assertEqual("cleanup-required", state["status"])
            self.assertTrue(state["cleanupOnly"])

    def test_explicit_acceptance_failure_is_distinct_from_a_hard_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self.run_directory(Path(temporary))
            document = self.document(
                completed=["deploy", "verify", "correctness", "seed"],
                paid_started=START,
            )
            write_json(run_dir / "run.json", document)
            command = self.command(timeout=7)
            command["nonzeroDisposition"] = "acceptance-failure"
            evidence = runner.execute_stage(
                run_dir,
                "warmup",
                command,
                now_provider=Clock(START + timedelta(minutes=30), START + timedelta(minutes=30, seconds=7)),
                process_runner=lambda *_args: runner.ProcessOutcome(2, "below-threshold", ""),
            )
            self.assertEqual("acceptance-failure", evidence["failureDisposition"])
            state = read_json(run_dir / "run.json")
            self.assertEqual("acceptance-failure", state["failureDisposition"])
            self.assertEqual("cleanup-required", state["status"])

    def test_cleanup_retries_are_bounded_and_inventory_runs_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self.run_directory(Path(temporary))
            document = self.document(paid_started=START)
            document.update({"failedStage": "deploy", "cleanupOnly": True, "status": "cleanup-required"})
            write_json(run_dir / "run.json", document)
            clock = START + timedelta(minutes=181)

            first_cleanup = runner.execute_stage(
                run_dir,
                "cleanup",
                self.command(timeout=3),
                now_provider=Clock(clock, clock + timedelta(seconds=3)),
                process_runner=lambda *_args: runner.ProcessOutcome(2, "residual", ""),
            )
            self.assertFalse(first_cleanup["passed"])
            after_failed_cleanup = read_json(run_dir / "run.json")
            self.assertTrue(runner.stage_gate(after_failed_cleanup, "inventory", clock, COST_MODEL, 3)["allowed"])

            first_inventory = runner.execute_stage(
                run_dir,
                "inventory",
                self.command(timeout=3),
                now_provider=Clock(clock + timedelta(seconds=4), clock + timedelta(seconds=5)),
                process_runner=lambda *_args: runner.ProcessOutcome(2, "not-zero", ""),
            )
            self.assertFalse(first_inventory["passed"])
            after_inventory = read_json(run_dir / "run.json")
            self.assertFalse(runner.stage_gate(after_inventory, "evaluate", clock, COST_MODEL, 3)["allowed"])
            self.assertTrue(runner.stage_gate(after_inventory, "cleanup", clock, COST_MODEL, 3)["allowed"])

            for attempt in (2, 3):
                runner.execute_stage(
                    run_dir,
                    "cleanup",
                    self.command(timeout=3),
                    now_provider=Clock(clock + timedelta(seconds=attempt * 10), clock + timedelta(seconds=attempt * 10 + 1)),
                    process_runner=lambda *_args: runner.ProcessOutcome(2, "residual", ""),
                )
                runner.execute_stage(
                    run_dir,
                    "inventory",
                    self.command(timeout=3),
                    now_provider=Clock(clock + timedelta(seconds=attempt * 10 + 2), clock + timedelta(seconds=attempt * 10 + 3)),
                    process_runner=lambda *_args: runner.ProcessOutcome(2, "not-zero", ""),
                )

            exhausted = read_json(run_dir / "run.json")
            self.assertFalse(runner.stage_gate(exhausted, "cleanup", clock, COST_MODEL, 3)["allowed"])
            self.assertTrue(runner.stage_gate(exhausted, "evaluate", clock, COST_MODEL, 3)["allowed"])

    def test_evaluate_requires_inventory_after_latest_cleanup(self) -> None:
        document = self.document(paid_started=START)
        document["stageAttempts"] = [{
            "ordinal": 1,
            "stage": "cleanup",
            "status": "finished",
            "exitCode": 0,
        }]
        self.assertFalse(runner.stage_gate(document, "evaluate", START, COST_MODEL)["allowed"])
        document["stageAttempts"].append({
            "ordinal": 2,
            "stage": "inventory",
            "status": "finished",
            "exitCode": 0,
        })
        self.assertTrue(runner.stage_gate(document, "evaluate", START, COST_MODEL)["allowed"])

    def test_watchdog_terminates_the_process_group(self) -> None:
        process = mock.Mock()
        process.pid = 1234
        process.communicate.side_effect = [
            __import__("subprocess").TimeoutExpired(["fake"], 1),
            ("partial", "late"),
        ]
        with mock.patch.object(runner.subprocess, "Popen", return_value=process), \
             mock.patch.object(runner.os, "killpg") as killpg:
            outcome = runner.run_command(["fake"], "/tmp", {}, 1)
        killpg.assert_called_once_with(1234, runner.signal.SIGTERM)
        self.assertEqual(124, outcome.returncode)
        self.assertTrue(outcome.timed_out)

    @staticmethod
    def command(timeout: int) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "argv": ["fake-command"],
            "cwd": "/tmp",
            "environment": {},
            "timeoutSeconds": timeout,
        }

    @staticmethod
    def document(
        *,
        completed: list[str] | None = None,
        paid_started: datetime | None = None,
    ) -> dict[str, object]:
        return {
            "schemaVersion": 2,
            "status": "running",
            "completedStages": list(completed or []),
            "attemptedStages": [],
            "stageAttempts": [],
            "inProgressStage": None,
            "cleanupOnly": False,
            "failedStage": None,
            "failureDisposition": None,
            "paidStartedAt": paid_started.isoformat() if paid_started else None,
        }

    def run_directory(self, parent: Path) -> Path:
        run_dir = parent / "run"
        (run_dir / "inputs").mkdir(parents=True)
        write_json(run_dir / "run.json", self.document())
        write_json(run_dir / "inputs" / "cost-model.json", COST_MODEL)
        return run_dir

    def drain_document(
        self, *, score_attempt_start: datetime, score_end: datetime
    ) -> dict[str, object]:
        document = self.document(
            completed=list(runner.PRE_CLEANUP_STAGES[:6]),
            paid_started=START,
        )
        document["cleanupStartDeadline"] = (
            START + timedelta(minutes=runner.CLEANUP_START_MINUTES)
        ).isoformat()
        document["scoreEndedAt"] = score_end.isoformat()
        document["scoreDrainDeadline"] = (
            score_end + timedelta(seconds=runner.POST_SCORE_DRAIN_SECONDS)
        ).isoformat()
        document["stageAttempts"] = [{
            "ordinal": 1,
            "stage": "score_archive",
            "startedAt": score_attempt_start.isoformat(),
            "status": "finished",
            "exitCode": 0,
        }]
        return document


if __name__ == "__main__":
    unittest.main()

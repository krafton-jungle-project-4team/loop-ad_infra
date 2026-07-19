from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


AWS_DIR = Path(__file__).resolve().parents[1] / "aws"
sys.path.insert(0, str(AWS_DIR))

from build_run_commands import seal_commands  # noqa: E402
from common import read_json, write_json  # noqa: E402
from run_chain import run_chain  # noqa: E402
from runner import ProcessOutcome, STAGES  # noqa: E402
from evidence_assembler import CLEANUP_SERVICE_CLASSES  # noqa: E402


RUN_ID = "run_20260718_210000_phase7_integration"
SESSION_ID = "phase7-integration-20260718T210000Z"


class RunChainTest(unittest.TestCase):
    def run_directory(self, root: Path) -> Path:
        run_dir = root / RUN_ID
        (run_dir / "inputs").mkdir(parents=True)
        write_json(run_dir / "run.json", {
            "schemaVersion": 2,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "phase": "7-2",
            "phase5": "skipped",
            "status": "initialized",
            "verdict": None,
            "paidStartedAt": None,
            "completedStages": [],
            "attemptedStages": [],
            "stageAttempts": [],
            "inProgressStage": None,
            "cleanupOnly": False,
            "failedStage": None,
            "failureDisposition": None,
            "commandSetRequired": True,
            "commandSetSha256": None,
        })
        write_json(run_dir / "inputs/cost-model.json", {
            "passed": True,
            "operationalMaximumUsd": "30",
            "maximumIncludingCleanupUsd": "39",
        })
        commands = {
            stage: {
                "schemaVersion": 1,
                "argv": ["/bin/true", stage],
                "cwd": "/tmp",
                "environment": {},
                "timeoutSeconds": 3,
                "nonzeroDisposition": "acceptance-failure" if stage == "evaluate" else "hard-stop",
            }
            for stage in STAGES
        }
        seal_commands(run_dir, commands)
        return run_dir

    def test_one_driver_runs_each_stage_once_and_finalizes_only_after_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(__import__("os").environ, {}, clear=True):
            run_dir = self.run_directory(Path(directory))
            observed = []

            def process(argv, _cwd, _environment, _timeout):
                stage = argv[-1]
                observed.append(stage)
                self.write_stage_artifacts(run_dir, stage)
                return ProcessOutcome(0, "ok", "")

            result = run_chain(run_dir, process_runner=process)
        self.assertEqual(STAGES, observed)
        self.assertEqual("passed", result["verdict"])
        self.assertEqual(STAGES, result["completedStages"])
        self.assertEqual(1, max(item["attempt"] for item in result["stageAttempts"]))

    def test_hard_stop_skips_new_work_and_transitions_directly_to_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(__import__("os").environ, {}, clear=True):
            run_dir = self.run_directory(Path(directory))
            observed = []

            def process(argv, _cwd, _environment, _timeout):
                stage = argv[-1]
                observed.append(stage)
                self.write_stage_artifacts(run_dir, stage)
                return ProcessOutcome(2 if stage == "verify" else 0, "", "failure")

            result = run_chain(run_dir, process_runner=process)
            persisted = read_json(run_dir / "run.json")
        self.assertEqual(["deploy", "verify", "cleanup", "inventory", "evaluate"], observed)
        self.assertEqual("verify", persisted["failedStage"])
        self.assertEqual("failed", result["verdict"])
        self.assertNotIn("warmup", persisted["attemptedStages"])
        self.assertNotIn("score_archive", persisted["attemptedStages"])

    def test_third_cleanup_crash_is_finalized_by_inventory_without_fourth_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(__import__("os").environ, {}, clear=True):
            run_dir = self.run_directory(Path(directory))
            state = read_json(run_dir / "run.json")
            state.update({
                "status": "running",
                "cleanupOnly": True,
                "failedStage": "score_archive",
                "failureDisposition": "hard-stop",
                "attemptedStages": ["cleanup", "cleanup", "cleanup"],
                "stageAttempts": [
                    {
                        "ordinal": 1,
                        "attempt": 1,
                        "stage": "cleanup",
                        "status": "finished",
                        "exitCode": 2,
                        "failureDisposition": "hard-stop",
                    },
                    {
                        "ordinal": 2,
                        "attempt": 2,
                        "stage": "cleanup",
                        "status": "finished",
                        "exitCode": 2,
                        "failureDisposition": "hard-stop",
                    },
                    {
                        "ordinal": 3,
                        "attempt": 3,
                        "stage": "cleanup",
                        "status": "in-progress",
                        "exitCode": None,
                        "failureDisposition": None,
                    },
                ],
                "inProgressStage": {
                    "ordinal": 3,
                    "attempt": 3,
                    "stage": "cleanup",
                    "commandSha256": "persisted-before-crash",
                    "startedAt": "2026-07-18T21:00:00Z",
                    "timeoutSeconds": 3,
                },
            })
            write_json(run_dir / "run.json", state)
            observed = []

            def process(argv, _cwd, _environment, _timeout):
                stage = argv[-1]
                observed.append(stage)
                self.write_stage_artifacts(run_dir, stage)
                return ProcessOutcome(0, "ok", "")

            result = run_chain(run_dir, process_runner=process)

        self.assertEqual(["inventory", "evaluate"], observed)
        cleanup_attempts = [
            item for item in result["stageAttempts"]
            if item["stage"] == "cleanup"
        ]
        self.assertEqual(3, len(cleanup_attempts))
        self.assertEqual("interrupted-unconfirmed", cleanup_attempts[-1]["status"])
        inventory_attempts = [
            item for item in result["stageAttempts"]
            if item["stage"] == "inventory"
        ]
        self.assertEqual(1, len(inventory_attempts))
        self.assertEqual("finished", inventory_attempts[0]["status"])
        self.assertTrue(inventory_attempts[0]["authoritativeZero"])
        self.assertEqual("failed", result["verdict"])

    def test_third_inventory_crash_is_reverified_once_without_fourth_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(__import__("os").environ, {}, clear=True):
            run_dir = self.run_directory(Path(directory))
            state = read_json(run_dir / "run.json")
            state.update({
                "status": "cleanup-unverified",
                "cleanupOnly": True,
                "failedStage": "score_archive",
                "failureDisposition": "hard-stop",
                "attemptedStages": ["cleanup", "cleanup", "cleanup", "inventory"],
                "stageAttempts": [
                    {
                        "ordinal": ordinal,
                        "attempt": ordinal,
                        "stage": "cleanup",
                        "status": "finished",
                        "exitCode": 2,
                        "failureDisposition": "hard-stop",
                    }
                    for ordinal in (1, 2, 3)
                ] + [{
                    "ordinal": 4,
                    "attempt": 1,
                    "stage": "inventory",
                    "status": "in-progress",
                    "exitCode": None,
                    "failureDisposition": None,
                }],
                "inProgressStage": {
                    "ordinal": 4,
                    "attempt": 1,
                    "stage": "inventory",
                    "commandSha256": "persisted-before-crash",
                    "startedAt": "2026-07-18T21:00:00Z",
                    "timeoutSeconds": 3,
                },
            })
            write_json(run_dir / "run.json", state)
            observed = []

            def process(argv, _cwd, _environment, _timeout):
                stage = argv[-1]
                observed.append(stage)
                self.write_stage_artifacts(run_dir, stage)
                return ProcessOutcome(0, "ok", "")

            result = run_chain(run_dir, process_runner=process)

        self.assertEqual(["inventory", "evaluate"], observed)
        self.assertEqual(
            3,
            sum(item["stage"] == "cleanup" for item in result["stageAttempts"]),
        )
        inventories = [
            item for item in result["stageAttempts"]
            if item["stage"] == "inventory"
        ]
        self.assertEqual(2, len(inventories))
        self.assertEqual("interrupted-unconfirmed", inventories[0]["status"])
        self.assertTrue(inventories[1]["authoritativeZero"])

    def test_exit_zero_inventory_without_authoritative_shape_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(__import__("os").environ, {}, clear=True):
            run_dir = self.run_directory(Path(directory))
            observed = []
            inventory_count = 0

            def process(argv, _cwd, _environment, _timeout):
                nonlocal inventory_count
                stage = argv[-1]
                observed.append(stage)
                if stage == "score_archive":
                    self.write_stage_artifacts(run_dir, stage)
                elif stage == "inventory":
                    inventory_count += 1
                    if inventory_count == 1:
                        write_json(run_dir / "cleanup-inventory.json", {
                            "schemaVersion": 1,
                            "runId": RUN_ID,
                            "sessionId": SESSION_ID,
                            "allZero": True,
                        })
                    else:
                        self.write_stage_artifacts(run_dir, stage)
                return ProcessOutcome(0, "ok", "")

            result = run_chain(run_dir, process_runner=process)

        self.assertEqual(2, observed.count("cleanup"))
        self.assertEqual(2, observed.count("inventory"))
        inventories = [
            item for item in result["stageAttempts"]
            if item["stage"] == "inventory"
        ]
        self.assertFalse(inventories[0]["authoritativeShape"])
        self.assertTrue(inventories[1]["authoritativeZero"])

    def write_stage_artifacts(self, run_dir: Path, stage: str) -> None:
        if stage == "score_archive":
            ended_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            write_json(run_dir / "evidence/score/stage-summary.json", {
                "aggregate": {"nodes": [{"endedAt": ended_at}]},
            })
        if stage == "inventory":
            resources = {
                service_class: [] for service_class in CLEANUP_SERVICE_CLASSES
            }
            write_json(run_dir / "cleanup-inventory.json", {
                "schemaVersion": 1,
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "counts": {service_class: 0 for service_class in resources},
                "resources": resources,
                "taggingApiResiduals": [],
                "taggingApiAuthoritative": False,
                "serviceInventoryZero": True,
                "taggingApiResidualsZero": True,
                "allZero": True,
            })


if __name__ == "__main__":
    unittest.main()

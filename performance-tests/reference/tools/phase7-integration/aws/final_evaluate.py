#!/usr/bin/env python3
"""Assemble, evaluate, and atomically finalize one cleaned Phase 7-2 run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from common import read_json, utc_now, write_json
from evidence_assembler import (
    ARTIFACT_CONTRACTS,
    EvidenceAssemblyError,
    assemble_evidence,
    finalize_run_document,
    validate_cleanup_inventory_document,
)
from evaluator import evaluate


def final_evaluate(run_dir: Path) -> dict[str, object]:
    run_dir = run_dir.resolve()
    run_before = capture_evaluate_start_run(run_dir)
    try:
        evidence = assemble_evidence(run_dir)
    except (EvidenceAssemblyError, OSError, UnicodeError, json.JSONDecodeError) as error:
        status = partial_evidence_status(run_dir, run_before, error)
        write_json(run_dir / "final-evidence-status.json", status)
        evaluation = partial_evaluation(run_dir, run_before, status)
    else:
        write_json(run_dir / "final-evidence.json", evidence)
        evaluation = evaluate(evidence, run_dir)
        evaluation["evidenceAssemblySha256"] = evidence["assemblySha256"]
    write_json(run_dir / "final-evaluation.json", evaluation)
    finalized = finalize_run_document(run_before, evaluation)
    write_json(run_dir / "run.json", finalized)
    return evaluation


def capture_evaluate_start_run(run_dir: Path) -> dict[str, Any]:
    """Persist the exact evaluate-start runner state without later overwrites.

    ``run.json`` remains the runner's mutable state machine and is changed both
    by this process and by the parent runner after the evaluate subprocess
    exits.  Final evidence therefore binds this immutable byte-for-byte copy
    instead of the circular mutable path.
    """

    run_path = run_dir / "run.json"
    raw = run_path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceAssemblyError("run.json must be valid JSON at evaluate start") from error
    if not isinstance(value, dict):
        raise EvidenceAssemblyError("run.json must contain an object at evaluate start")

    snapshot = run_dir / "evidence" / "control" / "evaluate-start-run.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.exists():
        if snapshot.is_symlink() or not snapshot.is_file() or snapshot.read_bytes() != raw:
            raise EvidenceAssemblyError(
                "immutable evaluate-start runner snapshot already differs from run.json"
            )
    else:
        with snapshot.open("xb") as handle:
            handle.write(raw)
        snapshot.chmod(0o600)
    return value


def partial_evidence_status(
    run_dir: Path, run: dict[str, Any], error: Exception
) -> dict[str, Any]:
    artifacts: dict[str, dict[str, Any]] = {}
    for name, contract in ARTIFACT_CONTRACTS.items():
        path = run_dir / contract.path
        item: dict[str, Any] = {"path": contract.path, "present": False, "sha256": None}
        if path.is_file() and not path.is_symlink():
            try:
                path.resolve(strict=True).relative_to(run_dir)
            except (FileNotFoundError, ValueError):
                pass
            else:
                item["present"] = True
                item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        artifacts[name] = item
    return {
        "schemaVersion": 1,
        "runId": run.get("runId"),
        "sessionId": run.get("sessionId"),
        "complete": False,
        "assemblyError": f"{type(error).__name__}: {error}"[:1_000],
        "artifacts": artifacts,
    }


def partial_evaluation(
    run_dir: Path, run: dict[str, Any], status: dict[str, Any]
) -> dict[str, Any]:
    run_id = str(run.get("runId", ""))
    session_id = str(run.get("sessionId", ""))
    cleanup_valid = False
    cleanup_zero = False
    cleanup_error: str | None = None
    try:
        cleanup = read_json(run_dir / "cleanup-inventory.json")
        cleanup_zero = validate_cleanup_inventory_document(cleanup, run_id, session_id)
        cleanup_valid = True
    except (EvidenceAssemblyError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        cleanup_error = f"{type(error).__name__}: {error}"[:1_000]

    known_hard_stop = isinstance(run.get("failedStage"), str) and bool(run.get("failedStage"))
    if not cleanup_valid or not cleanup_zero:
        verdict = "blocked"
        basis = "cleanup-not-authoritatively-zero"
    elif known_hard_stop:
        verdict = "failed"
        basis = "known-hard-stop-with-partial-evidence"
    else:
        verdict = "inconclusive"
        basis = "required-evidence-incomplete"
    checks = {
        "fullEvidenceAssembled": False,
        "knownHardStopRecorded": known_hard_stop,
        "cleanupInventoryValid": cleanup_valid,
        "cleanupInventoryZero": cleanup_zero,
    }
    return {
        "schemaVersion": 1,
        "workload": "phase7-end-to-end-integration",
        "runId": run_id,
        "sessionId": session_id,
        "phase5": run.get("phase5"),
        "evaluatedAt": utc_now(),
        "checks": checks,
        "verdict": verdict,
        "verdictBasis": basis,
        "failedChecks": sorted(name for name, passed in checks.items() if not passed),
        "partialEvidence": status,
        "cleanupError": cleanup_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    result = final_evaluate(args.run_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    # A non-passing experiment verdict is still a successfully finalized run.
    # run_chain reports a nonzero process exit after reading the terminal verdict.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

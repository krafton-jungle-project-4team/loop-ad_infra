#!/usr/bin/env python3
"""Drive one sealed Phase 7-2 attempt through cleanup and final evaluation."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Callable

from common import read_json, write_json
from evidence_assembler import validate_cleanup_inventory_document
from runner import (
    MAX_CLEANUP_ATTEMPTS,
    PRE_CLEANUP_STAGES,
    STAGES,
    ProcessOutcome,
    command_timeout,
    execute_stage,
    stage_attempts,
    stage_gate,
)


ProcessRunner = Callable[[list[str], str, dict[str, str], int], ProcessOutcome]


def run_chain(run_dir: Path, *, process_runner: ProcessRunner | None = None) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    commands = {
        stage: read_json(run_dir / "inputs" / f"{stage}-command.json")
        for stage in STAGES
    }
    cost_model = read_json(run_dir / "inputs" / "cost-model.json")

    for stage in PRE_CLEANUP_STAGES:
        state = read_json(run_dir / "run.json")
        if stage in state.get("completedStages", []):
            continue
        if state.get("cleanupOnly") or state.get("failedStage") or state.get("inProgressStage"):
            break
        try:
            outcome = execute_stage(
                run_dir, stage, commands[stage], process_runner=process_runner
            )
        except Exception as error:
            mark_cleanup_only(run_dir, stage, error)
            break
        if not outcome["passed"]:
            break

    try:
        while True:
            state = read_json(run_dir / "run.json")
            cleanup_attempts = [
                attempt for attempt in stage_attempts(state)
                if attempt.get("stage") == "cleanup"
            ]
            if len(cleanup_attempts) >= MAX_CLEANUP_ATTEMPTS:
                break
            cleanup_gate = stage_gate(
                state,
                "cleanup",
                datetime.now(UTC),
                cost_model,
                command_timeout("cleanup", commands["cleanup"]),
            )
            if not cleanup_gate["allowed"]:
                break
            execute_stage(
                run_dir, "cleanup", commands["cleanup"], process_runner=process_runner
            )
            ensure_inventory_after_latest_cleanup(
                run_dir, commands["inventory"], process_runner=process_runner
            )
            if authoritative_inventory_zero_after_latest_cleanup(run_dir):
                break
    except Exception as error:
        mark_cleanup_only(run_dir, "cleanup", error)
    finally:
        try:
            ensure_inventory_after_latest_cleanup(
                run_dir, commands["inventory"], process_runner=process_runner
            )
        except Exception as error:
            mark_cleanup_only(run_dir, "inventory", error)

    state = read_json(run_dir / "run.json")
    evaluate_gate = stage_gate(
        state,
        "evaluate",
        datetime.now(UTC),
        cost_model,
        command_timeout("evaluate", commands["evaluate"]),
    )
    if evaluate_gate["allowed"]:
        execute_stage(
            run_dir, "evaluate", commands["evaluate"], process_runner=process_runner
        )
    return read_json(run_dir / "run.json")


def ensure_inventory_after_latest_cleanup(
    run_dir: Path,
    inventory_command: dict[str, Any],
    *,
    process_runner: ProcessRunner | None = None,
) -> dict[str, Any] | None:
    """Finalize the newest cleanup attempt with one persisted inventory attempt.

    In particular, execute_stage first converts an in-progress third cleanup
    attempt from a prior controller crash into interrupted-unconfirmed state.
    Inventory then remains sequence-valid without authorizing a fourth cleanup.
    """
    state = read_json(run_dir / "run.json")
    attempts = stage_attempts(state)
    cleanup_attempts = [item for item in attempts if item.get("stage") == "cleanup"]
    if not cleanup_attempts:
        return None
    latest_cleanup = max(cleanup_attempts, key=attempt_ordinal)
    inventory_attempts = [
        item for item in attempts if item.get("stage") == "inventory"
    ]
    latest_inventory = (
        max(inventory_attempts, key=attempt_ordinal) if inventory_attempts else None
    )
    inventory_is_finished_for_cleanup = (
        latest_inventory is not None
        and attempt_ordinal(latest_inventory) > attempt_ordinal(latest_cleanup)
        and latest_inventory.get("status") == "finished"
        and isinstance(latest_inventory.get("exitCode"), int)
        and not state.get("inProgressStage")
    )
    if inventory_is_finished_for_cleanup:
        annotate_inventory_validation(run_dir, attempt_ordinal(latest_inventory))
        return None
    result = execute_stage(
        run_dir,
        "inventory",
        inventory_command,
        process_runner=process_runner,
    )
    state = read_json(run_dir / "run.json")
    latest = max(
        (item for item in stage_attempts(state) if item.get("stage") == "inventory"),
        key=attempt_ordinal,
    )
    annotate_inventory_validation(run_dir, attempt_ordinal(latest))
    return result


def authoritative_inventory_zero_after_latest_cleanup(run_dir: Path) -> bool:
    state = read_json(run_dir / "run.json")
    attempts = stage_attempts(state)
    cleanup_attempts = [item for item in attempts if item.get("stage") == "cleanup"]
    inventory_attempts = [item for item in attempts if item.get("stage") == "inventory"]
    if not cleanup_attempts or not inventory_attempts or state.get("inProgressStage"):
        return False
    latest_cleanup = max(cleanup_attempts, key=attempt_ordinal)
    latest_inventory = max(inventory_attempts, key=attempt_ordinal)
    if (
        attempt_ordinal(latest_inventory) <= attempt_ordinal(latest_cleanup)
        or latest_inventory.get("status") != "finished"
        or latest_inventory.get("exitCode") != 0
    ):
        return False
    annotate_inventory_validation(run_dir, attempt_ordinal(latest_inventory))
    refreshed = read_json(run_dir / "run.json")
    validated = next(
        item for item in stage_attempts(refreshed)
        if attempt_ordinal(item) == attempt_ordinal(latest_inventory)
    )
    return validated.get("authoritativeZero") is True


def annotate_inventory_validation(run_dir: Path, ordinal: int) -> None:
    state = read_json(run_dir / "run.json")
    attempt = next(
        (item for item in stage_attempts(state) if attempt_ordinal(item) == ordinal),
        None,
    )
    if attempt is None or attempt.get("stage") != "inventory":
        raise RuntimeError("inventory attempt to validate is missing")
    shape_valid = False
    all_zero = False
    try:
        inventory = read_json(run_dir / "cleanup-inventory.json")
        all_zero = validate_cleanup_inventory_document(
            inventory,
            str(state["runId"]),
            str(state["sessionId"]),
        )
        shape_valid = True
    except (KeyError, OSError, TypeError, ValueError):
        # The subprocess exit status alone cannot make a malformed or stale
        # inventory authoritative. Persist this distinction for the cleanup
        # retry gate and the final report.
        shape_valid = False
        all_zero = False
    attempt["authoritativeShape"] = shape_valid
    attempt["authoritativeZero"] = bool(
        shape_valid and all_zero and attempt.get("exitCode") == 0
    )
    write_json(run_dir / "run.json", state)


def attempt_ordinal(attempt: dict[str, Any]) -> int:
    value = attempt.get("ordinal", 0)
    return value if isinstance(value, int) else 0


def mark_cleanup_only(run_dir: Path, stage: str, error: Exception) -> None:
    state = read_json(run_dir / "run.json")
    if state.get("inProgressStage"):
        return
    state["failedStage"] = state.get("failedStage") or stage
    state["failureDisposition"] = "hard-stop"
    state["cleanupOnly"] = True
    state["status"] = "cleanup-required"
    state["hardStopReason"] = f"{type(error).__name__}: {error}"[:500]
    write_json(run_dir / "run.json", state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    result = run_chain(args.run_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("verdict") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Immutable whole-attempt controller with cost, deadline, and cleanup gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from common import (
    parse_utc,
    read_json,
    reject_strict_paid_work_under_composite_policy,
    validate_identifiers,
    write_json,
)


STAGES = [
    "deploy",
    "verify",
    "correctness",
    "seed",
    "warmup",
    "score_archive",
    "drain_validate",
    "collect",
    "cleanup",
    "inventory",
    "evaluate",
]
PRE_CLEANUP_STAGES = STAGES[:STAGES.index("cleanup")]
SAFE_FINALIZATION_STAGES = {"cleanup", "inventory", "evaluate"}
NEW_WORK_STAGES = {"deploy", "correctness", "seed", "warmup", "score_archive"}
ONE_SHOT_STAGES = {"deploy", "warmup", "score_archive"}
CLEANUP_START_MINUTES = 160
HARD_DEADLINE_MINUTES = 180
MAX_CLEANUP_ATTEMPTS = 3
CAMPAIGN_NEW_WORK_STOP_USD = 55
CAMPAIGN_HARD_CAP_USD = 60
MINIMUM_CLEANUP_RESERVE_USD = 5
SCORE_END_ADMISSION_SECONDS = 10 * 60
POST_SCORE_DRAIN_SECONDS = 45 * 60
COLLECT_ADMISSION_SECONDS = 10 * 60

# These are process watchdog ceilings. Admission reserves are calculated
# separately because score/archive can keep running after the score request
# window ends while the absolute post-score drain clock is already advancing.
STAGE_TIMEOUT_SECONDS = {
    "deploy": 20 * 60,
    "verify": 10 * 60,
    "correctness": 20 * 60,
    "seed": 15 * 60,
    "warmup": 20 * 60,
    "score_archive": 38 * 60,
    "drain_validate": 45 * 60,
    "collect": 10 * 60,
    "cleanup": 20 * 60,
    "inventory": 5 * 60,
    "evaluate": 5 * 60,
}

STAGE_ADMISSION_SECONDS = {
    "deploy": 20 * 60,
    "verify": 10 * 60,
    "correctness": 20 * 60,
    "seed": 15 * 60,
    "warmup": 20 * 60,
    # The score must actually end within this milestone. Archive polling after
    # score end consumes the same absolute 45-minute window as drain validation.
    "score_archive": SCORE_END_ADMISSION_SECONDS,
    "drain_validate": POST_SCORE_DRAIN_SECONDS,
    "collect": COLLECT_ADMISSION_SECONDS,
}


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def initialize(run_dir: Path, run_id: str, session_id: str, preflight_path: Path,
               image_manifest_path: Path, cost_model_path: Path,
               identity_contract_path: Path) -> dict[str, Any]:
    validate_identifiers(run_id, session_id)
    reject_strict_paid_work_under_composite_policy(
        Path(__file__).resolve().parents[3]
    )
    if run_dir.exists():
        raise FileExistsError("a Phase 7-2 attempt directory is immutable and may not be reused")
    preflight = read_json(preflight_path)
    image_manifest = read_json(image_manifest_path)
    cost_model = read_json(cost_model_path)
    identity_contract = read_json(identity_contract_path)
    if preflight.get("passed") is not True or preflight.get("imageState") != "prepared":
        raise RuntimeError("prepared-image preflight must pass before initializing an AWS attempt")
    if cost_model.get("passed") is not True:
        raise RuntimeError("cost model must pass before initializing an AWS attempt")
    if image_manifest.get("runtimeDeployed") is not False or len(image_manifest.get("images", [])) != 3:
        raise RuntimeError("three prepared images and an absent runtime are required")
    validate_initialize_bindings(
        run_id, session_id, preflight, image_manifest, identity_contract
    )
    run_dir.mkdir(parents=True)
    started = datetime.now(UTC)
    document = {
        "schemaVersion": 2,
        "runId": run_id,
        "sessionId": session_id,
        "phase": "7-2",
        "phase5": "skipped",
        "status": "initialized",
        "verdict": None,
        "initializedAt": timestamp(started),
        "paidStartedAt": None,
        "cleanupStartDeadline": None,
        "hardDeadline": None,
        "completedStages": [],
        "attemptedStages": [],
        "stageAttempts": [],
        "inProgressStage": None,
        "cleanupOnly": False,
        "failedStage": None,
        "failureDisposition": None,
        "preflightSha256": file_sha256(preflight_path),
        "imageManifestSha256": file_sha256(image_manifest_path),
        "costModelSha256": file_sha256(cost_model_path),
        "identityContractSha256": file_sha256(identity_contract_path),
        "commandSetRequired": True,
        "commandSetSha256": None,
    }
    write_json(run_dir / "run.json", document)
    write_json(run_dir / "inputs" / "preflight.json", preflight)
    write_json(run_dir / "inputs" / "image-manifest.json", image_manifest)
    write_json(run_dir / "inputs" / "cost-model.json", cost_model)
    write_json(run_dir / "inputs" / "identity-contract.json", identity_contract)
    for name, heading in (("commands.md", "Phase 7-2 commands"), ("infra.md", "Phase 7-2 infrastructure"), ("failures.md", "Phase 7-2 failures")):
        (run_dir / name).write_text(f"# {heading}\n\n", encoding="utf-8")
    return document


def validate_initialize_bindings(
    run_id: str,
    session_id: str,
    preflight: dict[str, Any],
    image_manifest: dict[str, Any],
    identity_contract: dict[str, Any],
) -> None:
    for name, document in (
        ("prepared preflight", preflight),
        ("image manifest", image_manifest),
        ("identity contract", identity_contract),
    ):
        if document.get("runId") != run_id or document.get("sessionId") != session_id:
            raise RuntimeError(f"{name} belongs to another Run ID or Session ID")
    images = image_manifest.get("images")
    expected_architectures = {
        "collector": "linux/amd64",
        "consumer": "linux/arm64",
        "archive": "linux/arm64",
    }
    if not isinstance(images, list) or {
        str(item.get("role")) for item in images if isinstance(item, dict)
    } != set(expected_architectures):
        raise RuntimeError("image manifest must contain the exact three Phase 7 roles")
    for item in images:
        role = str(item.get("role"))
        if (
            item.get("architecture") != expected_architectures[role]
            or not isinstance(item.get("digest"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", item["digest"]) is None
        ):
            raise RuntimeError(f"image manifest contract is invalid for {role}")
    implementation_sha = image_manifest.get("implementationTreeSha256")
    if (
        not isinstance(implementation_sha, str)
        or preflight.get("handoff", {}).get("implementationTreeSha256") != implementation_sha
        or identity_contract.get("source", {}).get("implementationTreeSha256") != implementation_sha
    ):
        raise RuntimeError("preflight, image, and identity implementation hashes differ")
    if (
        identity_contract.get("identityMode") != "balanced-pool-sampled-with-replacement"
        or identity_contract.get("predeclaredBeforeDeploy") is not True
        or identity_contract.get("userApproved") is not True
        or identity_contract.get("selectionWithReplacement") is not True
        or identity_contract.get("warmupScorePoolsSeparated") is not True
        or identity_contract.get("balancedShardCount") != 120
        or identity_contract.get("fixturePoolRows") != 480
        or identity_contract.get("archive", {}).get("equivalenceAndDropContractUnchanged") is not True
    ):
        raise RuntimeError("predeclared diagnostic identity contract is invalid")


def stage_gate(document: dict[str, Any], stage: str, now: datetime,
               cost_model: dict[str, Any], timeout_seconds: int = 0) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"unsupported stage: {stage}")
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must not be negative")

    completed = set(document.get("completedStages", []))
    attempts = stage_attempts(document)
    in_progress = document.get("inProgressStage")
    cleanup_attempts = [attempt for attempt in attempts if attempt.get("stage") == "cleanup"]
    latest_cleanup = latest_attempt(attempts, "cleanup")
    latest_inventory = latest_attempt(attempts, "inventory")
    cleanup_only = bool(document.get("cleanupOnly") or document.get("failedStage") or in_progress)

    if stage in PRE_CLEANUP_STAGES:
        index = PRE_CLEANUP_STAGES.index(stage)
        previous = PRE_CLEANUP_STAGES[:index]
        allowed_sequence = (
            all(previous_stage in completed for previous_stage in previous)
            and stage not in completed
            and not any(attempt.get("stage") == stage for attempt in attempts)
            and not cleanup_attempts
            and not cleanup_only
        )
    elif stage == "cleanup":
        inventory_after_cleanup = (
            latest_cleanup is not None
            and latest_inventory is not None
            and attempt_ordinal(latest_inventory) > attempt_ordinal(latest_cleanup)
        )
        latest_inventory_failed = inventory_after_cleanup and (
            latest_inventory.get("exitCode") != 0
            or latest_inventory.get("authoritativeZero") is not True
        )
        allowed_sequence = (
            len(cleanup_attempts) < MAX_CLEANUP_ATTEMPTS
            and (
                latest_cleanup is None
                or latest_cleanup.get("exitCode") != 0
                or latest_inventory_failed
            )
        )
    elif stage == "inventory":
        inventory_after_cleanup = [
            attempt for attempt in attempts
            if attempt.get("stage") == "inventory"
            and latest_cleanup is not None
            and attempt_ordinal(attempt) > attempt_ordinal(latest_cleanup)
        ]
        interrupted_final_inventory = (
            len(cleanup_attempts) >= MAX_CLEANUP_ATTEMPTS
            and len(inventory_after_cleanup) == 1
            and inventory_after_cleanup[0].get("status") == "interrupted-unconfirmed"
        )
        allowed_sequence = (
            latest_cleanup is not None
            and latest_cleanup.get("status") != "in-progress"
            and (
                latest_inventory is None
                or attempt_ordinal(latest_cleanup) > attempt_ordinal(latest_inventory)
                or interrupted_final_inventory
            )
        )
    else:
        inventory_is_latest = (
            latest_inventory is not None
            and latest_cleanup is not None
            and attempt_ordinal(latest_inventory) > attempt_ordinal(latest_cleanup)
        )
        cleanup_retries_exhausted = len(cleanup_attempts) >= MAX_CLEANUP_ATTEMPTS
        allowed_sequence = (
            inventory_is_latest
            and (latest_inventory.get("exitCode") == 0 or cleanup_retries_exhausted)
            and not any(attempt.get("stage") == "evaluate" for attempt in attempts)
        )

    if in_progress:
        # Only cleanup/inventory may recover an unconfirmed subprocess. The
        # recovery is persisted by execute_stage before this gate is evaluated.
        no_unconfirmed_attempt = stage in {"cleanup", "inventory"}
    else:
        no_unconfirmed_attempt = True

    paid_started = document.get("paidStartedAt")
    elapsed_seconds = 0.0 if not paid_started else max(0.0, (now - parse_utc(paid_started)).total_seconds())
    elapsed_minutes = elapsed_seconds / 60
    cleanup_seconds_remaining = CLEANUP_START_MINUTES * 60 - elapsed_seconds
    hard_seconds_remaining = HARD_DEADLINE_MINUTES * 60 - elapsed_seconds
    operational_maximum = float(cost_model.get("operationalMaximumUsd", 10**9))
    maximum_including_cleanup = float(
        cost_model.get("maximumIncludingCleanupUsd", 10**9)
    )
    cleanup_reserve = maximum_including_cleanup - operational_maximum
    cost_ok = (
        cost_model.get("passed") is True
        and operational_maximum < CAMPAIGN_NEW_WORK_STOP_USD
        and maximum_including_cleanup <= CAMPAIGN_HARD_CAP_USD
        and cleanup_reserve >= MINIMUM_CLEANUP_RESERVE_USD
    )
    drain_deadline = drain_completion_deadline(document)
    drain_execution = drain_execution_deadline(document)
    drain_deadline_open = (
        stage != "drain_validate"
        or (drain_deadline is not None and now < drain_deadline)
    )
    score_end_admission_met = (
        stage != "drain_validate" or score_end_within_admission(document)
    )
    if stage in PRE_CLEANUP_STAGES:
        remaining_reserve = admission_reserve_seconds(stage, timeout_seconds)
        worst_case_fits = cleanup_seconds_remaining >= remaining_reserve
    else:
        remaining_reserve = 0
        worst_case_fits = True
    checks = {
        "stageSequenceValid": allowed_sequence,
        "noUnconfirmedAttempt": no_unconfirmed_attempt,
        "cleanupOnlyStateRespected": stage in SAFE_FINALIZATION_STAGES or not cleanup_only,
        "stageWorstCaseFitsCleanupWindow": worst_case_fits,
        "scoreEndAdmissionMilestoneMet": score_end_admission_met,
        "dynamicDrainDeadlineOpen": drain_deadline_open,
        "hardDeadlineNotPassedForNonCleanup": stage in SAFE_FINALIZATION_STAGES or hard_seconds_remaining > 0,
        "costGatePassedForNewWork": stage not in NEW_WORK_STAGES or cost_ok,
        "oneShotNotPreviouslyAttempted": stage not in ONE_SHOT_STAGES or not any(
            attempt.get("stage") == stage for attempt in attempts
        ),
    }
    cleanup_required = (
        elapsed_minutes >= CLEANUP_START_MINUTES
        or cleanup_only
        or bool(cleanup_attempts)
        or not worst_case_fits
        or not score_end_admission_met
        or not drain_deadline_open
        or (stage in NEW_WORK_STAGES and not cost_ok)
    )
    return {
        "evaluatedAt": timestamp(now),
        "stage": stage,
        "elapsedPaidMinutes": round(elapsed_minutes, 3),
        "stageTimeoutSeconds": timeout_seconds,
        "remainingPreCleanupReserveSeconds": remaining_reserve,
        "dynamicDrainDeadline": timestamp(drain_deadline) if drain_deadline else None,
        "collectionSafeDrainDeadline": timestamp(drain_execution) if drain_execution else None,
        "cleanupWindowSecondsRemaining": round(cleanup_seconds_remaining, 3),
        "hardDeadlineSecondsRemaining": round(hard_seconds_remaining, 3),
        "checks": checks,
        "allowed": all(checks.values()),
        "cleanupRequired": cleanup_required,
        "hardDeadlineBreached": elapsed_minutes >= HARD_DEADLINE_MINUTES,
    }


def execute_stage(
    run_dir: Path,
    stage: str,
    command_document: dict[str, Any],
    *,
    now_provider: Callable[[], datetime] | None = None,
    process_runner: Callable[[list[str], str, dict[str, str], int], ProcessOutcome] | None = None,
) -> dict[str, Any]:
    now_provider = now_provider or current_datetime
    process_runner = process_runner or run_command
    if stage in NEW_WORK_STAGES:
        reject_strict_paid_work_under_composite_policy(
            Path(__file__).resolve().parents[3]
        )
    document = read_json(run_dir / "run.json")
    cost_model = read_json(run_dir / "inputs" / "cost-model.json")
    argv, cwd, environment_values = validate_command_document(command_document)
    environment = stage_environment(environment_values)
    timeout_seconds = command_timeout(stage, command_document)
    command_sha256 = hashlib.sha256(
        json.dumps(command_document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    verify_command_seal(run_dir, document, stage, command_sha256)

    now = now_provider()
    if document.get("inProgressStage") and stage in {"cleanup", "inventory"}:
        document = recover_interrupted_attempt(document, now)
        write_json(run_dir / "run.json", document)

    if stage == "drain_validate":
        document = bind_score_timing(run_dir, document)
        write_json(run_dir / "run.json", document)
    timeout_seconds = effective_stage_timeout(document, stage, now, timeout_seconds)

    gate = stage_gate(document, stage, now, cost_model, timeout_seconds)
    write_json(run_dir / "evidence" / "control" / f"{stage}-gate.json", gate)
    if not gate["allowed"]:
        if gate["cleanupRequired"] and stage in PRE_CLEANUP_STAGES:
            document["cleanupOnly"] = True
            document["status"] = "cleanup-required"
            document["failureDisposition"] = "hard-stop"
            document["hardStopReason"] = "stage gate left insufficient safe time or cost headroom"
            write_json(run_dir / "run.json", document)
        raise RuntimeError(f"stage gate failed: {stage}")

    if stage == "deploy" and document.get("paidStartedAt") is None:
        paid_start = now
        document["paidStartedAt"] = timestamp(paid_start)
        document["cleanupStartDeadline"] = timestamp(paid_start + timedelta(minutes=CLEANUP_START_MINUTES))
        document["hardDeadline"] = timestamp(paid_start + timedelta(minutes=HARD_DEADLINE_MINUTES))

    ordinal = max((attempt_ordinal(item) for item in stage_attempts(document)), default=0) + 1
    attempt_number = 1 + sum(1 for item in stage_attempts(document) if item.get("stage") == stage)
    attempt = {
        "ordinal": ordinal,
        "attempt": attempt_number,
        "stage": stage,
        "commandSha256": command_sha256,
        "startedAt": timestamp(now),
        "timeoutSeconds": timeout_seconds,
        "status": "in-progress",
        "exitCode": None,
        "failureDisposition": None,
    }
    document.setdefault("attemptedStages", []).append(stage)
    document.setdefault("stageAttempts", []).append(attempt)
    document["inProgressStage"] = {
        "ordinal": ordinal,
        "attempt": attempt_number,
        "stage": stage,
        "commandSha256": command_sha256,
        "startedAt": attempt["startedAt"],
        "timeoutSeconds": timeout_seconds,
    }
    document["status"] = "running"
    write_json(run_dir / "run.json", document)

    try:
        outcome = process_runner(argv, cwd, environment, timeout_seconds)
    except Exception as error:  # subprocess setup errors are hard stops; process death remains unconfirmed.
        outcome = ProcessOutcome(
            returncode=125,
            stdout="",
            stderr=f"{type(error).__name__}: {error}\n",
        )

    finished = now_provider()
    disposition = None
    if outcome.returncode != 0:
        if outcome.timed_out:
            disposition = "hard-stop"
        else:
            disposition = str(command_document.get(
                "nonzeroDisposition",
                "acceptance-failure" if stage == "evaluate" else "hard-stop",
            ))
    evidence = {
        "schemaVersion": 2,
        "stage": stage,
        "attempt": attempt_number,
        "startedAt": attempt["startedAt"],
        "finishedAt": timestamp(finished),
        "commandSha256": command_sha256,
        "timeoutSeconds": timeout_seconds,
        "timedOut": outcome.timed_out,
        "exitCode": outcome.returncode,
        "failureDisposition": disposition,
        "stdoutPath": evidence_relative_path(stage, attempt_number, "stdout.log"),
        "stderrPath": evidence_relative_path(stage, attempt_number, "stderr.log"),
        "passed": outcome.returncode == 0,
    }
    control = run_dir / "evidence" / "control"
    control.mkdir(parents=True, exist_ok=True)
    write_stage_log(control, stage, attempt_number, "stdout.log", outcome.stdout)
    write_stage_log(control, stage, attempt_number, "stderr.log", outcome.stderr)
    write_json(control / f"{stage}.attempt-{attempt_number}.json", evidence)
    write_json(control / f"{stage}.json", evidence)

    document = read_json(run_dir / "run.json")
    finish_attempt(document, ordinal, outcome, timestamp(finished), disposition)
    document["inProgressStage"] = None
    if outcome.returncode == 0:
        if stage not in document.setdefault("completedStages", []):
            document["completedStages"].append(stage)
        if stage == "cleanup":
            document["status"] = "cleanup-executed"
        elif stage == "inventory":
            document["status"] = "cleanup-verified"
        elif stage == "evaluate":
            document["status"] = "finalized"
            if document.get("verdict") not in {"passed", "failed", "blocked", "inconclusive"}:
                document["verdict"] = "failed" if document.get("failedStage") else "passed"
            document["finalizedAt"] = timestamp(finished)
        else:
            document["status"] = "running"
    elif stage in {"cleanup", "inventory"}:
        document["cleanupOnly"] = True
        document["status"] = "cleanup-required" if stage == "cleanup" else "cleanup-unverified"
        document["failureDisposition"] = disposition
    elif stage == "evaluate":
        document["status"] = "finalized"
        if document.get("verdict") not in {"passed", "failed", "blocked", "inconclusive"}:
            document["verdict"] = "inconclusive"
        document["failedStage"] = document.get("failedStage") or stage
        document["failureDisposition"] = disposition
        document["finalizedAt"] = timestamp(finished)
    else:
        document["failedStage"] = document.get("failedStage") or stage
        document["failureDisposition"] = disposition
        document["cleanupOnly"] = True
        document["status"] = "cleanup-required"
    write_json(run_dir / "run.json", document)
    return evidence


def validate_command_document(command_document: dict[str, Any]) -> tuple[list[str], str, dict[str, str]]:
    argv = command_document.get("argv")
    cwd = command_document.get("cwd")
    environment = command_document.get("environment", {})
    if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value for value in argv):
        raise ValueError("command document argv must be a non-empty string array")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError("command document cwd must be an absolute path")
    if not isinstance(environment, dict):
        raise ValueError("command document environment must be an object")
    if any(str(key).lower() in {"aws_secret_access_key", "aws_access_key_id", "aws_session_token"} for key in environment):
        raise ValueError("static AWS credentials are forbidden in stage command documents")
    disposition = command_document.get("nonzeroDisposition")
    if disposition not in {None, "acceptance-failure", "hard-stop"}:
        raise ValueError("nonzeroDisposition must be acceptance-failure or hard-stop")
    return list(argv), cwd, {str(key): str(value) for key, value in environment.items()}


def stage_environment(overrides: dict[str, str]) -> dict[str, str]:
    forbidden = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
    inherited = sorted(key for key in forbidden if os.environ.get(key))
    if inherited:
        raise RuntimeError(
            "stage runner refuses inherited AWS credential environment variables; use fresh aws login"
        )
    environment = os.environ.copy()
    environment.update(overrides)
    return environment


def verify_command_seal(
    run_dir: Path,
    document: dict[str, Any],
    stage: str,
    command_sha256: str,
) -> None:
    if document.get("commandSetRequired") is not True:
        return
    seal = read_json(run_dir / "inputs" / "command-seal.json")
    if (
        seal.get("runId") != document.get("runId")
        or seal.get("sessionId") != document.get("sessionId")
        or seal.get("commandSetSha256") != document.get("commandSetSha256")
        or set(seal.get("commands", {})) != set(STAGES)
        or seal.get("commands", {}).get(stage, {}).get("sha256") != command_sha256
    ):
        raise RuntimeError("stage command differs from the pre-deploy sealed command set")


def command_timeout(stage: str, command_document: dict[str, Any]) -> int:
    ceiling = STAGE_TIMEOUT_SECONDS[stage]
    requested = command_document.get("timeoutSeconds", ceiling)
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        raise ValueError("timeoutSeconds must be a positive integer")
    if requested > ceiling:
        raise ValueError(f"timeoutSeconds exceeds the {stage} ceiling of {ceiling}")
    return requested


def admission_reserve_seconds(stage: str, timeout_seconds: int) -> int:
    if stage not in PRE_CLEANUP_STAGES:
        return 0
    index = PRE_CLEANUP_STAGES.index(stage)
    if stage == "drain_validate":
        return timeout_seconds + COLLECT_ADMISSION_SECONDS
    current = (
        SCORE_END_ADMISSION_SECONDS
        if stage == "score_archive"
        else timeout_seconds
    )
    return current + sum(
        STAGE_ADMISSION_SECONDS[item]
        for item in PRE_CLEANUP_STAGES[index + 1:]
    )


def bind_score_timing(run_dir: Path, document: dict[str, Any]) -> dict[str, Any]:
    summary = read_json(run_dir / "evidence" / "score" / "stage-summary.json")
    aggregate = summary.get("aggregate")
    nodes = aggregate.get("nodes") if isinstance(aggregate, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise RuntimeError("score summary has no node completion timestamps")
    try:
        score_ended = max(parse_utc(str(node["endedAt"])) for node in nodes)
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("score node completion timestamps are invalid") from error
    score_attempt = latest_attempt(stage_attempts(document), "score_archive")
    if score_attempt is None:
        raise RuntimeError("score timing cannot be bound without a score attempt")
    document["scoreEndedAt"] = timestamp(score_ended)
    document["scoreDrainDeadline"] = timestamp(
        score_ended + timedelta(seconds=POST_SCORE_DRAIN_SECONDS)
    )
    return document


def score_end_within_admission(document: dict[str, Any]) -> bool:
    attempt = latest_attempt(stage_attempts(document), "score_archive")
    try:
        return (
            attempt is not None
            and parse_utc(str(document["scoreEndedAt"]))
            <= parse_utc(str(attempt["startedAt"]))
            + timedelta(seconds=SCORE_END_ADMISSION_SECONDS)
        )
    except (KeyError, TypeError, ValueError):
        return False


def drain_completion_deadline(document: dict[str, Any]) -> datetime | None:
    try:
        score_deadline = parse_utc(str(document["scoreDrainDeadline"]))
        cleanup_deadline = parse_utc(str(document["cleanupStartDeadline"]))
    except (KeyError, TypeError, ValueError):
        return None
    return min(score_deadline, cleanup_deadline)


def drain_execution_deadline(document: dict[str, Any]) -> datetime | None:
    absolute_deadline = drain_completion_deadline(document)
    try:
        cleanup_deadline = parse_utc(str(document["cleanupStartDeadline"]))
    except (KeyError, TypeError, ValueError):
        return absolute_deadline
    collection_safe_deadline = cleanup_deadline - timedelta(
        seconds=COLLECT_ADMISSION_SECONDS
    )
    return (
        collection_safe_deadline
        if absolute_deadline is None
        else min(absolute_deadline, collection_safe_deadline)
    )


def effective_stage_timeout(
    document: dict[str, Any], stage: str, now: datetime, watchdog_seconds: int
) -> int:
    if stage != "drain_validate":
        return watchdog_seconds
    deadline = drain_execution_deadline(document)
    if deadline is None:
        return watchdog_seconds
    remaining = int((deadline - now).total_seconds())
    return max(1, min(watchdog_seconds, remaining))


def run_command(argv: list[str], cwd: str, environment: dict[str, str], timeout_seconds: int) -> ProcessOutcome:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return ProcessOutcome(process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        terminate_process_group(process, signal.SIGTERM)
        try:
            # The JS load orchestrator handles SIGTERM by cancelling exact SSM
            # commands and the exact archive task. Give that bounded remote
            # cancellation path time to persist evidence before SIGKILL.
            stdout, stderr = process.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            terminate_process_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
        stderr = f"{stderr}\nrunner watchdog exceeded {timeout_seconds} seconds\n"
        return ProcessOutcome(124, stdout, stderr, timed_out=True)


def terminate_process_group(process: subprocess.Popen[str], requested_signal: signal.Signals) -> None:
    try:
        os.killpg(process.pid, requested_signal)
    except ProcessLookupError:
        return


def recover_interrupted_attempt(document: dict[str, Any], recovered_at: datetime) -> dict[str, Any]:
    in_progress = document.get("inProgressStage")
    if not isinstance(in_progress, dict):
        return document
    ordinal = attempt_ordinal(in_progress)
    attempts = stage_attempts(document)
    for attempt in attempts:
        if attempt_ordinal(attempt) == ordinal:
            attempt["status"] = "interrupted-unconfirmed"
            attempt["finishedAt"] = timestamp(recovered_at)
            attempt["failureDisposition"] = "hard-stop"
            break
    interrupted_stage = str(in_progress.get("stage"))
    document.setdefault("interruptedStages", []).append({
        "ordinal": ordinal,
        "stage": interrupted_stage,
        "recoveredAt": timestamp(recovered_at),
        "disposition": "cleanup-only",
    })
    document["inProgressStage"] = None
    document["cleanupOnly"] = True
    document["status"] = "cleanup-required"
    document["failureDisposition"] = "hard-stop"
    if interrupted_stage not in SAFE_FINALIZATION_STAGES:
        document["failedStage"] = document.get("failedStage") or interrupted_stage
    return document


def finish_attempt(document: dict[str, Any], ordinal: int, outcome: ProcessOutcome,
                   finished_at: str, disposition: str | None) -> None:
    for attempt in stage_attempts(document):
        if attempt_ordinal(attempt) == ordinal:
            attempt["status"] = "timed-out" if outcome.timed_out else "finished"
            attempt["finishedAt"] = finished_at
            attempt["exitCode"] = outcome.returncode
            attempt["failureDisposition"] = disposition
            return
    raise RuntimeError("persisted in-progress attempt disappeared")


def stage_attempts(document: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = document.get("stageAttempts", [])
    return attempts if isinstance(attempts, list) else []


def latest_attempt(attempts: list[dict[str, Any]], stage: str) -> dict[str, Any] | None:
    matches = [attempt for attempt in attempts if attempt.get("stage") == stage]
    return max(matches, key=attempt_ordinal) if matches else None


def attempt_ordinal(attempt: dict[str, Any]) -> int:
    value = attempt.get("ordinal", 0)
    return value if isinstance(value, int) else 0


def write_stage_log(control: Path, stage: str, attempt: int, suffix: str, value: str) -> None:
    (control / f"{stage}.attempt-{attempt}.{suffix}").write_text(value, encoding="utf-8")
    (control / f"{stage}.{suffix}").write_text(value, encoding="utf-8")


def evidence_relative_path(stage: str, attempt: int, suffix: str) -> str:
    return f"evidence/control/{stage}.attempt-{attempt}.{suffix}"


def current_datetime() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    init_parser = subparsers.add_parser("initialize")
    init_parser.add_argument("--run-dir", required=True, type=Path)
    init_parser.add_argument("--run-id", required=True)
    init_parser.add_argument("--session-id", required=True)
    init_parser.add_argument("--preflight", required=True, type=Path)
    init_parser.add_argument("--image-manifest", required=True, type=Path)
    init_parser.add_argument("--cost-model", required=True, type=Path)
    init_parser.add_argument("--identity-contract", required=True, type=Path)
    stage_parser = subparsers.add_parser("stage")
    stage_parser.add_argument("--run-dir", required=True, type=Path)
    stage_parser.add_argument("--stage", choices=STAGES, required=True)
    stage_parser.add_argument("--command", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "initialize":
        result = initialize(
            args.run_dir.resolve(), args.run_id, args.session_id,
            args.preflight.resolve(), args.image_manifest.resolve(),
            args.cost_model.resolve(), args.identity_contract.resolve(),
        )
    else:
        result = execute_stage(args.run_dir.resolve(), args.stage, read_json(args.command))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())

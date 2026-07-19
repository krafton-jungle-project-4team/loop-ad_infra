#!/usr/bin/env python3
"""Fail-closed Phase 7-2 evidence assembly and pure run finalization.

This module performs no AWS calls.  It treats the checked-in collectors as
untrusted inputs, binds every required artifact to one run/session, records the
exact source bytes, and produces the sole input shape accepted by evaluator.py.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from common import (
    EXPECTED_ACCOUNT,
    EXPECTED_OPERATOR_ARN,
    RUNTIME_STACK_NAME,
    parse_utc,
    validate_identifiers,
    write_json,
)


@dataclass(frozen=True)
class ArtifactContract:
    path: str
    provenance: str


ARTIFACT_CONTRACTS: dict[str, ArtifactContract] = {
    "runState": ArtifactContract(
        "evidence/control/evaluate-start-run.json",
        "phase7-runner-evaluate-start",
    ),
    "commandSeal": ArtifactContract(
        "inputs/command-seal.json",
        "phase7-command-set-sealer",
    ),
    "deploymentVerification": ArtifactContract(
        "deployment-verification.json",
        "phase7-runtime-deployment-verifier",
    ),
    "correctnessSummary": ArtifactContract(
        "correctness-summary.json",
        "phase7-runtime-correctness-validator",
    ),
    "seedSummary": ArtifactContract(
        "seed-summary.json",
        "phase7-runtime-seed-validator",
    ),
    "warmupStageSummary": ArtifactContract(
        "evidence/warmup/stage-summary.json",
        "phase7-oha-warmup-orchestrator",
    ),
    "scoreStageSummary": ArtifactContract(
        "evidence/score/stage-summary.json",
        "phase7-oha-score-orchestrator",
    ),
    "drainAccounting": ArtifactContract(
        "drain-accounting.json",
        "phase7-runtime-drain-validator",
    ),
    "archiveValidation": ArtifactContract(
        "archive-validation.json",
        "phase7-archive-validator",
    ),
    "observabilitySummary": ArtifactContract(
        "observability-summary.json",
        "phase7-observability-collector",
    ),
    "costStatus": ArtifactContract(
        "cost-status.json",
        "phase7-cost-controller",
    ),
    "cleanupInventory": ArtifactContract(
        "cleanup-inventory.json",
        "phase7-cleanup-inventory",
    ),
}
REQUIRED_ARTIFACT_ALLOWLIST = frozenset(ARTIFACT_CONTRACTS)
REQUIRED_ARTIFACT_METADATA_FIELDS = frozenset(
    {"path", "sha256", "provenance", "runId", "sessionId"}
)
IDENTITY_MODES = {
    "globally-unique-event-id",
    "balanced-pool-sampled-with-replacement",
}
FINAL_VERDICTS = {"passed", "failed", "blocked", "inconclusive"}
FAILURE_FIELDS = (
    "kinesisThrottle",
    "collectorFinalFailure",
    "kclTerminalFailure",
    "failureObjects",
    "clickHouseInsertErrors",
    "archiveFailures",
    "unexpectedRestarts",
    "oomKills",
)
EXPECTED_OBSERVABILITY_HOSTS = {"collector": 6, "consumer": 2, "clickHouse": 1}
CLEANUP_SERVICE_CLASSES = frozenset(
    {
        "cloudFormationStacks",
        "ec2Instances",
        "ebsVolumes",
        "ebsSnapshots",
        "networkInterfaces",
        "natGateways",
        "vpcs",
        "subnets",
        "routeTables",
        "internetGateways",
        "securityGroups",
        "vpcEndpoints",
        "launchTemplates",
        "elasticIpAllocations",
        "autoScalingGroups",
        "ecsClusters",
        "ecsServices",
        "ecsTasks",
        "ecsContainerInstances",
        "ecsTaskDefinitions",
        "ecsCapacityProviders",
        "loadBalancers",
        "targetGroups",
        "listeners",
        "s3Buckets",
        "secrets",
        "cloudMapNamespaces",
        "cloudMapServices",
        "cloudWatchAlarms",
        "ecrRepositories",
        "ecrImages",
        "kinesisStreams",
        "dynamoDbTables",
        "logGroups",
        "iamRoles",
    }
)
IDENTITY_CONTRACT_FIELDS = (
    "predeclaredBeforeDeploy",
    "userApproved",
    "selectionWithReplacement",
    "warmupScorePoolsSeparated",
    "balancedShardCount",
    "fixturePoolRows",
)
PRE_CLEANUP_STAGE_ORDER = (
    "deploy",
    "verify",
    "correctness",
    "seed",
    "warmup",
    "score_archive",
    "drain_validate",
    "collect",
)
ALL_RUNNER_STAGES = (*PRE_CLEANUP_STAGE_ORDER, "cleanup", "inventory", "evaluate")


class EvidenceAssemblyError(ValueError):
    """Raised when source evidence cannot be trusted as one whole attempt."""


def canonical_sha256(value: Any) -> str:
    """Return a deterministic digest for JSON-compatible metadata."""

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise EvidenceAssemblyError("value is not canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def compute_assembly_sha256(evidence: Mapping[str, Any]) -> str:
    """Seal every assembled field except the seal itself."""

    payload = copy.deepcopy(dict(evidence))
    payload.pop("assemblySha256", None)
    return canonical_sha256(payload)


def assemble_evidence(run_dir: Path) -> dict[str, Any]:
    """Read and bind the exact Phase 7-2 artifact set for evaluation."""

    root = run_dir.resolve()
    if not root.is_dir():
        raise EvidenceAssemblyError(f"run directory does not exist: {root}")

    documents: dict[str, dict[str, Any]] = {}
    raw_digests: dict[str, str] = {}
    for name, contract in ARTIFACT_CONTRACTS.items():
        document, digest = _read_artifact(root, name, contract)
        documents[name] = document
        raw_digests[name] = digest

    run = documents["runState"]
    run_id = _required_string(run, "runId", "runState")
    session_id = _required_string(run, "sessionId", "runState")
    try:
        validate_identifiers(run_id, session_id)
    except ValueError as error:
        raise EvidenceAssemblyError(str(error)) from error
    if run.get("phase") != "7-2":
        raise EvidenceAssemblyError("runState.phase must be 7-2")
    if run.get("phase5") != "skipped":
        raise EvidenceAssemblyError("runState.phase5 must remain skipped")

    for name, document in documents.items():
        _require_run_binding(name, document, run_id, session_id)

    control_evidence = _validate_execution_control(
        root,
        run,
        documents["commandSeal"],
    )
    execution = _validate_execution_artifacts(
        run,
        documents["deploymentVerification"],
        documents["correctnessSummary"],
        documents["seedSummary"],
        documents["warmupStageSummary"],
        control_evidence,
    )

    score = documents["scoreStageSummary"]
    if score.get("stage") != "score":
        raise EvidenceAssemblyError("scoreStageSummary.stage must be score")
    identity_mode = _required_string(score, "identityMode", "scoreStageSummary")
    if identity_mode not in IDENTITY_MODES:
        raise EvidenceAssemblyError(f"unsupported score identityMode: {identity_mode}")
    identity_contract = _required_mapping(
        score, "identityContract", "scoreStageSummary"
    )
    _validate_identity_contract(identity_contract)
    if documents["warmupStageSummary"].get("identityMode") != identity_mode:
        raise EvidenceAssemblyError("warmup and score identity modes differ")

    aggregate = _required_mapping(score, "aggregate", "scoreStageSummary")
    attempted = _required_nonnegative_integer(
        aggregate, "attemptedRequests", "scoreStageSummary.aggregate"
    )
    if attempted == 0:
        raise EvidenceAssemblyError(
            "scoreStageSummary.aggregate.attemptedRequests must be positive"
        )
    transport_errors = _required_nonnegative_integer(
        aggregate, "transportErrors", "scoreStageSummary.aggregate"
    )
    completed = _required_nonnegative_integer(
        aggregate, "completedRequests", "scoreStageSummary.aggregate"
    )
    duration_seconds = _required_positive_integer(
        aggregate, "durationSeconds", "scoreStageSummary.aggregate"
    )
    corrected_latency = _required_mapping(
        aggregate, "latencyCorrectedMs", "scoreStageSummary.aggregate"
    )
    performance = {
        "actualRps": _required_nonnegative_number(
            aggregate, "actualRps", "scoreStageSummary.aggregate"
        ),
        "correctedP95Ms": _required_nonnegative_number(
            corrected_latency,
            "p95",
            "scoreStageSummary.aggregate.latencyCorrectedMs",
        ),
        "transportErrorRate": transport_errors / attempted,
        "attemptedRequests": attempted,
        "completedRequests": completed,
        "transportErrors": transport_errors,
        "durationSeconds": duration_seconds,
        "http429": _required_nonnegative_integer(
            aggregate, "http429", "scoreStageSummary.aggregate"
        ),
        "http5xx": _required_nonnegative_integer(
            aggregate, "http5xx", "scoreStageSummary.aggregate"
        ),
    }

    drain_document = documents["drainAccounting"]
    counts = copy.deepcopy(
        _required_mapping(drain_document, "counts", "drainAccounting")
    )
    _validate_counts(counts, identity_mode)
    if (
        identity_mode == "balanced-pool-sampled-with-replacement"
        and counts["fixturePoolRows"] != identity_contract["fixturePoolRows"]
    ):
        raise EvidenceAssemblyError(
            "drainAccounting fixturePoolRows differs from identityContract"
        )
    aggregate_http202 = _required_nonnegative_integer(
        aggregate, "http202", "scoreStageSummary.aggregate"
    )
    if counts["http202"] != aggregate_http202:
        raise EvidenceAssemblyError(
            "scoreStageSummary.aggregate.http202 and drainAccounting.counts.http202 differ"
        )
    drain = copy.deepcopy(
        _required_mapping(drain_document, "drain", "drainAccounting")
    )
    _validate_drain(drain)

    archive = copy.deepcopy(documents["archiveValidation"])
    drain_archive = _required_mapping(
        drain_document, "archive", "drainAccounting"
    )
    if drain_archive != archive:
        raise EvidenceAssemblyError(
            "drainAccounting.archive does not exactly match archiveValidation"
        )
    _validate_archive(archive)

    observability = documents["observabilitySummary"]
    resources = copy.deepcopy(
        _required_mapping(observability, "resources", "observabilitySummary")
    )
    haproxy = copy.deepcopy(
        _required_mapping(observability, "haproxy", "observabilitySummary")
    )
    failures = copy.deepcopy(
        _required_mapping(observability, "failures", "observabilitySummary")
    )
    cloudtrail = copy.deepcopy(
        _required_mapping(observability, "cloudTrail", "observabilitySummary")
    )
    _validate_observability(resources, haproxy, failures, cloudtrail)
    _validate_observability_raw_source(
        root,
        observability,
        run_id,
        session_id,
        resources,
        failures,
        drain_document,
        documents["deploymentVerification"],
    )
    archive_attempt = _required_mapping(score, "archive", "scoreStageSummary")
    archive_started_by = _required_string(
        archive_attempt, "startedBy", "scoreStageSummary.archive"
    )
    _validate_cloudtrail_source_files(
        root,
        cloudtrail,
        run_id,
        session_id,
        archive_started_by,
    )
    drain_failures = _required_mapping(
        drain_document, "failures", "drainAccounting"
    )
    for key, value in drain_failures.items():
        normalized = _coerce_nonnegative_integer(
            value, f"drainAccounting.failures.{key}"
        )
        if key in failures and failures[key] != normalized:
            raise EvidenceAssemblyError(
                f"conflicting failure evidence for {key}"
            )
        failures[key] = normalized

    cost_document = documents["costStatus"]
    cost = copy.deepcopy(_required_mapping(cost_document, "cost", "costStatus"))
    _validate_cost(cost)
    _validate_cost_source(root, cost_document, cost)
    cleanup = copy.deepcopy(documents["cleanupInventory"])
    _validate_cleanup(cleanup)

    required_artifacts = {
        name: {
            "path": contract.path,
            "sha256": raw_digests[name],
            "provenance": contract.provenance,
            "runId": run_id,
            "sessionId": session_id,
        }
        for name, contract in ARTIFACT_CONTRACTS.items()
    }
    result = {
        "schemaVersion": 1,
        "workload": "phase7-end-to-end-integration",
        "phase": "7-2",
        "phase5": "skipped",
        "runId": run_id,
        "sessionId": session_id,
        "identityMode": identity_mode,
        "identityContract": copy.deepcopy(identity_contract),
        "execution": execution,
        "controlEvidence": control_evidence,
        "controlEvidenceSha256": canonical_sha256(control_evidence),
        "performance": performance,
        "counts": counts,
        "failures": failures,
        "drain": drain,
        "resources": resources,
        "haproxy": haproxy,
        "cloudTrail": cloudtrail,
        "archive": archive,
        "cost": cost,
        "cleanup": cleanup,
        "requiredArtifacts": required_artifacts,
        "requiredArtifactsSha256": canonical_sha256(required_artifacts),
        "requiredArtifactsVerifiedFromRunDirectory": True,
    }
    result["assemblySha256"] = compute_assembly_sha256(result)
    return result


def required_artifacts_are_valid(
    evidence: Mapping[str, Any], run_dir: Path | None = None
) -> bool:
    """Validate the exact artifact allowlist and its bound provenance manifest."""

    run_id = evidence.get("runId")
    session_id = evidence.get("sessionId")
    if not isinstance(run_id, str) or not isinstance(session_id, str):
        return False
    try:
        validate_identifiers(run_id, session_id)
    except ValueError:
        return False
    manifest = evidence.get("requiredArtifacts")
    if not isinstance(manifest, dict) or set(manifest) != REQUIRED_ARTIFACT_ALLOWLIST:
        return False
    for name, contract in ARTIFACT_CONTRACTS.items():
        metadata = manifest.get(name)
        if not isinstance(metadata, dict) or set(metadata) != REQUIRED_ARTIFACT_METADATA_FIELDS:
            return False
        digest = metadata.get("sha256")
        if (
            metadata.get("path") != contract.path
            or metadata.get("provenance") != contract.provenance
            or metadata.get("runId") != run_id
            or metadata.get("sessionId") != session_id
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return False
    manifest_digest = evidence.get("requiredArtifactsSha256")
    try:
        locally_sealed = (
            isinstance(manifest_digest, str)
            and manifest_digest == canonical_sha256(manifest)
            and evidence.get("requiredArtifactsVerifiedFromRunDirectory") is True
            and isinstance(evidence.get("assemblySha256"), str)
            and evidence.get("assemblySha256") == compute_assembly_sha256(evidence)
        )
    except EvidenceAssemblyError:
        return False
    if not locally_sealed:
        return False
    if run_dir is None:
        return True
    try:
        rebuilt = assemble_evidence(run_dir)
    except (EvidenceAssemblyError, OSError):
        return False
    return dict(evidence) == rebuilt


def finalize_run_document(
    run_document: Mapping[str, Any], evaluation: Mapping[str, Any]
) -> dict[str, Any]:
    """Return, without mutating inputs, a terminal run.json document."""

    if run_document.get("phase") != "7-2" or run_document.get("phase5") != "skipped":
        raise EvidenceAssemblyError("only a phase 7-2 run with phase5=skipped can finalize")
    if run_document.get("verdict") is not None or run_document.get("status") in {
        "completed",
        "finalized",
    }:
        raise EvidenceAssemblyError("a terminal run document may not be finalized again")
    run_id = run_document.get("runId")
    session_id = run_document.get("sessionId")
    if (
        evaluation.get("runId") != run_id
        or evaluation.get("sessionId") != session_id
        or evaluation.get("phase5") != "skipped"
    ):
        raise EvidenceAssemblyError("evaluation run/session/phase5 binding mismatch")
    verdict = evaluation.get("verdict")
    checks = evaluation.get("checks")
    if verdict not in FINAL_VERDICTS or not isinstance(checks, dict) or not checks:
        raise EvidenceAssemblyError("evaluation must contain a supported final verdict")
    if not all(isinstance(value, bool) for value in checks.values()):
        raise EvidenceAssemblyError("evaluation checks must be booleans")
    if all(checks.values()) != (verdict == "passed"):
        raise EvidenceAssemblyError("evaluation verdict is inconsistent with its checks")
    failed_checks = sorted(name for name, passed in checks.items() if not passed)
    if evaluation.get("failedChecks") != failed_checks:
        raise EvidenceAssemblyError("evaluation failedChecks is inconsistent with its checks")
    verdict_basis = evaluation.get("verdictBasis")
    if not isinstance(verdict_basis, str) or not verdict_basis:
        raise EvidenceAssemblyError("evaluation.verdictBasis is required")
    evaluated_at = evaluation.get("evaluatedAt")
    if not isinstance(evaluated_at, str) or not evaluated_at.endswith("Z"):
        raise EvidenceAssemblyError("evaluation.evaluatedAt is required")
    try:
        parse_utc(evaluated_at)
    except (TypeError, ValueError) as error:
        raise EvidenceAssemblyError("evaluation.evaluatedAt must be UTC timestamp evidence") from error

    result = copy.deepcopy(dict(run_document))
    result.update(
        {
            "status": "completed",
            "verdict": verdict,
            "phase5": "skipped",
            "completedAt": evaluated_at,
            "finalEvaluation": {
                "verdict": verdict,
                "basis": verdict_basis,
                "evaluatedAt": evaluated_at,
                "sha256": canonical_sha256(evaluation),
                "failedChecks": copy.deepcopy(failed_checks),
            },
        }
    )
    return result


def validate_cleanup_inventory_document(
    cleanup: Mapping[str, Any], run_id: str, session_id: str
) -> bool:
    """Validate exact run binding and authoritative cleanup service classes."""

    if cleanup.get("schemaVersion") != 1:
        raise EvidenceAssemblyError("cleanupInventory.schemaVersion must be 1")
    if cleanup.get("runId") != run_id or cleanup.get("sessionId") != session_id:
        raise EvidenceAssemblyError("cleanupInventory runId/sessionId binding mismatch")
    _validate_cleanup(cleanup)
    return cleanup.get("allZero") is True


def _read_artifact(
    root: Path, name: str, contract: ArtifactContract
) -> tuple[dict[str, Any], str]:
    candidate = root / contract.path
    if candidate.is_symlink():
        raise EvidenceAssemblyError(f"{name} must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise EvidenceAssemblyError(
            f"missing or out-of-run required artifact: {contract.path}"
        ) from error
    if not resolved.is_file():
        raise EvidenceAssemblyError(f"required artifact is not a file: {contract.path}")
    raw = resolved.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceAssemblyError(f"corrupt JSON artifact: {contract.path}") from error
    if not isinstance(document, dict):
        raise EvidenceAssemblyError(f"{contract.path} must contain a JSON object")
    expected_schema = 2 if name == "runState" else 1
    if document.get("schemaVersion") != expected_schema:
        raise EvidenceAssemblyError(
            f"{contract.path}.schemaVersion must be {expected_schema}"
        )
    return document, hashlib.sha256(raw).hexdigest()


def _require_run_binding(
    name: str, document: Mapping[str, Any], run_id: str, session_id: str
) -> None:
    if document.get("runId") != run_id or document.get("sessionId") != session_id:
        raise EvidenceAssemblyError(f"{name} runId/sessionId binding mismatch")


def _required_mapping(
    document: Mapping[str, Any], key: str, context: str
) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise EvidenceAssemblyError(f"{context}.{key} must be an object")
    return value


def _required_string(document: Mapping[str, Any], key: str, context: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise EvidenceAssemblyError(f"{context}.{key} must be a non-empty string")
    return value


def _required_boolean(document: Mapping[str, Any], key: str, context: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise EvidenceAssemblyError(f"{context}.{key} must be boolean")
    return value


def _required_number(document: Mapping[str, Any], key: str, context: str) -> float:
    value = document.get(key)
    if isinstance(value, bool):
        raise EvidenceAssemblyError(f"{context}.{key} must be a finite number")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise EvidenceAssemblyError(f"{context}.{key} must be a finite number") from error
    if not math.isfinite(numeric):
        raise EvidenceAssemblyError(f"{context}.{key} must be a finite number")
    return numeric


def _required_nonnegative_number(
    document: Mapping[str, Any], key: str, context: str
) -> float:
    numeric = _required_number(document, key, context)
    if numeric < 0:
        raise EvidenceAssemblyError(f"{context}.{key} must be nonnegative")
    return numeric


def _required_nonnegative_integer(
    document: Mapping[str, Any], key: str, context: str
) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceAssemblyError(f"{context}.{key} must be a nonnegative integer")
    return value


def _required_positive_integer(
    document: Mapping[str, Any], key: str, context: str
) -> int:
    value = _required_nonnegative_integer(document, key, context)
    if value == 0:
        raise EvidenceAssemblyError(f"{context}.{key} must be positive")
    return value


def _coerce_nonnegative_integer(value: Any, context: str) -> int:
    if isinstance(value, bool):
        raise EvidenceAssemblyError(f"{context} must be a nonnegative integer")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise EvidenceAssemblyError(
            f"{context} must be a nonnegative integer"
        ) from error
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        raise EvidenceAssemblyError(f"{context} must be a nonnegative integer")
    return int(numeric)


def _validate_identity_contract(contract: Mapping[str, Any]) -> None:
    for field in IDENTITY_CONTRACT_FIELDS[:4]:
        if not isinstance(contract.get(field), bool):
            raise EvidenceAssemblyError(f"identityContract.{field} must be boolean")
    for field in IDENTITY_CONTRACT_FIELDS[4:]:
        _required_nonnegative_integer(contract, field, "identityContract")


def _read_control_source(
    root: Path, relative: str, context: str
) -> tuple[dict[str, Any], str]:
    path = root / relative
    if path.is_symlink():
        raise EvidenceAssemblyError(f"{context} must not be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise EvidenceAssemblyError(f"missing or out-of-run {context}: {relative}") from error
    if not resolved.is_file():
        raise EvidenceAssemblyError(f"{context} is not a file: {relative}")
    raw = resolved.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceAssemblyError(f"{context} is not valid JSON: {relative}") from error
    if not isinstance(value, dict):
        raise EvidenceAssemblyError(f"{context} must contain a JSON object: {relative}")
    return value, hashlib.sha256(raw).hexdigest()


def _validate_execution_control(
    root: Path,
    run: Mapping[str, Any],
    seal: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind runner self-reporting to sealed commands and immutable control bytes."""

    commands = _required_mapping(seal, "commands", "commandSeal")
    if set(commands) != set(ALL_RUNNER_STAGES):
        raise EvidenceAssemblyError("commandSeal must contain the exact runner stages")

    command_digests: dict[str, str] = {}
    command_documents: dict[str, dict[str, Any]] = {}
    command_sources: list[dict[str, str]] = []
    normalized_metadata: dict[str, dict[str, str]] = {}
    for stage in ALL_RUNNER_STAGES:
        metadata = commands.get(stage)
        expected_path = f"inputs/{stage}-command.json"
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"path", "sha256"}
            or metadata.get("path") != expected_path
            or re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("sha256", ""))) is None
        ):
            raise EvidenceAssemblyError(f"commandSeal metadata is invalid for {stage}")
        command, raw_digest = _read_control_source(
            root, expected_path, f"sealed {stage} command"
        )
        canonical_digest = canonical_sha256(command)
        if canonical_digest != metadata["sha256"]:
            raise EvidenceAssemblyError(f"sealed {stage} command content digest mismatch")
        if (
            command.get("schemaVersion") != 1
            or not isinstance(command.get("argv"), list)
            or not command.get("argv")
            or not all(isinstance(value, str) and value for value in command["argv"])
            or not isinstance(command.get("cwd"), str)
            or not Path(command["cwd"]).is_absolute()
            or not isinstance(command.get("environment"), dict)
        ):
            raise EvidenceAssemblyError(f"sealed {stage} command contract is invalid")
        command_digests[stage] = canonical_digest
        command_documents[stage] = command
        normalized_metadata[stage] = {
            "path": expected_path,
            "sha256": canonical_digest,
        }
        command_sources.append({"path": expected_path, "sha256": raw_digest})

    command_set_digest = canonical_sha256(normalized_metadata)
    if (
        seal.get("schemaVersion") != 1
        or seal.get("commandSetSha256") != command_set_digest
        or run.get("commandSetRequired") is not True
        or run.get("commandSetSha256") != command_set_digest
    ):
        raise EvidenceAssemblyError("runner command-set seal does not match actual command content")

    attempts = run.get("stageAttempts")
    if not isinstance(attempts, list) or not all(isinstance(item, dict) for item in attempts):
        raise EvidenceAssemblyError("runState stage attempts are invalid for control verification")
    if not attempts or attempts[-1].get("stage") != "evaluate":
        raise EvidenceAssemblyError("evaluate-start state must end with the evaluate attempt")

    attempt_sources: list[dict[str, str]] = []
    expected_attempt_paths: set[Path] = set()
    stage_attempt_numbers: dict[str, int] = {}
    for index, attempt in enumerate(attempts):
        stage = str(attempt.get("stage", ""))
        if stage not in command_digests:
            raise EvidenceAssemblyError(f"runState contains an unknown stage: {stage}")
        stage_attempt_numbers[stage] = stage_attempt_numbers.get(stage, 0) + 1
        attempt_number = stage_attempt_numbers[stage]
        command_document = command_documents[stage]
        if (
            attempt.get("attempt") != attempt_number
            or attempt.get("commandSha256") != command_digests[stage]
            or attempt.get("timeoutSeconds")
            != _required_positive_integer(
                command_document, "timeoutSeconds", f"sealed {stage} command"
            )
        ):
            raise EvidenceAssemblyError(f"runState attempt differs from sealed command: {stage}")

        is_evaluate_start = index == len(attempts) - 1
        if is_evaluate_start:
            if (
                attempt.get("status") != "in-progress"
                or attempt.get("exitCode") is not None
                or attempt.get("failureDisposition") is not None
                or "finishedAt" in attempt
            ):
                raise EvidenceAssemblyError("evaluate-start attempt state is not immutable in-progress evidence")
            continue

        relative = f"evidence/control/{stage}.attempt-{attempt_number}.json"
        control, digest = _read_control_source(root, relative, f"{stage} attempt control")
        expected_attempt_paths.add(root / relative)
        expected_status = "timed-out" if control.get("timedOut") is True else "finished"
        comparable = (
            "stage",
            "attempt",
            "startedAt",
            "finishedAt",
            "commandSha256",
            "timeoutSeconds",
            "exitCode",
            "failureDisposition",
        )
        if (
            control.get("schemaVersion") != 2
            or any(control.get(field) != attempt.get(field) for field in comparable)
            or control.get("passed") is not (attempt.get("exitCode") == 0)
            or attempt.get("status") != expected_status
            or control.get("stdoutPath") != f"evidence/control/{stage}.attempt-{attempt_number}.stdout.log"
            or control.get("stderrPath") != f"evidence/control/{stage}.attempt-{attempt_number}.stderr.log"
        ):
            raise EvidenceAssemblyError(f"runState and {stage} attempt control evidence differ")
        attempt_sources.append({"path": relative, "sha256": digest})

    control_root = root / "evidence" / "control"
    discovered_attempt_paths = {
        path for path in control_root.glob("*.attempt-*.json")
        if not path.name.startswith("evaluate.attempt-")
    }
    if discovered_attempt_paths != expected_attempt_paths:
        raise EvidenceAssemblyError("runner attempt control file set is incomplete or contains extras")

    paid_started = parse_utc(_required_string(run, "paidStartedAt", "runState"))
    gate_sources: list[dict[str, str]] = []
    expected_gate_paths: set[Path] = set()
    latest_by_stage: dict[str, Mapping[str, Any]] = {}
    for attempt in attempts:
        latest_by_stage[str(attempt["stage"])] = attempt
    for stage in ALL_RUNNER_STAGES:
        attempt = latest_by_stage.get(stage)
        if attempt is None:
            raise EvidenceAssemblyError(f"runner control evidence is missing stage {stage}")
        relative = f"evidence/control/{stage}-gate.json"
        gate, digest = _read_control_source(root, relative, f"{stage} gate control")
        expected_gate_paths.add(root / relative)
        started = parse_utc(str(attempt.get("startedAt")))
        elapsed_seconds = max(0.0, (started - paid_started).total_seconds())
        checks = gate.get("checks")
        if (
            gate.get("stage") != stage
            or gate.get("evaluatedAt") != attempt.get("startedAt")
            or gate.get("stageTimeoutSeconds") != attempt.get("timeoutSeconds")
            or not isinstance(checks, dict)
            or not checks
            or not all(value is True for value in checks.values())
            or gate.get("allowed") is not True
            or not math.isclose(
                _required_number(gate, "elapsedPaidMinutes", f"{stage} gate"),
                round(elapsed_seconds / 60, 3),
                abs_tol=0.001,
            )
            or not math.isclose(
                _required_number(
                    gate,
                    "cleanupWindowSecondsRemaining",
                    f"{stage} gate",
                ),
                round(160 * 60 - elapsed_seconds, 3),
                abs_tol=0.001,
            )
            or not math.isclose(
                _required_number(
                    gate,
                    "hardDeadlineSecondsRemaining",
                    f"{stage} gate",
                ),
                round(180 * 60 - elapsed_seconds, 3),
                abs_tol=0.001,
            )
            or gate.get("hardDeadlineBreached") is not (elapsed_seconds >= 180 * 60)
        ):
            raise EvidenceAssemblyError(f"runner {stage} gate differs from attempt/deadline evidence")
        gate_sources.append({"path": relative, "sha256": digest})
    discovered_gate_paths = set(control_root.glob("*-gate.json"))
    if discovered_gate_paths != expected_gate_paths:
        raise EvidenceAssemblyError("runner gate control file set is incomplete or contains extras")

    return {
        "schemaVersion": 1,
        "commandSetSha256": command_set_digest,
        "commandFiles": command_sources,
        "gateFiles": gate_sources,
        "attemptFiles": attempt_sources,
        "evaluateStartSnapshot": ARTIFACT_CONTRACTS["runState"].path,
        "verified": True,
    }


def _validate_execution_artifacts(
    run: Mapping[str, Any],
    deployment: Mapping[str, Any],
    correctness_document: Mapping[str, Any],
    seed: Mapping[str, Any],
    warmup: Mapping[str, Any],
    control_evidence: Mapping[str, Any],
) -> dict[str, bool]:
    deployment_passed = _required_boolean(
        deployment, "passed", "deploymentVerification"
    )
    identity = _required_mapping(
        deployment, "identity", "deploymentVerification"
    )
    stream = _required_mapping(deployment, "stream", "deploymentVerification")
    protocol_path = _required_mapping(
        deployment, "protocolPath", "deploymentVerification"
    )
    deployment_exact = (
        deployment_passed
        and deployment.get("stackStatus") == "CREATE_COMPLETE"
        and identity.get("account") == EXPECTED_ACCOUNT
        and identity.get("arn") == EXPECTED_OPERATOR_ARN
        and stream.get("status") == "ACTIVE"
        and _required_nonnegative_integer(
            stream, "openShardCount", "deploymentVerification.stream"
        ) == 120
        and protocol_path.get("scheme") == "internal"
        and protocol_path.get("activeBackendsPerProxy") == [6, 6]
    )

    correctness = _required_mapping(
        correctness_document, "correctness", "correctnessSummary"
    )
    replacement = _required_mapping(
        correctness_document, "replacement", "correctnessSummary"
    )
    http = _required_mapping(correctness, "http", "correctnessSummary.correctness")
    direct = _required_mapping(
        correctness, "directKinesis", "correctnessSummary.correctness"
    )
    counts = _required_mapping(
        correctness, "counts", "correctnessSummary.correctness"
    )
    correctness_exact = (
        _required_boolean(correctness, "passed", "correctnessSummary.correctness")
        and _required_nonnegative_integer(
            correctness, "inputRecords", "correctnessSummary.correctness"
        ) == 1_002
        and {
            key: _required_nonnegative_integer(
                http, key, "correctnessSummary.correctness.http"
            )
            for key in ("http202", "non202", "total")
        } == {"http202": 1_000, "non202": 0, "total": 1_000}
        and _required_nonnegative_integer(
            direct, "accepted", "correctnessSummary.correctness.directKinesis"
        ) == 2
        and _required_nonnegative_integer(
            direct, "failed", "correctnessSummary.correctness.directKinesis"
        ) == 0
        and _required_nonnegative_integer(
            counts, "final", "correctnessSummary.correctness.counts"
        ) == 1_000
        and _required_nonnegative_integer(
            counts, "unique", "correctnessSummary.correctness.counts"
        ) == 1_000
        and _required_nonnegative_integer(
            counts, "physical", "correctnessSummary.correctness.counts"
        ) == 1_000
        and _required_nonnegative_integer(
            counts, "raw", "correctnessSummary.correctness.counts"
        ) == 1
        and _required_nonnegative_number(
            correctness, "lateEventDropped", "correctnessSummary.correctness"
        ) >= 1
    )
    replacement_counts = _required_mapping(
        replacement, "counts", "correctnessSummary.replacement"
    )
    baseline_tasks = replacement.get("baselineTasks")
    current_tasks = replacement.get("currentTasks")
    stopped_task = replacement.get("stoppedTask")
    replacement_exact = (
        _required_boolean(replacement, "passed", "correctnessSummary.replacement")
        and _required_nonnegative_integer(
            replacement, "offered", "correctnessSummary.replacement"
        ) == 900
        and _required_nonnegative_integer(
            replacement, "accepted", "correctnessSummary.replacement"
        ) == 900
        and _required_nonnegative_integer(
            replacement_counts, "final", "correctnessSummary.replacement.counts"
        ) == 900
        and _required_nonnegative_integer(
            replacement_counts, "unique", "correctnessSummary.replacement.counts"
        ) == 900
        and _required_nonnegative_integer(
            replacement_counts, "physical", "correctnessSummary.replacement.counts"
        ) >= 900
        and isinstance(baseline_tasks, list)
        and isinstance(current_tasks, list)
        and len(set(baseline_tasks)) == 2
        and len(set(current_tasks)) == 2
        and len(set(baseline_tasks) | set(current_tasks)) == 3
        and stopped_task in set(baseline_tasks)
        and stopped_task not in set(current_tasks)
    )
    if correctness_document.get("passed") != (correctness_exact and replacement_exact):
        raise EvidenceAssemblyError(
            "correctnessSummary.passed disagrees with exact correctness/replacement evidence"
        )

    generator = _required_mapping(seed, "generatorContract", "seedSummary")
    fingerprints = seed.get("fingerprintSamples")
    try:
        partition = date.fromisoformat(_required_string(seed, "partition", "seedSummary"))
        today = date.fromisoformat(_required_string(seed, "today", "seedSummary"))
    except ValueError as error:
        raise EvidenceAssemblyError("seedSummary partition/today must be ISO dates") from error
    seed_exact = (
        _required_boolean(seed, "stable", "seedSummary")
        and _required_nonnegative_integer(seed, "rows", "seedSummary") == 15_000_000
        and partition == today - timedelta(days=8)
        and generator.get("partition") == partition.isoformat()
        and generator.get("rows") == 15_000_000
        and generator.get("runId") == run.get("runId")
        and isinstance(fingerprints, list)
        and len(fingerprints) == 2
        and fingerprints[0] == fingerprints[1]
        and isinstance(fingerprints[0], dict)
        and fingerprints[0].get("rows") == 15_000_000
        and fingerprints[0].get("uniqueEvents") == 15_000_000
    )

    if warmup.get("stage") != "warmup":
        raise EvidenceAssemblyError("warmupStageSummary.stage must be warmup")
    warmup_aggregate = _required_mapping(
        warmup, "aggregate", "warmupStageSummary"
    )
    warmup_accounting = _required_mapping(
        warmup, "accounting", "warmupStageSummary"
    )
    warmup_http202 = _required_nonnegative_integer(
        warmup_aggregate, "http202", "warmupStageSummary.aggregate"
    )
    warmup_exact = (
        warmup.get("diagnosticContinuationAllowed") is True
        and warmup.get("archive") is None
        and _required_positive_integer(
            warmup_aggregate, "durationSeconds", "warmupStageSummary.aggregate"
        ) == 180
        and warmup_http202 > 0
        and _required_nonnegative_integer(
            warmup_aggregate, "completedRequests", "warmupStageSummary.aggregate"
        ) == warmup_http202
        and all(
            _required_nonnegative_integer(
                warmup_accounting, field, "warmupStageSummary.accounting"
            ) == warmup_http202
            for field in ("http202", "kinesisAccepted", "kclProcessed", "clickHouseInserted")
        )
    )
    runner = _runner_execution_summary(run, control_evidence)
    return {
        "deploymentVerified": deployment_exact,
        "correctness1002Passed": correctness_exact,
        "consumerReplacement900Passed": replacement_exact,
        "closedPartitionSeed15MPassed": seed_exact,
        "warmup180FullyAccounted": warmup_exact,
        **runner,
    }


def _runner_execution_summary(
    run: Mapping[str, Any], control_evidence: Mapping[str, Any]
) -> dict[str, bool]:
    attempts = run.get("stageAttempts")
    attempted_stages = run.get("attemptedStages")
    completed_stages = run.get("completedStages")
    if (
        not isinstance(attempts, list)
        or not all(isinstance(item, dict) for item in attempts)
        or not isinstance(attempted_stages, list)
        or not all(isinstance(item, str) for item in attempted_stages)
        or not isinstance(completed_stages, list)
        or not all(isinstance(item, str) for item in completed_stages)
    ):
        raise EvidenceAssemblyError("runState stage execution arrays are invalid")
    attempt_stages = [str(item.get("stage", "")) for item in attempts]
    ordinals = [item.get("ordinal") for item in attempts]
    stage_counts: dict[str, int] = {}
    attempt_numbers_valid = True
    command_hashes_valid = True
    for item in attempts:
        stage = str(item.get("stage", ""))
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        attempt_numbers_valid = (
            attempt_numbers_valid and item.get("attempt") == stage_counts[stage]
        )
        command_hashes_valid = command_hashes_valid and (
            isinstance(item.get("commandSha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", str(item.get("commandSha256")))
            is not None
        )
    tail = attempt_stages[len(PRE_CLEANUP_STAGE_ORDER):-1]
    tail_is_pairs = (
        bool(tail)
        and len(tail) % 2 == 0
        and 1 <= len(tail) // 2 <= 3
        and all(
            tail[index:index + 2] == ["cleanup", "inventory"]
            for index in range(0, len(tail), 2)
        )
    )
    sequence_valid = (
        attempt_stages[:len(PRE_CLEANUP_STAGE_ORDER)] == list(PRE_CLEANUP_STAGE_ORDER)
        and attempt_stages[-1:] == ["evaluate"]
        and tail_is_pairs
        and attempted_stages == attempt_stages
        and ordinals == list(range(1, len(attempts) + 1))
        and attempt_numbers_valid
        and command_hashes_valid
        and completed_stages == [*PRE_CLEANUP_STAGE_ORDER, "cleanup", "inventory"]
    )
    pre_attempts = attempts[:len(PRE_CLEANUP_STAGE_ORDER)]
    pre_passed = all(
        item.get("status") == "finished" and item.get("exitCode") == 0
        for item in pre_attempts
    )
    cleanup_attempts = [item for item in attempts if item.get("stage") == "cleanup"]
    inventory_attempts = [item for item in attempts if item.get("stage") == "inventory"]
    if (
        len(pre_attempts) != len(PRE_CLEANUP_STAGE_ORDER)
        or not cleanup_attempts
        or not inventory_attempts
        or not attempts
    ):
        raise EvidenceAssemblyError("runState does not contain a complete finalization path")
    final_cleanup_zero_path = (
        bool(cleanup_attempts)
        and len(cleanup_attempts) == len(inventory_attempts)
        and cleanup_attempts[-1].get("status") == "finished"
        and cleanup_attempts[-1].get("exitCode") == 0
        and inventory_attempts[-1].get("status") == "finished"
        and inventory_attempts[-1].get("exitCode") == 0
    )
    evaluate_attempt = attempts[-1] if attempts else {}
    in_progress = run.get("inProgressStage")
    evaluate_in_progress = (
        isinstance(in_progress, dict)
        and in_progress.get("stage") == "evaluate"
        and in_progress.get("ordinal") == evaluate_attempt.get("ordinal")
        and evaluate_attempt.get("status") == "in-progress"
        and evaluate_attempt.get("exitCode") is None
    )
    one_shot_exact = all(
        attempt_stages.count(stage) == 1
        for stage in ("deploy", "warmup", "score_archive")
    )
    no_hard_stop = (
        run.get("failedStage") is None
        and run.get("cleanupOnly") is False
        and run.get("failureDisposition") is None
        and all(item.get("failureDisposition") is None for item in attempts)
    )
    try:
        paid_started = parse_utc(_required_string(run, "paidStartedAt", "runState"))
        cleanup_deadline = parse_utc(
            _required_string(run, "cleanupStartDeadline", "runState")
        )
        hard_deadline = parse_utc(
            _required_string(run, "hardDeadline", "runState")
        )
        pre_finished = max(parse_utc(str(item["finishedAt"])) for item in pre_attempts)
        cleanup_started = parse_utc(str(cleanup_attempts[0]["startedAt"]))
        evaluate_started = parse_utc(str(evaluate_attempt["startedAt"]))
        timeline = [
            (
                parse_utc(str(item["startedAt"])),
                parse_utc(str(item["finishedAt"])),
            )
            for item in attempts[:-1]
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise EvidenceAssemblyError("runState deadline evidence is invalid") from error
    timeline_valid = (
        bool(timeline)
        and all(start <= finish for start, finish in timeline)
        and all(
            before[1] <= after[0]
            for before, after in zip(timeline, timeline[1:])
        )
        and timeline[-1][1] <= evaluate_started
        and paid_started <= timeline[0][0]
    )
    deadline_shape = (
        cleanup_deadline == paid_started + timedelta(minutes=160)
        and hard_deadline == paid_started + timedelta(minutes=180)
    )
    return {
        "runnerSequenceComplete": sequence_valid and pre_passed and evaluate_in_progress and timeline_valid,
        "singleDeployWarmupScoreArchive": one_shot_exact,
        "noRecordedHardStop": no_hard_stop,
        "cleanupStartedByMinute160": deadline_shape and pre_finished <= cleanup_deadline and cleanup_started <= cleanup_deadline,
        "hardDeadlineMet": deadline_shape and evaluate_started <= hard_deadline,
        "cleanupAndInventorySucceeded": final_cleanup_zero_path,
        "commandSetSealed": control_evidence.get("verified") is True
        and control_evidence.get("commandSetSha256") == run.get("commandSetSha256"),
    }


def _validate_counts(counts: Mapping[str, Any], identity_mode: str) -> None:
    common = ("http202", "collectorFinalAck", "kinesisAccepted", "clickHouseLiveUnique")
    diagnostic = ("kclProcessed", "clickHouseInserted", "fixturePoolRows")
    globally_unique = ("clickHouseAccounted",)
    fields = common + (diagnostic if identity_mode == "balanced-pool-sampled-with-replacement" else globally_unique)
    for field in fields:
        _required_nonnegative_integer(counts, field, "drainAccounting.counts")


def _validate_drain(drain: Mapping[str, Any]) -> None:
    for field in ("seconds", "visibilityP50Ms", "visibilityP95Ms", "visibilityP99Ms"):
        _required_nonnegative_number(drain, field, "drainAccounting.drain")
    if not isinstance(drain.get("iteratorAgeProgressed"), bool):
        raise EvidenceAssemblyError(
            "drainAccounting.drain.iteratorAgeProgressed must be boolean"
        )


def _validate_archive(archive: Mapping[str, Any]) -> None:
    for field in (
        "rows",
        "objects",
        "preDropSourceMinusArchive",
        "preDropArchiveMinusSource",
        "committedSourceMinusArchive",
        "committedArchiveMinusSource",
        "postDropReferenceMinusArchive",
        "postDropArchiveMinusReference",
        "sourceRowsAfterDrop",
        "liveRowsAfterDrop",
    ):
        _required_nonnegative_integer(archive, field, "archiveValidation")
    object_rows = archive.get("objectRows")
    if (
        not isinstance(object_rows, list)
        or not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in object_rows)
    ):
        raise EvidenceAssemblyError("archiveValidation.objectRows must be integer array")
    for field in ("committedReRead", "overlappedScoreWindow"):
        if not isinstance(archive.get(field), bool):
            raise EvidenceAssemblyError(f"archiveValidation.{field} must be boolean")
    _required_nonnegative_number(archive, "cycleSeconds", "archiveValidation")


def _validate_observability(
    resources: Mapping[str, Any],
    haproxy: Mapping[str, Any],
    failures: Mapping[str, Any],
    cloudtrail: Mapping[str, Any],
) -> None:
    for role in ("collector", "consumer", "clickHouse"):
        metrics = _required_mapping(resources, role, "observabilitySummary.resources")
        for metric in ("cpuP95Percent", "memoryP95Percent"):
            _required_nonnegative_number(
                metrics,
                metric,
                f"observabilitySummary.resources.{role}",
            )
        hosts = _required_mapping(
            metrics, "hosts", f"observabilitySummary.resources.{role}"
        )
        if len(hosts) != EXPECTED_OBSERVABILITY_HOSTS[role]:
            raise EvidenceAssemblyError(
                f"observabilitySummary.resources.{role}.hosts cardinality is invalid"
            )
        host_cpu: list[float] = []
        host_memory: list[float] = []
        for instance_id, host in hosts.items():
            if re.fullmatch(r"i-[0-9a-f]+", str(instance_id)) is None or not isinstance(host, dict):
                raise EvidenceAssemblyError(
                    f"observabilitySummary.resources.{role}.hosts is malformed"
                )
            if _required_positive_integer(host, "sampleCount", f"observabilitySummary.resources.{role}.hosts.{instance_id}") < 50:
                raise EvidenceAssemblyError(
                    f"observabilitySummary.resources.{role}.hosts.{instance_id} is incomplete"
                )
            host_cpu.append(_required_nonnegative_number(
                host, "cpuP95Percent", f"observabilitySummary.resources.{role}.hosts.{instance_id}"
            ))
            host_memory.append(_required_nonnegative_number(
                host, "memoryP95Percent", f"observabilitySummary.resources.{role}.hosts.{instance_id}"
            ))
            _required_nonnegative_number(
                host, "filesystemPeakPercent", f"observabilitySummary.resources.{role}.hosts.{instance_id}"
            )
        if not math.isclose(float(metrics["cpuP95Percent"]), max(host_cpu), abs_tol=1e-9) or not math.isclose(
            float(metrics["memoryP95Percent"]), max(host_memory), abs_tol=1e-9
        ):
            raise EvidenceAssemblyError(
                f"observabilitySummary.resources.{role} must use the worst per-host p95"
            )
    clickhouse = _required_mapping(
        resources, "clickHouse", "observabilitySummary.resources"
    )
    _required_nonnegative_number(
        clickhouse,
        "filesystemPeakPercent",
        "observabilitySummary.resources.clickHouse",
    )
    config_sha256 = _required_string(
        haproxy, "configSha256", "observabilitySummary.haproxy"
    )
    if len(config_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in config_sha256
    ):
        raise EvidenceAssemblyError(
            "observabilitySummary.haproxy.configSha256 must be lowercase SHA-256"
        )
    for field in ("activeBackends", "http4xx", "http5xx"):
        _required_nonnegative_integer(
            haproxy, field, "observabilitySummary.haproxy"
        )
    _required_nonnegative_number(
        haproxy, "maxQueue", "observabilitySummary.haproxy"
    )
    if not isinstance(haproxy.get("prometheusCollected"), bool):
        raise EvidenceAssemblyError(
            "observabilitySummary.haproxy.prometheusCollected must be boolean"
        )
    for field in FAILURE_FIELDS:
        _required_nonnegative_integer(
            failures, field, "observabilitySummary.failures"
        )
    if not isinstance(cloudtrail.get("collected"), bool):
        raise EvidenceAssemblyError(
            "observabilitySummary.cloudTrail.collected must be boolean"
        )
    for field in (
        "deployAttempts",
        "warmupAttempts",
        "scoreAttempts",
        "archiveAttempts",
    ):
        _required_nonnegative_integer(
            cloudtrail, field, "observabilitySummary.cloudTrail"
        )
    source_paths = cloudtrail.get("sourcePaths")
    source_digests = cloudtrail.get("sha256")
    if (
        not isinstance(source_paths, list)
        or not source_paths
        or not all(
            isinstance(path, str)
            and path
            and not Path(path).is_absolute()
            and ".." not in Path(path).parts
            for path in source_paths
        )
        or len(set(source_paths)) != len(source_paths)
    ):
        raise EvidenceAssemblyError(
            "observabilitySummary.cloudTrail.sourcePaths must be nonempty safe relative paths"
        )
    if (
        not isinstance(source_digests, list)
        or len(source_digests) != len(source_paths)
        or not all(
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            for digest in source_digests
        )
    ):
        raise EvidenceAssemblyError(
            "observabilitySummary.cloudTrail.sha256 must bind every source path"
        )


def _validate_observability_raw_source(
    root: Path,
    observability: Mapping[str, Any],
    run_id: str,
    session_id: str,
    resources: Mapping[str, Any],
    failures: Mapping[str, Any],
    drain_document: Mapping[str, Any],
    deployment: Mapping[str, Any],
) -> None:
    metadata = _required_mapping(observability, "rawEvidence", "observabilitySummary")
    if set(metadata) != {"path", "sha256"} or metadata.get("path") != "evidence/score-observability/after.json":
        raise EvidenceAssemblyError("observabilitySummary.rawEvidence path contract mismatch")
    digest = metadata.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise EvidenceAssemblyError("observabilitySummary.rawEvidence SHA-256 is invalid")
    candidate = root / str(metadata["path"])
    if candidate.is_symlink():
        raise EvidenceAssemblyError("observability raw evidence must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise EvidenceAssemblyError("observability raw evidence is missing or out of run") from error
    raw = resolved.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise EvidenceAssemblyError("observability raw evidence digest mismatch")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceAssemblyError("observability raw evidence is invalid JSON") from error
    if (
        not isinstance(document, dict)
        or document.get("schemaVersion") != 1
        or document.get("runId") != run_id
        or document.get("sessionId") != session_id
        or not isinstance(document.get("hostTelemetry"), dict)
    ):
        raise EvidenceAssemblyError("observability raw evidence binding is invalid")
    calculated_resources = _resources_from_raw_telemetry(document["hostTelemetry"])
    if dict(resources) != calculated_resources:
        raise EvidenceAssemblyError(
            "observability resource summary differs from raw per-host telemetry"
        )
    calculated_failures = _failures_from_raw_evidence(
        document, drain_document, deployment
    )
    if any(failures.get(field) != calculated_failures[field] for field in FAILURE_FIELDS):
        raise EvidenceAssemblyError(
            "observability failure summary differs from raw query evidence"
        )


def _resources_from_raw_telemetry(
    telemetry: Mapping[str, Any],
) -> dict[str, Any]:
    if set(telemetry) != set(EXPECTED_OBSERVABILITY_HOSTS):
        raise EvidenceAssemblyError("raw host telemetry roles are incomplete")
    result: dict[str, Any] = {}
    for role, expected_count in EXPECTED_OBSERVABILITY_HOSTS.items():
        hosts = telemetry.get(role)
        if not isinstance(hosts, dict) or len(hosts) != expected_count:
            raise EvidenceAssemblyError(f"raw host telemetry count mismatch for {role}")
        per_host: dict[str, dict[str, Any]] = {}
        for instance_id, text in hosts.items():
            if (
                re.fullmatch(r"i-[0-9a-f]+", str(instance_id)) is None
                or not isinstance(text, str)
            ):
                raise EvidenceAssemblyError(f"raw host telemetry is malformed for {role}")
            parsed = _parse_raw_host_telemetry(text)
            if parsed["sampleCount"] < 50:
                raise EvidenceAssemblyError(f"raw host telemetry is incomplete for {instance_id}")
            per_host[str(instance_id)] = {
                "sampleCount": parsed["sampleCount"],
                "cpuP95Percent": _nearest_rank(parsed["cpuPercent"], 0.95),
                "memoryP95Percent": _nearest_rank(parsed["memoryPercent"], 0.95),
                "filesystemPeakPercent": max(parsed["filesystemPercent"]),
            }
        role_result: dict[str, Any] = {
            "cpuP95Percent": max(item["cpuP95Percent"] for item in per_host.values()),
            "memoryP95Percent": max(
                item["memoryP95Percent"] for item in per_host.values()
            ),
            "sampleCount": sum(item["sampleCount"] for item in per_host.values()),
            "hosts": per_host,
        }
        if role == "clickHouse":
            role_result["filesystemPeakPercent"] = max(
                item["filesystemPeakPercent"] for item in per_host.values()
            )
        result[role] = role_result
    return result


def _parse_raw_host_telemetry(text: str) -> dict[str, Any]:
    rows: list[list[int]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 13:
            raise EvidenceAssemblyError("raw host telemetry row has an invalid field count")
        try:
            values = [int(value) for value in fields]
        except ValueError as error:
            raise EvidenceAssemblyError("raw host telemetry contains a non-integer") from error
        if any(value < 0 for value in values):
            raise EvidenceAssemblyError("raw host telemetry values must be nonnegative")
        rows.append(values)
    if len(rows) < 2:
        raise EvidenceAssemblyError("raw host telemetry needs at least two samples")
    cpu: list[float] = []
    for before, after in zip(rows, rows[1:]):
        before_total = sum(before[1:9])
        after_total = sum(after[1:9])
        total_delta = after_total - before_total
        idle_delta = (after[4] + after[5]) - (before[4] + before[5])
        if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
            raise EvidenceAssemblyError("raw host CPU counters are not monotonic")
        cpu.append((total_delta - idle_delta) * 100.0 / total_delta)
    memory = [
        (row[9] - row[10]) * 100.0 / row[9]
        for row in rows
        if row[9] > 0 and 0 <= row[10] <= row[9]
    ]
    filesystem = [
        row[12] * 100.0 / row[11]
        for row in rows
        if row[11] > 0 and row[12] <= row[11]
    ]
    if len(memory) != len(rows) or len(filesystem) != len(rows):
        raise EvidenceAssemblyError("raw host memory or filesystem telemetry is invalid")
    return {
        "sampleCount": len(rows),
        "cpuPercent": cpu,
        "memoryPercent": memory,
        "filesystemPercent": filesystem,
    }


def _nearest_rank(values: list[float], quantile: float) -> float:
    if not values:
        raise EvidenceAssemblyError("raw percentile input is empty")
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return round(ordered[index], 6)


def _collector_delta_from_raw(document: Mapping[str, Any]) -> dict[str, int]:
    before = document.get("collectorBefore")
    after = document.get("collectorAfter")
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or set(before) != set(after)
        or len(before) != EXPECTED_OBSERVABILITY_HOSTS["collector"]
    ):
        raise EvidenceAssemblyError("raw collector snapshot host sets differ")
    fields = ("successes", "failures", "retries", "partial_failures", "timeouts")
    totals = {field: 0 for field in fields}
    for instance_id in before:
        old = before[instance_id].get("kinesis", {}).get("put_records", {})
        new = after[instance_id].get("kinesis", {}).get("put_records", {})
        for field in fields:
            try:
                delta = int(new.get(field, -1)) - int(old.get(field, -1))
            except (AttributeError, TypeError, ValueError) as error:
                raise EvidenceAssemblyError("raw collector counters are malformed") from error
            if delta < 0:
                raise EvidenceAssemblyError(
                    f"raw collector counter reset during score: {instance_id}/{field}"
                )
            totals[field] += delta
    return totals


def _service_task_arns(raw: Any, context: str) -> list[str]:
    if not isinstance(raw, dict):
        raise EvidenceAssemblyError(f"{context} must be an object")
    arns: list[str] = []
    for role in raw.values():
        if not isinstance(role, dict) or not isinstance(role.get("tasks"), list):
            raise EvidenceAssemblyError(f"{context} task snapshots are malformed")
        for task in role["tasks"]:
            if not isinstance(task, dict) or not isinstance(task.get("taskArn"), str):
                raise EvidenceAssemblyError(f"{context} task snapshot is malformed")
            arns.append(task["taskArn"])
    return sorted(set(arns))


def _failures_from_raw_evidence(
    document: Mapping[str, Any],
    drain_document: Mapping[str, Any],
    deployment: Mapping[str, Any],
) -> dict[str, int]:
    evidence = _required_mapping(document, "failureEvidence", "observabilityRaw")
    if evidence.get("schemaVersion") != 1:
        raise EvidenceAssemblyError("raw failure evidence schemaVersion must be 1")

    metric = _required_mapping(
        evidence, "kinesisThrottleMetric", "observabilityRaw.failureEvidence"
    )
    datapoints = metric.get("datapoints")
    deployment_stream = _required_mapping(
        deployment, "stream", "deploymentVerification"
    )
    if (
        metric.get("namespace") != "AWS/Kinesis"
        or metric.get("metricName") != "WriteProvisionedThroughputExceeded"
        or metric.get("periodSeconds") != 60
        or metric.get("statistic") != "Sum"
        or metric.get("dimensions")
        != [{"Name": "StreamName", "Value": deployment_stream.get("name")}]
        or not isinstance(datapoints, list)
    ):
        raise EvidenceAssemblyError("raw Kinesis throttle metric contract is invalid")
    metric_sum = 0.0
    for point in datapoints:
        if not isinstance(point, dict) or not isinstance(point.get("timestamp"), str):
            raise EvidenceAssemblyError("raw Kinesis throttle datapoint is malformed")
        try:
            parse_utc(point["timestamp"])
            value = float(point.get("sum"))
        except (TypeError, ValueError) as error:
            raise EvidenceAssemblyError("raw Kinesis throttle datapoint is invalid") from error
        if not math.isfinite(value) or value < 0:
            raise EvidenceAssemblyError("raw Kinesis throttle datapoint must be nonnegative")
        metric_sum += value
    if not math.isclose(
        _required_nonnegative_number(metric, "sum", "raw Kinesis throttle metric"),
        metric_sum,
        abs_tol=1e-9,
    ):
        raise EvidenceAssemblyError("raw Kinesis throttle metric sum is inconsistent")

    failure_objects = _required_mapping(
        evidence, "failureObjects", "observabilityRaw.failureEvidence"
    )
    keys = failure_objects.get("keys")
    if (
        not isinstance(failure_objects.get("bucket"), str)
        or not failure_objects.get("bucket")
        or failure_objects.get("prefix")
        != f"failures/{drain_document.get('runId')}/"
        or not isinstance(keys, list)
        or keys != sorted(set(keys))
        or not all(isinstance(key, str) and key for key in keys)
        or not all(key.startswith(str(failure_objects.get("prefix"))) for key in keys)
    ):
        raise EvidenceAssemblyError("raw failure-object query result is invalid")

    log_query = _required_mapping(
        evidence, "clickHouseInsertErrorQuery", "observabilityRaw.failureEvidence"
    )
    results = log_query.get("results")
    if (
        log_query.get("status") != "Complete"
        or log_query.get("logGroup")
        != f"/loopad/perf/phase7/{drain_document.get('runId')}/ConsumerLogs"
        or not isinstance(results, list)
        or len(results) != 1
    ):
        raise EvidenceAssemblyError("raw ClickHouse error query result is invalid")
    try:
        fields = {item["field"]: item["value"] for item in results[0]}
        clickhouse_errors = int(float(fields["count"]))
    except (KeyError, TypeError, ValueError) as error:
        raise EvidenceAssemblyError("raw ClickHouse error count is invalid") from error
    if clickhouse_errors < 0 or log_query.get("count") != clickhouse_errors:
        raise EvidenceAssemblyError("raw ClickHouse error count is inconsistent")

    stopped = _required_mapping(
        evidence, "stoppedTaskQuery", "observabilityRaw.failureEvidence"
    )
    tasks = stopped.get("tasks")
    if not isinstance(tasks, list):
        raise EvidenceAssemblyError("raw stopped-task query result is invalid")
    oom_count = 0
    for task in tasks:
        if (
            not isinstance(task, dict)
            or not isinstance(task.get("taskArn"), str)
            or not task.get("taskArn")
            or not isinstance(task.get("stoppedAt"), str)
            or not isinstance(task.get("containerReasons"), list)
            or not all(isinstance(value, str) for value in task["containerReasons"])
            or not isinstance(task.get("oom"), bool)
        ):
            raise EvidenceAssemblyError("raw stopped-task entry is malformed")
        try:
            parse_utc(task["stoppedAt"])
        except (TypeError, ValueError) as error:
            raise EvidenceAssemblyError("raw stopped-task timestamp is invalid") from error
        text = " ".join(
            [str(task.get("stoppedReason", "")), *task["containerReasons"]]
        )
        calculated_oom = re.search(r"out.?of.?memory|oom", text, re.IGNORECASE) is not None
        if task["oom"] is not calculated_oom:
            raise EvidenceAssemblyError("raw stopped-task OOM classification is inconsistent")
        oom_count += int(calculated_oom)
    if stopped.get("oomCount") != oom_count:
        raise EvidenceAssemblyError("raw stopped-task OOM count is inconsistent")

    collector = _collector_delta_from_raw(document)
    if evidence.get("collectorDelta") != collector:
        raise EvidenceAssemblyError("raw collector delta differs from source snapshots")
    drain_counts = _required_mapping(
        drain_document, "counts", "drainAccounting"
    )
    if (
        collector["successes"] != drain_counts.get("http202")
        or collector["successes"] != drain_counts.get("collectorFinalAck")
    ):
        raise EvidenceAssemblyError("raw collector ACK delta differs from drain accounting")
    before_arns = _service_task_arns(document.get("servicesBefore"), "servicesBefore")
    after_arns = _service_task_arns(document.get("servicesAfter"), "servicesAfter")
    if (
        evidence.get("serviceTaskArnsBefore") != before_arns
        or evidence.get("serviceTaskArnsAfter") != after_arns
    ):
        raise EvidenceAssemblyError("raw service task sets differ from source snapshots")

    drain_failures = _required_mapping(
        drain_document, "failures", "drainAccounting"
    )
    kcl_terminal = _coerce_nonnegative_integer(
        drain_failures.get("terminalFailure"),
        "drainAccounting.failures.terminalFailure",
    )
    archive = _required_mapping(drain_document, "archive", "drainAccounting")
    worker = _required_mapping(archive, "workerResult", "drainAccounting.archive")
    archive_status = worker.get("status")
    if (
        evidence.get("kclTerminalFailure") != kcl_terminal
        or evidence.get("archiveWorkerStatus") != archive_status
    ):
        raise EvidenceAssemblyError("raw failure inputs differ from drain/archive evidence")

    return {
        "kinesisThrottle": int(round(metric_sum)),
        "collectorFinalFailure": (
            collector["failures"]
            + collector["partial_failures"]
            + collector["timeouts"]
        ),
        "kclTerminalFailure": kcl_terminal,
        "failureObjects": len(keys),
        "clickHouseInsertErrors": clickhouse_errors,
        "archiveFailures": 0 if archive_status == "passed" else 1,
        "unexpectedRestarts": len(set(before_arns).symmetric_difference(after_arns)),
        "oomKills": oom_count,
    }


def _validate_cloudtrail_source_files(
    root: Path,
    cloudtrail: Mapping[str, Any],
    run_id: str,
    session_id: str,
    archive_started_by: str,
) -> None:
    source_paths = cloudtrail["sourcePaths"]
    source_digests = cloudtrail["sha256"]
    expected_paths = [
        "evidence/cloudtrail/deploy.json",
        "evidence/cloudtrail/warmup.json",
        "evidence/cloudtrail/score.json",
        "evidence/cloudtrail/archive.json",
    ]
    if source_paths != expected_paths:
        raise EvidenceAssemblyError(
            "CloudTrail sources must be the exact deploy/warmup/score/archive documents"
        )
    recorded_commands = {
        stage: _load_recorded_stage_commands(root, run_id, stage)
        for stage in ("warmup", "score")
    }
    all_event_ids: set[str] = set()
    all_command_ids: set[str] = set()
    for relative, expected_digest in zip(source_paths, source_digests, strict=True):
        candidate = root / relative
        if candidate.is_symlink():
            raise EvidenceAssemblyError(
                f"CloudTrail source must not be a symbolic link: {relative}"
            )
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, ValueError) as error:
            raise EvidenceAssemblyError(
                f"missing or out-of-run CloudTrail source: {relative}"
            ) from error
        if not resolved.is_file():
            raise EvidenceAssemblyError(f"CloudTrail source is not a file: {relative}")
        raw = resolved.read_bytes()
        actual_digest = hashlib.sha256(raw).hexdigest()
        if actual_digest != expected_digest:
            raise EvidenceAssemblyError(
                f"CloudTrail source digest mismatch: {relative}"
            )
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvidenceAssemblyError(
                f"CloudTrail source is not valid JSON: {relative}"
            ) from error
        if (
            not isinstance(document, dict)
            or document.get("schemaVersion") != 1
            or document.get("runId") != run_id
            or document.get("sessionId") != session_id
            or not isinstance(document.get("events"), list)
        ):
            raise EvidenceAssemblyError(
                f"CloudTrail source binding is invalid: {relative}"
            )
        events = document["events"]
        name = Path(relative).stem
        expected_count = 16 if name in {"warmup", "score"} else 1
        expected_event_name = {
            "deploy": "CreateChangeSet",
            "warmup": "SendCommand",
            "score": "SendCommand",
            "archive": "RunTask",
        }[name]
        if len(events) != expected_count or not all(isinstance(event, dict) for event in events):
            raise EvidenceAssemblyError(
                f"CloudTrail {name} must contain exactly {expected_count} events"
            )
        observed_commands: dict[str, dict[str, str]] = {}
        for event in events:
            event_id = event.get("eventId")
            event_time = event.get("eventTime")
            if (
                not isinstance(event_id, str)
                or not event_id
                or event_id in all_event_ids
                or event.get("eventName") != expected_event_name
                or event.get("principalArn") != EXPECTED_OPERATOR_ARN
                or not isinstance(event_time, str)
            ):
                raise EvidenceAssemblyError(
                    f"CloudTrail {name} event identity/principal is invalid"
                )
            try:
                parse_utc(event_time)
            except (TypeError, ValueError) as error:
                raise EvidenceAssemblyError(
                    f"CloudTrail {name} event timestamp is invalid"
                ) from error
            all_event_ids.add(event_id)
            request = event.get("request")
            response = event.get("response")
            if not isinstance(request, dict) or not isinstance(response, dict):
                raise EvidenceAssemblyError(
                    f"CloudTrail {name} request/response evidence is invalid"
                )
            if name == "deploy" and (
                request.get("stackName") != RUNTIME_STACK_NAME
                or not isinstance(request.get("tags"), dict)
                or request["tags"].get("RunId") != run_id
                or request["tags"].get("SessionId") != session_id
            ):
                raise EvidenceAssemblyError(
                    "CloudTrail deploy event does not bind the exact runtime stack and tags"
                )
            if name in {"warmup", "score"}:
                command_id = response.get("commandId")
                comment = request.get("comment")
                instance_ids = request.get("instanceIds")
                if (
                    not isinstance(command_id, str)
                    or re.fullmatch(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        command_id,
                    ) is None
                    or command_id in all_command_ids
                    or not isinstance(comment, str)
                    or not comment
                    or request.get("documentName") != "AWS-RunShellScript"
                    or not isinstance(instance_ids, list)
                    or len(instance_ids) != 1
                    or not isinstance(instance_ids[0], str)
                ):
                    raise EvidenceAssemblyError(
                        f"CloudTrail {name} command cardinality is invalid"
                    )
                all_command_ids.add(command_id)
                observed_commands[command_id] = {
                    "comment": comment,
                    "instanceId": instance_ids[0],
                }
            if name == "archive" and (
                request.get("startedBy") != archive_started_by
                or not isinstance(request.get("taskDefinition"), str)
                or not request.get("taskDefinition")
                or not isinstance(request.get("cluster"), str)
                or not request.get("cluster")
            ):
                raise EvidenceAssemblyError(
                    "CloudTrail archive event does not bind exact startedBy/task/cluster"
                )
        if name in {"warmup", "score"} and observed_commands != recorded_commands[name]:
            raise EvidenceAssemblyError(
                f"CloudTrail {name} commands/comments differ from immutable node evidence"
            )


def _load_recorded_stage_commands(
    root: Path, run_id: str, stage: str
) -> dict[str, dict[str, str]]:
    match = re.fullmatch(
        r"run_(\d{8}_\d{6})_phase7_integration",
        run_id,
    )
    if match is None or stage not in {"warmup", "score"}:
        raise EvidenceAssemblyError("recorded SSM command identity is invalid")
    worker_run_id = f"run_{match.group(1)}_phase7_{stage}"
    duration_seconds = 180 if stage == "warmup" else 300
    expected_comments = {
        "ssm-command-started.json": (
            "phase7-oha-load-command",
            f"loop-ad {worker_run_id} oha 6250rps {duration_seconds}s",
        ),
        "ssm-transfer-probe-started.json": (
            "phase7-ssm-transfer-probe",
            f"loop-ad {worker_run_id} 20KiB SSM transfer probe",
        ),
    }
    stage_root = root / "evidence" / stage
    expected_paths = {
        stage_root / f"node-{node:02d}" / file_name
        for node in range(1, 9)
        for file_name in expected_comments
    }
    discovered_paths = {
        path
        for file_name in expected_comments
        for path in stage_root.glob(f"node-*/{file_name}")
    }
    if discovered_paths != expected_paths:
        raise EvidenceAssemblyError(
            f"{stage} must contain exactly 8 load and 8 transfer-probe command records"
        )

    result: dict[str, dict[str, str]] = {}
    instance_ids: set[str] = set()
    instances_by_node: dict[str, str] = {}
    for path in sorted(expected_paths):
        if path.is_symlink():
            raise EvidenceAssemblyError(
                f"recorded SSM command must not be a symbolic link: {path.relative_to(root)}"
            )
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            document = json.loads(resolved.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise EvidenceAssemblyError(
                f"recorded SSM command is missing or invalid: {path.relative_to(root)}"
            ) from error
        file_name = path.name
        expected_kind, expected_comment = expected_comments[file_name]
        node_id = path.parent.name
        command_id = document.get("commandId") if isinstance(document, dict) else None
        instance_id = document.get("instanceId") if isinstance(document, dict) else None
        comment = document.get("comment") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("schemaVersion") != 1
            or document.get("kind") != expected_kind
            or document.get("runId") != worker_run_id
            or document.get("stageLabel") != stage
            or document.get("nodeId") != node_id
            or not isinstance(instance_id, str)
            or re.fullmatch(r"i-[0-9a-f]+", instance_id) is None
            or not isinstance(command_id, str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                command_id,
            ) is None
            or comment != expected_comment
            or command_id in result
        ):
            raise EvidenceAssemblyError(
                f"recorded SSM command binding is invalid: {path.relative_to(root)}"
            )
        existing_instance = instances_by_node.setdefault(node_id, instance_id)
        if existing_instance != instance_id:
            raise EvidenceAssemblyError(
                f"{stage} load and transfer-probe commands target different instances"
            )
        if file_name == "ssm-command-started.json":
            if instance_id in instance_ids:
                raise EvidenceAssemblyError(
                    f"{stage} load commands do not cover eight unique instances"
                )
            instance_ids.add(instance_id)
        result[command_id] = {"comment": comment, "instanceId": instance_id}
    if len(result) != 16 or len(instance_ids) != 8:
        raise EvidenceAssemblyError(
            f"{stage} command evidence cardinality is invalid"
        )
    return result


def _validate_cost(cost: Mapping[str, Any]) -> None:
    for field in (
        "accruedUpperBoundUsd",
        "maximumIncludingCleanupUsd",
        "cleanupReserveUsd",
    ):
        value = _required_number(cost, field, "costStatus.cost")
        if value < 0:
            raise EvidenceAssemblyError(f"costStatus.cost.{field} must be nonnegative")


def _validate_cost_source(
    root: Path,
    cost_document: Mapping[str, Any],
    cost: Mapping[str, Any],
) -> None:
    model_path = root / "inputs" / "cost-model.json"
    if model_path.is_symlink():
        raise EvidenceAssemblyError("cost model must not be a symbolic link")
    try:
        resolved = model_path.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise EvidenceAssemblyError("cost model is missing or out of run") from error
    raw = resolved.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if cost_document.get("sourceCostModelSha256") != digest:
        raise EvidenceAssemblyError("costStatus source cost model digest mismatch")
    try:
        model = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceAssemblyError("cost model is invalid JSON") from error
    if not isinstance(model, dict) or model.get("passed") is not True:
        raise EvidenceAssemblyError("cost model did not pass")
    expected = {
        "accruedUpperBoundUsd": model.get("operationalMaximumUsd"),
        "maximumIncludingCleanupUsd": model.get("maximumIncludingCleanupUsd"),
        "cleanupReserveUsd": model.get("cleanupReserveUsd"),
    }
    try:
        values_match = all(
            math.isclose(float(cost.get(key)), float(value), abs_tol=1e-9)
            for key, value in expected.items()
        )
    except (TypeError, ValueError):
        values_match = False
    if not values_match:
        raise EvidenceAssemblyError("costStatus values differ from the frozen cost model")
    if cost.get("basis") != "full approved 180-minute operational maximum at collection time":
        raise EvidenceAssemblyError("costStatus basis is not the approved conservative bound")


def _validate_cleanup(cleanup: Mapping[str, Any]) -> None:
    all_zero = cleanup.get("allZero")
    if not isinstance(all_zero, bool):
        raise EvidenceAssemblyError("cleanupInventory.allZero must be boolean")
    counts = _required_mapping(cleanup, "counts", "cleanupInventory")
    resources = _required_mapping(cleanup, "resources", "cleanupInventory")
    if set(counts) != CLEANUP_SERVICE_CLASSES or set(resources) != CLEANUP_SERVICE_CLASSES:
        raise EvidenceAssemblyError(
            "cleanupInventory must contain the exact authoritative service classes"
        )
    for service_class, value in counts.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvidenceAssemblyError(
                f"cleanupInventory.counts.{service_class} must be a nonnegative integer"
            )
        owned = resources.get(service_class)
        if not isinstance(owned, list) or len(owned) != value:
            raise EvidenceAssemblyError(
                f"cleanupInventory resource count mismatch for {service_class}"
            )
    tagging_residuals = cleanup.get("taggingApiResiduals")
    if not isinstance(tagging_residuals, list) or not all(isinstance(item, str) and item for item in tagging_residuals):
        raise EvidenceAssemblyError("cleanupInventory.taggingApiResiduals must be a string array")
    service_zero = all(value == 0 for value in counts.values())
    tagging_zero = len(tagging_residuals) == 0
    if cleanup.get("serviceInventoryZero") is not service_zero or cleanup.get("taggingApiResidualsZero") is not tagging_zero:
        raise EvidenceAssemblyError("cleanupInventory zero summaries disagree with inventories")
    if cleanup.get("taggingApiAuthoritative") is not False:
        raise EvidenceAssemblyError("cleanupInventory must not treat the Tagging API as authoritative")
    if all_zero != (service_zero and tagging_zero):
        raise EvidenceAssemblyError(
            "cleanupInventory.allZero disagrees with service and tagging inventories"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--run-dir", required=True, type=Path)
    assemble.add_argument("--output", required=True, type=Path)
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--run-json", required=True, type=Path)
    finalize.add_argument("--evaluation", required=True, type=Path)
    finalize.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.action == "assemble":
        result = assemble_evidence(args.run_dir)
    else:
        try:
            run_document = json.loads(args.run_json.read_text(encoding="utf-8"))
            evaluation = json.loads(args.evaluation.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvidenceAssemblyError("finalizer input must be valid JSON") from error
        if not isinstance(run_document, dict) or not isinstance(evaluation, dict):
            raise EvidenceAssemblyError("finalizer inputs must be JSON objects")
        result = finalize_run_document(run_document, evaluation)
    write_json(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

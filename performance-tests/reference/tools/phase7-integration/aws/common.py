#!/usr/bin/env python3
"""Shared fail-closed helpers for the Phase 7-2 AWS attempt tooling."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


EXPECTED_ACCOUNT = "742711170910"
EXPECTED_REGION = "ap-northeast-2"
EXPECTED_OPERATOR_ARN = f"arn:aws:iam::{EXPECTED_ACCOUNT}:root"
RUNTIME_STACK_NAME = "LoopAdPerfPhase7IntegrationStack"
IMAGE_STACK_NAME = "LoopAdPerfPhase7IntegrationImageStack"
PHASE7_COLLECTOR_COMMIT = "497315137251af82d0d203ce34702d5543553942"
RUN_ID_PATTERN = re.compile(r"^run_(\d{8})_(\d{6})_phase7_integration$")
SESSION_ID_PATTERN = re.compile(r"^phase7-integration-(\d{8})T(\d{6})Z$")
OWNERSHIP_TAGS = {
    "Project": "loop-ad",
    "Phase": "7",
    "ResourceScope": "run",
    "ManagedBy": "codex",
}
SCOPED_BASELINE_PATH = (
    "performance-tests/run_20260719_041439_phase7_1_local_integration/"
    "local-handoff.json"
)
SCOPED_BASELINE_SHA256 = (
    "acfc85301a155a99e4351567f9aa618d16a7fe707adf2284b1a8b0290246674e"
)
SCOPED_POLICY_PATH = (
    "performance-tests/phase7_2-stabilization/"
    "full-stack-scoped-diagnostic-policy-20260719.json"
)
SCOPED_POLICY_SHA256 = (
    "5eb380a8890ae6113703adbd4f0843a628fce65605cfb3e77d3bbb0a8f560d9b"
)
SCOPED_PROMOTION_POLICY_PATH = (
    "performance-tests/phase7_2-stabilization/"
    "phase8-composite-promotion-policy-20260719.json"
)
SCOPED_PROMOTION_POLICY_SHA256 = (
    "b8d5fbaa00558b6a2ee97d27fc37f7d4c0839d2e198e6a0bf2fc58a4b1abf501"
)
SCOPED_FOCUSED_GATE_PATHS = {
    "performance-tests/phase7_2-stabilization/attempt-17-fix-verification.json",
    "performance-tests/phase7_2-stabilization/attempt-19-fix-verification.json",
    "performance-tests/phase7_2-stabilization/attempt-20-fix-verification.json",
    "performance-tests/phase7_2-stabilization/attempt-21-fix-verification.json",
    "performance-tests/phase7_2-stabilization/attempt-22-fix-verification.json",
    "performance-tests/phase7_2-stabilization/"
    "full-stack-scoped-tooling-verification-20260719.json",
}
SCOPED_STAGE_PLAN = [
    "deploy",
    "verify",
    "seed",
    "archive",
    "collect",
    "cleanup",
    "inventory",
]
SCOPED_ZERO_ATTEMPT_STAGES = [
    "correctness",
    "replacement",
    "warmup",
    "score",
    "source-drop",
]


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    observed: Any
    required: Any
    detail: str

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["pass"] = value.pop("passed")
        return value


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include an offset")
    return parsed.astimezone(UTC)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def validate_identifiers(run_id: str, session_id: str) -> None:
    run_match = RUN_ID_PATTERN.fullmatch(run_id)
    if not run_match:
        raise ValueError("run_id must match run_YYYYMMDD_HHMMSS_phase7_integration")
    session_match = SESSION_ID_PATTERN.fullmatch(session_id)
    if not session_match:
        raise ValueError("session_id must match phase7-integration-YYYYMMDDTHHMMSSZ")
    if run_match.groups() != session_match.groups():
        raise ValueError("run_id and session_id timestamps must match exactly")


def expected_tags(run_id: str, session_id: str) -> dict[str, str]:
    validate_identifiers(run_id, session_id)
    return {**OWNERSHIP_TAGS, "RunId": run_id, "SessionId": session_id}


def tag_map(tags: Iterable[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in tags:
        key = item.get("Key", item.get("key"))
        value = item.get("Value", item.get("value"))
        if key is not None and value is not None:
            result[str(key)] = str(value)
    return result


def tags_match(tags: dict[str, str], run_id: str, session_id: str) -> bool:
    return all(tags.get(key) == value for key, value in expected_tags(run_id, session_id).items())


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def image_source_closure_sha256(
    role: str, implementation_tree_sha256: str
) -> str:
    if role not in {"collector", "consumer", "archive"}:
        raise ValueError("unsupported Phase 7 image role")
    return hashlib.sha256(json.dumps(
        {
            "role": role,
            "implementationTreeSha256": implementation_tree_sha256,
            "collectorCommit": (
                PHASE7_COLLECTOR_COMMIT if role == "collector" else None
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def reject_strict_paid_work_under_composite_policy(root: Path) -> None:
    """Fail closed while the user-authorized no-new-50k policy is active."""
    root = root.resolve()
    policy_path = (root / SCOPED_PROMOTION_POLICY_PATH).resolve()
    policy_path.relative_to(root)
    if (
        not policy_path.is_file()
        or file_sha256(policy_path) != SCOPED_PROMOTION_POLICY_SHA256
    ):
        raise RuntimeError(
            "strict paid work has no exact current-campaign authorization policy"
        )
    policy = read_json(policy_path)
    if (
        policy.get("decision")
        != "promote-after-minimal-smoke-and-archive-without-new-50k"
        or policy.get("execution", {}).get("new50kRpsAttempt") is not False
        or policy.get("execution", {}).get("newWarmupAttempt") is not False
        or policy.get("execution", {}).get("newScoreAttempt") is not False
        or policy.get("phase8", {}).get("paidAwsExperiment") is not False
    ):
        raise RuntimeError(
            "strict paid work has no exact current-campaign authorization policy"
        )
    raise RuntimeError(
        "strict paid work is disabled by the active composite Phase 8 policy; "
        "a future certification requires a new explicit authorization artifact "
        "and a new budget contract"
    )


def handoff_checks(root: Path, handoff_path: Path) -> tuple[list[Check], dict[str, Any]]:
    root = root.resolve()
    handoff_path = handoff_path.resolve()
    handoff = read_json(handoff_path)
    cleanup = handoff.get("cleanup") if isinstance(handoff.get("cleanup"), dict) else {}
    gates = handoff.get("gates") if isinstance(handoff.get("gates"), dict) else {}
    failures = handoff.get("unresolvedFailures")
    local_path = Path(str(handoff.get("localRunPath", ""))).resolve()
    implementation_check = implementation_manifest_check(root, handoff)
    checks = [
        Check("handoff verdict", handoff.get("finalVerdict") == "passed" and handoff.get("awsReady") is True,
              {"finalVerdict": handoff.get("finalVerdict"), "awsReady": handoff.get("awsReady")},
              {"finalVerdict": "passed", "awsReady": True}, "Only a passed immutable Phase 7-1 handoff authorizes AWS work."),
        Check("explicit handoff path", local_path == handoff_path.parent, str(local_path), str(handoff_path.parent),
              "The handoff may not auto-select or point at a different run directory."),
        Check("local AWS network audit", gates.get("realAwsRequests") == 0, gates.get("realAwsRequests"), 0,
              "Phase 7-1 must make zero real AWS requests."),
        Check("local cleanup inventory", cleanup.get("status") == "passed" and not any(cleanup.get(key) for key in ("containers", "volumes", "networks")),
              cleanup, "passed with exact zero inventory", "Every run-owned Docker resource must be absent."),
        Check("unresolved failures", failures == [], failures, [], "A handoff with unresolved failures is not deployable."),
        implementation_check,
    ]
    return checks, handoff


def implementation_manifest_check(root: Path, document: dict[str, Any]) -> Check:
    """Recompute one frozen implementation manifest without trusting its stored hashes."""
    root = root.resolve()
    manifest = document.get("implementationFiles")
    manifest_checks: list[dict[str, Any]] = []
    combined = hashlib.sha256()
    manifest_valid = isinstance(manifest, list) and bool(manifest)
    if manifest_valid:
        for entry in manifest:
            if not isinstance(entry, dict):
                manifest_valid = False
                break
            relative = entry.get("path")
            expected = entry.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected, str):
                manifest_valid = False
                break
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                manifest_valid = False
                break
            actual = file_sha256(candidate) if candidate.is_file() else None
            match = actual == expected
            manifest_checks.append({"path": relative, "expected": expected, "actual": actual, "match": match})
            combined.update(relative.encode("utf-8"))
            combined.update(b"\0")
            combined.update((actual or "missing").encode("ascii"))
            combined.update(b"\n")
            manifest_valid = manifest_valid and match
    computed_tree = combined.hexdigest()
    return Check(
        "implementation manifest",
        manifest_valid and computed_tree == document.get("implementationTreeSha256"),
        {
            "computedTreeSha256": computed_tree,
            "documentTreeSha256": document.get("implementationTreeSha256"),
            "files": manifest_checks,
        },
        "every file hash and implementation tree hash match",
        "Recompute every frozen implementation input before AWS calls.",
    )


def scoped_diagnostic_source_checks(
    root: Path, source_path: Path
) -> tuple[list[Check], dict[str, Any]]:
    """Validate a focused source seal without misrepresenting it as a Phase 7-1 handoff."""
    source_path = source_path.resolve()
    source = read_json(source_path)
    focused = source.get("focusedGates")
    focused_observed: list[dict[str, Any]] = []
    focused_valid = isinstance(focused, list) and bool(focused)
    if focused_valid:
        for item in focused:
            if (
                not isinstance(item, dict)
                or item.get("status") != "passed"
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str)
            ):
                focused_valid = False
                break
            candidate = (root.resolve() / str(item["path"])).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                focused_valid = False
                break
            actual = file_sha256(candidate) if candidate.is_file() else None
            match = actual == item["sha256"]
            focused_observed.append({**item, "actualSha256": actual, "match": match})
            focused_valid = focused_valid and match
    focused_paths = {
        str(item.get("path")) for item in focused if isinstance(item, dict)
    } if isinstance(focused, list) else set()
    focused_valid = focused_valid and focused_paths == SCOPED_FOCUSED_GATE_PATHS

    baseline = source.get("baselineHandoff", {})
    baseline_path = (root.resolve() / SCOPED_BASELINE_PATH).resolve()
    baseline_valid = (
        baseline.get("path") == SCOPED_BASELINE_PATH
        and baseline.get("sha256") == SCOPED_BASELINE_SHA256
        and baseline_path.is_file()
        and file_sha256(baseline_path) == SCOPED_BASELINE_SHA256
    )
    policy = source.get("policy", {})
    policy_path = (root.resolve() / SCOPED_POLICY_PATH).resolve()
    policy_valid = (
        policy.get("path") == SCOPED_POLICY_PATH
        and policy.get("sha256") == SCOPED_POLICY_SHA256
        and policy_path.is_file()
        and file_sha256(policy_path) == SCOPED_POLICY_SHA256
    )
    promotion_policy = source.get("compositePromotionPolicy", {})
    promotion_policy_path = (
        root.resolve() / SCOPED_PROMOTION_POLICY_PATH
    ).resolve()
    promotion_policy_valid = (
        promotion_policy.get("path") == SCOPED_PROMOTION_POLICY_PATH
        and promotion_policy.get("sha256") == SCOPED_PROMOTION_POLICY_SHA256
        and promotion_policy_path.is_file()
        and file_sha256(promotion_policy_path)
        == SCOPED_PROMOTION_POLICY_SHA256
    )
    execution_contract_valid = (
        source.get("attemptType") == "aws-full-stack-scoped-diagnostic"
        and source.get("stackDefinitions") == [
            IMAGE_STACK_NAME,
            RUNTIME_STACK_NAME,
        ]
        and source.get("topologyBaseline") == "Attempt 17"
        and source.get("stagePlan") == SCOPED_STAGE_PLAN
        and source.get("zeroAttemptStages") == SCOPED_ZERO_ATTEMPT_STAGES
    )
    checks = [
        Check(
            "scoped diagnostic pinned baseline",
            baseline_valid,
            baseline,
            {"path": SCOPED_BASELINE_PATH, "sha256": SCOPED_BASELINE_SHA256},
            "The implementation path list comes only from the immutable pre-Attempt 17 handoff.",
        ),
        Check(
            "scoped diagnostic pinned policy",
            policy_valid,
            policy,
            {"path": SCOPED_POLICY_PATH, "sha256": SCOPED_POLICY_SHA256},
            "The user-approved full-stack replacement policy is immutable.",
        ),
        Check(
            "scoped diagnostic pinned composite promotion policy",
            promotion_policy_valid,
            promotion_policy,
            {
                "path": SCOPED_PROMOTION_POLICY_PATH,
                "sha256": SCOPED_PROMOTION_POLICY_SHA256,
            },
            "The no-new-50k composite Phase 8 policy is immutable.",
        ),
        Check(
            "scoped diagnostic execution contract",
            execution_contract_valid,
            {
                "attemptType": source.get("attemptType"),
                "stackDefinitions": source.get("stackDefinitions"),
                "topologyBaseline": source.get("topologyBaseline"),
                "stagePlan": source.get("stagePlan"),
                "zeroAttemptStages": source.get("zeroAttemptStages"),
            },
            "Attempt 17 full stacks with deploy/verify/seed/archive/collect/cleanup/inventory only",
            "A dedicated diagnostic stack or load/source-DROP stage is not authorized.",
        ),
        Check(
            "scoped diagnostic source type",
            source.get("recordType") == "phase7-full-stack-scoped-diagnostic-source",
            source.get("recordType"),
            "phase7-full-stack-scoped-diagnostic-source",
            "A focused source seal is distinct from a strict Phase 7-1 handoff.",
        ),
        Check(
            "scoped diagnostic authorization",
            source.get("awsDiagnosticReady") is True
            and source.get("promotionEligible") is False
            and source.get("phase5") == "skipped",
            {
                "awsDiagnosticReady": source.get("awsDiagnosticReady"),
                "promotionEligible": source.get("promotionEligible"),
                "phase5": source.get("phase5"),
            },
            {
                "awsDiagnosticReady": True,
                "promotionEligible": False,
                "phase5": "skipped",
            },
            "The attempt remains promotion-ineligible; only a later composite handoff may promote its evidence.",
        ),
        Check(
            "scoped diagnostic unresolved failures",
            source.get("unresolvedFailures") == [],
            source.get("unresolvedFailures"),
            [],
            "A known focused-gate failure cannot cross the paid boundary.",
        ),
        Check(
            "scoped diagnostic focused gates",
            focused_valid,
            focused_observed,
            "one or more immutable passed focused-gate artifacts",
            "Deferred whole-local coverage is allowed only when focused deployment gates passed.",
        ),
        implementation_manifest_check(root, source),
    ]
    return checks, source


def checks_document(checks: Iterable[Check]) -> list[dict[str, Any]]:
    return [check.as_dict() for check in checks]

#!/usr/bin/env python3
"""Build the user-authorized composite Phase 8 handoff without mutating attempts."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from common import (
    SCOPED_PROMOTION_POLICY_PATH,
    SCOPED_PROMOTION_POLICY_SHA256,
    file_sha256,
    image_source_closure_sha256,
    read_json,
    scoped_diagnostic_source_checks,
    utc_now,
    write_json,
)
from full_stack_scoped_cost_model import (
    canonical_sha256,
    validate_campaign_ledger,
    validate_phase8_promotion_policy,
)


PERFORMANCE_ORDINAL = 17
PERFORMANCE_RUN_ID = "run_20260719_043415_phase7_integration"
PERFORMANCE_SESSION_ID = "phase7-integration-20260719T043415Z"
SCOPED_ATTEMPT_TYPE = "aws-full-stack-scoped-diagnostic"
HANDOFF_RECORD_TYPE = "phase7-2-composite-phase8-handoff"
PERFORMANCE_ENTRY_PATH = (
    "performance-tests/phase7_2-stabilization/attempt-17-ledger-entry.json"
)


def evidence_ref(root: Path, relative: str, expected_sha256: str) -> dict[str, str]:
    root = root.resolve()
    candidate = (root / relative).resolve()
    candidate.relative_to(root)
    if not candidate.is_file():
        raise FileNotFoundError(f"composite evidence is missing: {relative}")
    observed = file_sha256(candidate)
    if observed != expected_sha256:
        raise RuntimeError(f"composite evidence hash changed: {relative}")
    return {"path": relative, "sha256": observed}


def entry_evidence_ref(
    root: Path, entry: dict[str, Any], key: str
) -> tuple[dict[str, str], dict[str, Any]]:
    reference = entry.get("terminalEvidenceHashes", {}).get(key)
    if (
        not isinstance(reference, dict)
        or not isinstance(reference.get("path"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", "")))
        is None
    ):
        raise RuntimeError(f"scoped attempt has no exact {key} evidence reference")
    verified = evidence_ref(root, reference["path"], reference["sha256"])
    return verified, read_json(root / verified["path"])


def anchored_entry(
    root: Path,
    entry: dict[str, Any],
    relative: str,
    *,
    label: str,
) -> dict[str, str]:
    candidate = (root.resolve() / relative).resolve()
    candidate.relative_to(root.resolve())
    if not candidate.is_file() or read_json(candidate) != entry:
        raise RuntimeError(f"{label} immutable ledger-entry anchor differs")
    return {
        "path": relative,
        "fileSha256": file_sha256(candidate),
        "entrySha256": str(entry.get("entrySha256")),
    }


def scoped_source_anchor(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    relative = entry.get("evidencePaths", {}).get("scopedSource")
    if not isinstance(relative, str) or not relative:
        raise RuntimeError("fresh scoped entry has no immutable source path")
    source_path = (root.resolve() / relative).resolve()
    source_path.relative_to(root.resolve())
    checks, source = scoped_diagnostic_source_checks(root, source_path)
    failed = [check.name for check in checks if not check.passed]
    observed_file_sha = file_sha256(source_path) if source_path.is_file() else None
    source_tree = source.get("implementationTreeSha256")
    expected_image_sources = {
        role: image_source_closure_sha256(role, str(source_tree))
        for role in ("archive", "collector", "consumer")
    }
    if (
        failed
        or observed_file_sha != entry.get("immutableInputHashes", {}).get("scopedSource")
        or source.get("attemptType") != SCOPED_ATTEMPT_TYPE
        or source.get("promotionEligible") is not False
        or source.get("gitCommit") != entry.get("gitCommit")
        or source.get("gitTree") != entry.get("implementationGitTree")
        or source_tree != entry.get("implementationSourceClosureSha256")
        or entry.get("imageSourceHashes") != expected_image_sources
    ):
        raise RuntimeError(
            "fresh scoped source seal is not bound to the immutable ledger entry"
        )
    return {
        "path": relative,
        "fileSha256": observed_file_sha,
        "gitCommit": source["gitCommit"],
        "gitTree": source["gitTree"],
        "implementationTreeSha256": source_tree,
        "checksPassed": [check.name for check in checks],
    }


def performance_basis(
    root: Path,
    entry: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    if (
        entry.get("ordinal") != PERFORMANCE_ORDINAL
        or entry.get("runId") != PERFORMANCE_RUN_ID
        or entry.get("sessionId") != PERFORMANCE_SESSION_ID
        or entry.get("attemptType") != "aws-integration-strict"
        or entry.get("promotionEligible") is not True
        or entry.get("verdict") != "failed"
        or entry.get("phase5") != "skipped"
    ):
        raise RuntimeError("Attempt 17 immutable performance entry is not exact")
    counts = entry.get("stageAttemptCounts", {})
    if any(
        int(counts.get(stage, -1)) != 1
        for stage in ("deploy", "verify", "correctness", "warmup", "score", "archive", "cleanup", "inventory")
    ):
        raise RuntimeError("Attempt 17 did not execute the inherited stages exactly once")

    inherited = policy["inheritedPerformanceEvidence"]
    if (
        inherited.get("attemptOrdinal") != PERFORMANCE_ORDINAL
        or inherited.get("runId") != PERFORMANCE_RUN_ID
        or inherited.get("sessionId") != PERFORMANCE_SESSION_ID
        or inherited.get("attemptVerdict") != "failed"
    ):
        raise RuntimeError("promotion policy does not bind the exact Attempt 17 identity")

    score_ref = evidence_ref(
        root, inherited["score"]["path"], inherited["score"]["sha256"]
    )
    score = read_json(root / score_ref["path"])
    aggregate = score.get("aggregate", {})
    if (
        score.get("runId") != PERFORMANCE_RUN_ID
        or score.get("sessionId") != PERFORMANCE_SESSION_ID
        or score.get("workerContractChecksPassed") is not True
        or int(aggregate.get("requestedRequests", -1)) != 15_000_000
        or int(aggregate.get("attemptedRequests", -1)) != 15_000_000
        or int(aggregate.get("completedRequests", -1)) != 15_000_000
        or int(aggregate.get("http202", -1)) != 15_000_000
        or float(aggregate.get("actualRps", -1)) < 49_500
        or any(
            int(aggregate.get(name, -1)) != 0
            for name in ("transportErrors", "http429", "http5xx")
        )
        or float(aggregate.get("latencyCorrectedMs", {}).get("p95", 10**9))
        >= 300
    ):
        raise RuntimeError("Attempt 17 inherited 50k score evidence is not exact")

    correctness_ref = evidence_ref(
        root,
        inherited["correctness"]["path"],
        inherited["correctness"]["sha256"],
    )
    correctness = read_json(root / correctness_ref["path"])
    if (
        correctness.get("runId") != PERFORMANCE_RUN_ID
        or correctness.get("sessionId") != PERFORMANCE_SESSION_ID
        or correctness.get("passed") is not True
        or correctness.get("correctness", {}).get("inputRecords") != 1002
        or correctness.get("correctness", {}).get("passed") is not True
        or correctness.get("replacement", {}).get("offered") != 900
        or correctness.get("replacement", {}).get("passed") is not True
    ):
        raise RuntimeError("Attempt 17 inherited correctness/replacement evidence is not exact")

    deployment_ref = evidence_ref(
        root,
        inherited["deployment"]["path"],
        inherited["deployment"]["sha256"],
    )
    deployment = read_json(root / deployment_ref["path"])
    if (
        deployment.get("runId") != PERFORMANCE_RUN_ID
        or deployment.get("sessionId") != PERFORMANCE_SESSION_ID
        or deployment.get("passed") is not True
    ):
        raise RuntimeError("Attempt 17 deployment evidence is not exact")

    deviation_ref = evidence_ref(
        root,
        inherited["knownStrictDeviation"]["path"],
        inherited["knownStrictDeviation"]["sha256"],
    )
    return {
        "attemptOrdinal": PERFORMANCE_ORDINAL,
        "runId": PERFORMANCE_RUN_ID,
        "sessionId": PERFORMANCE_SESSION_ID,
        "entrySha256": entry["entrySha256"],
        "immutableVerdict": "failed",
        "verdictRewritten": False,
        "inheritedEvidenceOnly": True,
        "score": {
            "requestedRequests": int(aggregate["requestedRequests"]),
            "completedRequests": int(aggregate["completedRequests"]),
            "actualRps": float(aggregate["actualRps"]),
            "transportErrors": int(aggregate["transportErrors"]),
            "http429": int(aggregate["http429"]),
            "http5xx": int(aggregate["http5xx"]),
            "correctedP95Ms": float(aggregate["latencyCorrectedMs"]["p95"]),
            "evidence": score_ref,
        },
        "correctnessAndReplacement": {
            "passed": True,
            "evidence": correctness_ref,
        },
        "deployment": {"passed": True, "evidence": deployment_ref},
        "knownStrictDeviation": {
            "preserved": True,
            "evidence": deviation_ref,
            "description": inherited["knownStrictDeviation"]["description"],
        },
    }


def archive_basis(
    root: Path,
    entry: dict[str, Any],
    *,
    entry_anchor: dict[str, str] | None = None,
    source_anchor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (
        entry.get("attemptType") != SCOPED_ATTEMPT_TYPE
        or entry.get("promotionEligible") is not False
        or entry.get("verdict") != "passed"
        or entry.get("phase5") != "skipped"
    ):
        raise RuntimeError("fresh archive attempt is not an immutable scoped pass")
    counts = entry.get("stageAttemptCounts", {})
    if any(
        int(counts.get(stage, -1)) != 1
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
    ) or any(
        int(counts.get(stage, -1)) != 0
        for stage in ("correctness", "replacement", "warmup", "score", "source-drop")
    ):
        raise RuntimeError("fresh archive attempt stage counts are not exact")

    deployment_ref, deployment = entry_evidence_ref(
        root, entry, "deploymentVerification"
    )
    archive_ref, archive = entry_evidence_ref(root, entry, "archive")
    cleanup_ref, cleanup = entry_evidence_ref(root, entry, "cleanup")
    if (
        deployment.get("runId") != entry.get("runId")
        or deployment.get("sessionId") != entry.get("sessionId")
        or deployment.get("passed") is not True
        or deployment.get("clickHouse", {}).get("query_ok") != 1
        or deployment.get("clickHouse", {}).get("schema_tables") != 2
        or not deployment.get("protocolPath", {}).get("generatorReadiness")
        or any(
            item.get("tlsHttp2Health") is not True
            for item in deployment["protocolPath"]["generatorReadiness"].values()
        )
    ):
        raise RuntimeError("fresh deployment minimal smoke evidence is not exact")
    required_archive_checks = {
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
    }
    checks = archive.get("checks", {})
    archive_detail = archive.get("archive", {})
    if (
        archive.get("runId") != entry.get("runId")
        or archive.get("sessionId") != entry.get("sessionId")
        or archive.get("passed") is not True
        or set(checks) != required_archive_checks
        or not all(checks.values())
        or archive_detail.get("rows") != 15_000_000
        or archive_detail.get("sourceRowsAfterArchive") != 15_000_000
        or archive_detail.get("objects") != 3
        or archive_detail.get("objectRows") != [5_000_000, 5_000_000, 5_000_000]
        or archive_detail.get("diagnosticSourceRetention") is not True
        or archive_detail.get("dropExecuted") is not False
        or any(
            archive_detail.get(name) != 0
            for name in (
                "preDropSourceMinusArchive",
                "preDropArchiveMinusSource",
                "committedSourceMinusArchive",
                "committedArchiveMinusSource",
            )
        )
        or int(archive.get("queryLog", {}).get("code241Exceptions", -1)) != 0
        or int(archive.get("queryLog", {}).get("sourceDropQueries", -1)) != 0
    ):
        raise RuntimeError("fresh 15M retain-source archive evidence is not exact")
    inventory = entry.get("cleanup", {}).get("finalAuthoritativeInventory", {})
    if (
        cleanup.get("allZero") is not True
        or cleanup.get("serviceInventoryZero") is not True
        or cleanup.get("taggingApiResidualsZero") is not True
        or inventory.get("allZero") is not True
        or inventory.get("serviceInventoryZero") is not True
        or inventory.get("taggingApiResidualsZero") is not True
        or inventory.get("taggingApiResiduals") != []
    ):
        raise RuntimeError("fresh archive attempt cleanup evidence is not exact zero")
    images = entry.get("imageDigests", {})
    if set(images) != {"archive", "collector", "consumer"} or any(
        re.fullmatch(r"sha256:[0-9a-f]{64}", str(value)) is None
        for value in images.values()
    ):
        raise RuntimeError("fresh archive image digests are not exact")
    return {
        "attemptOrdinal": entry["ordinal"],
        "runId": entry["runId"],
        "sessionId": entry["sessionId"],
        "entrySha256": entry["entrySha256"],
        "immutableVerdict": "passed",
        "attemptPromotionEligible": False,
        "verdictRewritten": False,
        "immutableEntryAnchor": entry_anchor,
        "immutableSourceAnchor": source_anchor,
        "gitCommit": entry.get("gitCommit"),
        "implementationSourceClosureSha256": entry.get(
            "implementationSourceClosureSha256"
        ),
        "imageSourceHashes": entry.get("imageSourceHashes"),
        "imageDigests": images,
        "minimalSmoke": {"passed": True, "evidence": deployment_ref},
        "archive": {
            "passed": True,
            "rows": archive_detail["rows"],
            "objects": archive_detail["objects"],
            "objectRows": archive_detail["objectRows"],
            "sourceRetainedRows": archive_detail["sourceRowsAfterArchive"],
            "dropExecuted": False,
            "evidence": archive_ref,
        },
        "cleanup": {"allZero": True, "evidence": cleanup_ref},
    }


def build(
    root: Path,
    ledger_path: Path,
    policy_path: Path,
) -> dict[str, Any]:
    root = root.resolve()
    ledger_path = ledger_path.resolve()
    policy_path = policy_path.resolve()
    expected_ledger = (
        root / "performance-tests/phase7_2-stabilization/attempt-ledger.json"
    ).resolve()
    expected_policy = (root / SCOPED_PROMOTION_POLICY_PATH).resolve()
    if ledger_path != expected_ledger or policy_path != expected_policy:
        raise RuntimeError("composite handoff requires the exact ledger and policy paths")
    if file_sha256(policy_path) != SCOPED_PROMOTION_POLICY_SHA256:
        raise RuntimeError("composite Phase 8 promotion policy file hash changed")
    policy = read_json(policy_path)
    validate_phase8_promotion_policy(policy)
    ledger = read_json(ledger_path)
    active_accrued, active_epoch_id, _ = validate_campaign_ledger(ledger)
    if ledger.get("activeAttempt") is not None:
        raise RuntimeError("composite handoff requires an idle campaign ledger")
    attempts = ledger.get("attempts", [])
    if len(attempts) < PERFORMANCE_ORDINAL + 3:
        raise RuntimeError("composite handoff has no fresh post-Attempt 19 archive result")
    performance_entry = attempts[PERFORMANCE_ORDINAL - 1]
    archive_entry = attempts[-1]
    if ledger.get("ledgerHeadSha256") != archive_entry.get("entrySha256"):
        raise RuntimeError("fresh archive attempt is not the immutable ledger head")
    if active_accrued > 60:
        raise RuntimeError("active cost epoch exceeds the user-authorized 60 USD cap")

    performance_anchor = anchored_entry(
        root,
        performance_entry,
        PERFORMANCE_ENTRY_PATH,
        label="Attempt 17",
    )
    archive_runtime = archive_entry.get("evidencePaths", {}).get("awsAttempt")
    if not isinstance(archive_runtime, str) or not archive_runtime:
        raise RuntimeError("fresh archive attempt has no immutable runtime path")
    archive_entry_path = str(Path(archive_runtime) / "campaign-ledger-entry.json")
    archive_entry_anchor = anchored_entry(
        root,
        archive_entry,
        archive_entry_path,
        label="fresh scoped attempt",
    )
    source_anchor = scoped_source_anchor(root, archive_entry)

    performance = performance_basis(root, performance_entry, policy)
    performance["immutableEntryAnchor"] = performance_anchor
    archive = archive_basis(
        root,
        archive_entry,
        entry_anchor=archive_entry_anchor,
        source_anchor=source_anchor,
    )
    handoff = {
        "schemaVersion": 1,
        "recordType": HANDOFF_RECORD_TYPE,
        "generatedAt": utc_now(),
        "phase": "7-2-to-8",
        "phase5": "skipped",
        "promotionMode": "composite-user-authorized",
        "phase8EntryAuthorized": True,
        "strictSingleAttemptPassClaimed": False,
        "historicalAttemptVerdictsRewritten": False,
        "userAcceptancePolicy": {
            "path": SCOPED_PROMOTION_POLICY_PATH,
            "fileSha256": file_sha256(policy_path),
            "canonicalSha256": canonical_sha256(policy),
            "decision": policy["decision"],
        },
        "campaign": {
            "ledgerPath": str(ledger_path.relative_to(root)),
            "ledgerFileSha256AtHandoff": file_sha256(ledger_path),
            "ledgerHeadSha256": ledger["ledgerHeadSha256"],
            "statusAtHandoff": ledger["status"],
            "activeAttempt": None,
            "activeEpochId": active_epoch_id,
            "activeEpochAccruedUpperBoundUsd": format(active_accrued, ".6f"),
            "hardCapUsd": "60.000000",
            "phase8PaidAwsExperimentOperationalUpperBoundUsd": "0.000000",
        },
        "promotionBasis": {
            "performanceCorrectnessAndRecovery": performance,
            "freshMinimalSmokeAndArchive": archive,
        },
        "acceptance": {
            "deploymentReadiness": True,
            "minimalSmoke": True,
            "correctness1002Inherited": True,
            "replacement900Inherited": True,
            "score50kInheritedWithoutRerun": True,
            "freshArchive15M": True,
            "freshArchiveEquivalence": True,
            "freshSourceRetentionAndNoDrop": True,
            "freshCode241Zero": True,
            "cleanupZero": True,
            "withinActiveEpoch60UsdCap": True,
            "phase5Skipped": True,
        },
        "phase8Execution": {
            "paidAwsExperiment": False,
            "awsMutationByDefault": False,
            "repeatWarmupScoreOrArchive": False,
            "verificationScope": policy["phase8"]["verificationScope"],
        },
    }
    handoff["handoffSha256"] = canonical_sha256(handoff)
    return handoff


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-root", required=True, type=Path)
    parser.add_argument("--attempt-ledger", required=True, type=Path)
    parser.add_argument("--promotion-policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output.exists():
        raise FileExistsError("Phase 8 composite handoff is immutable")
    result = build(args.infra_root, args.attempt_ledger, args.promotion_policy)
    write_json(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

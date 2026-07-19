#!/usr/bin/env python3
"""Validate the unpaid composite evidence and build the Phase 8 baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
AWS_TOOLS = ROOT / "performance-tests/phase7-integration/aws"
sys.path.insert(0, str(AWS_TOOLS))

from build_phase8_composite_handoff import (  # noqa: E402
    PERFORMANCE_ENTRY_PATH,
    anchored_entry,
    performance_basis,
    scoped_source_anchor,
)
from common import file_sha256, read_json  # noqa: E402
from full_stack_scoped_cost_model import (  # noqa: E402
    canonical_sha256,
    validate_campaign_ledger,
    validate_phase8_promotion_policy,
)


LEDGER = Path("performance-tests/phase7_2-stabilization/attempt-ledger.json")
POLICY = Path(
    "performance-tests/phase7_2-stabilization/"
    "phase8-composite-promotion-policy-20260719.json"
)
AMENDMENT = Path(
    "performance-tests/phase7_2-stabilization/"
    "phase8-cleanup-recovered-amendment-20260719.json"
)
HANDOFF = Path("performance-tests/phase7_2-stabilization/phase8-handoff.json")
FINAL_DIR = Path("performance-tests/phase8-final")
ATTEMPT_23_RUNTIME = Path(
    "performance-tests/run_20260719_164311_phase7_2_aws_integration"
)
ATTEMPT_23_READINESS = Path(
    "performance-tests/run_20260719_164311_phase7_2_deployment_readiness"
)
EXPECTED_ATTEMPT_23 = {
    "ordinal": 23,
    "runId": "run_20260719_164311_phase7_integration",
    "sessionId": "phase7-integration-20260719T164311Z",
    "entrySha256": "cd5217ea95bd66d1c48540ec55cc88bf409c48a1b2fadee87e5e40a73b17aebc",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    if path.exists():
        raise FileExistsError(f"immutable Phase 8 output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    if path.exists():
        raise FileExistsError(f"immutable Phase 8 output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def evidence(path: Path, expected: str | None = None) -> dict[str, str]:
    absolute = ROOT / path
    if not absolute.is_file():
        raise FileNotFoundError(path)
    observed = file_sha256(absolute)
    if expected is not None and observed != expected:
        raise RuntimeError(f"evidence hash changed: {path}")
    return {"path": str(path), "sha256": observed}


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def stage_map(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("stage")): item
        for item in run.get("stageAttempts", [])
        if isinstance(item, dict)
    }


def archive_summary(document: dict[str, Any]) -> dict[str, Any]:
    manifest = document["archive"]["manifest"]
    parts = manifest["parts"]
    checks = document["checks"]
    ensure(document.get("passed") is True, "Attempt 23 archive did not pass")
    ensure(checks and all(checks.values()), "Attempt 23 archive checks are incomplete")
    ensure(manifest["archive"]["rows"] == 15_000_000, "archive row count differs")
    ensure(manifest["archive"]["uniqueEvents"] == 15_000_000, "archive unique count differs")
    ensure(len(parts) == 3, "archive object count differs")
    ensure([part["rows"] for part in parts] == [5_000_000] * 3, "archive part rows differ")
    ensure(
        document["archive"]["sourceRowsAfterArchive"] == 15_000_000,
        "source was not retained",
    )
    ensure(document["archive"]["dropExecuted"] is False, "source DROP was executed")
    ensure(document["queryLog"]["sourceDropQueries"] == 0, "source DROP query was observed")
    ensure(document["queryLog"]["code241Exceptions"] == 0, "Code 241 was observed")
    return {
        "rows": 15_000_000,
        "uniqueEvents": 15_000_000,
        "logicalChecksum": manifest["archive"]["logicalChecksum"],
        "sourceRowsAfter": document["archive"]["sourceRowsAfterArchive"],
        "dropExecuted": False,
        "code241Exceptions": 0,
        "sourceDropQueries": 0,
        "peakQueryMemoryBytes": document["queryLog"]["peakQueryMemoryBytes"],
        "objects": [
            {
                "index": part["index"],
                "rows": part["rows"],
                "bytes": part["bytes"],
                "sha256": part["sha256"],
            }
            for part in parts
        ],
        "checks": checks,
    }


def validate_inputs(ledger_override: dict[str, Any] | None = None) -> dict[str, Any]:
    ledger_path = ROOT / LEDGER
    policy_path = ROOT / POLICY
    amendment_path = ROOT / AMENDMENT
    ledger = ledger_override if ledger_override is not None else read_json(ledger_path)
    policy = read_json(policy_path)
    amendment = read_json(amendment_path)
    validate_phase8_promotion_policy(policy)
    accrued, epoch_id, _ = validate_campaign_ledger(ledger)
    ensure(ledger.get("activeAttempt") is None, "campaign ledger is not idle")
    ensure(ledger.get("status") == "stabilizing", "campaign status is not pre-promotion")
    ensure(accrued <= 60, "active cost epoch exceeds 60 USD")
    ensure(amendment["basePolicy"]["sha256"] == file_sha256(policy_path), "base policy changed")
    ensure(amendment["userDirection"]["newPaidAwsExperiment"] is False, "paid rerun enabled")
    attempts = ledger["attempts"]
    performance = attempts[16]
    fresh = attempts[-1]
    ensure(
        all(fresh.get(key) == value for key, value in EXPECTED_ATTEMPT_23.items()),
        "Attempt 23 ledger head differs",
    )
    ensure(ledger["ledgerHeadSha256"] == fresh["entrySha256"], "ledger head differs")
    ensure(fresh.get("verdict") == "failed", "Attempt 23 verdict was rewritten")
    ensure(fresh.get("firstFailingGate") == "cleanup", "Attempt 23 failure scope differs")
    ensure(fresh.get("promotionEligible") is False, "Attempt 23 promotion flag was rewritten")
    ensure(fresh.get("phase5") == "skipped", "Phase 5 changed")
    ensure(
        amendment["evidenceAttempt"]["entrySha256"] == fresh["entrySha256"],
        "amendment is not bound to Attempt 23",
    )

    performance_anchor = anchored_entry(
        ROOT, performance, PERFORMANCE_ENTRY_PATH, label="Attempt 17"
    )
    performance_result = performance_basis(ROOT, performance, policy)
    performance_result["immutableEntryAnchor"] = performance_anchor

    runtime = ROOT / ATTEMPT_23_RUNTIME
    entry_path = ATTEMPT_23_RUNTIME / "campaign-ledger-entry.json"
    fresh_anchor = anchored_entry(ROOT, fresh, str(entry_path), label="Attempt 23")
    source_anchor = scoped_source_anchor(ROOT, fresh)
    run = read_json(runtime / "run.json")
    execution = read_json(runtime / "execution-summary.json")
    deployment = read_json(runtime / "deployment-verification.json")
    archive = read_json(runtime / "archive-validation.json")
    cleanup = read_json(runtime / "cleanup-recovery-attempt-2-verification.json")
    global_inventory = read_json(runtime / "post-cleanup-global-inventory.json")
    stages = stage_map(run)
    for name in ("deploy", "verify", "seed", "archive", "collect"):
        ensure(stages.get(name, {}).get("passed") is True, f"functional stage failed: {name}")
    ensure(run.get("verdict") == "failed", "Attempt 23 runtime verdict was rewritten")
    ensure(run.get("failedStage") == "cleanup", "Attempt 23 runtime failure scope differs")
    ensure(run.get("cleanupInventoryZero") is True, "Attempt 23 cleanup did not recover")
    ensure(execution.get("sourceDropExecuted") is False, "source DROP non-execution is unproven")
    ensure(deployment.get("passed") is True, "minimal deployment smoke failed")
    ensure(cleanup.get("allZero") is True, "final cleanup inventory is nonzero")
    ensure(cleanup.get("serviceInventoryZero") is True, "service inventory is nonzero")
    ensure(cleanup.get("taggingApiResidualsZero") is True, "tag inventory is nonzero")
    ensure(cleanup.get("taggingApiResiduals") == [], "tag residual list is nonempty")
    ensure(global_inventory.get("allZero") is True, "global run-owned tag inventory is nonzero")
    ensure(global_inventory.get("resourceArns") == [], "global tag inventory is nonempty")
    archive_result = archive_summary(archive)

    expected_terminal = fresh["terminalEvidenceHashes"]
    refs = {
        "run": evidence(ATTEMPT_23_RUNTIME / "run.json", expected_terminal["run"]["sha256"]),
        "execution": evidence(
            ATTEMPT_23_RUNTIME / "execution-summary.json",
            expected_terminal["executionSummary"]["sha256"],
        ),
        "deployment": evidence(
            ATTEMPT_23_RUNTIME / "deployment-verification.json",
            expected_terminal["deploymentVerification"]["sha256"],
        ),
        "archive": evidence(
            ATTEMPT_23_RUNTIME / "archive-validation.json",
            expected_terminal["archive"]["sha256"],
        ),
        "cleanup": evidence(
            ATTEMPT_23_RUNTIME / "cleanup-recovery-attempt-2-verification.json",
            expected_terminal["cleanup"]["sha256"],
        ),
        "globalInventory": evidence(
            ATTEMPT_23_RUNTIME / "post-cleanup-global-inventory.json"
        ),
    }
    return {
        "ledger": ledger,
        "policy": policy,
        "amendment": amendment,
        "performance": performance_result,
        "fresh": fresh,
        "freshEntryAnchor": fresh_anchor,
        "sourceAnchor": source_anchor,
        "run": run,
        "deployment": deployment,
        "archive": archive_result,
        "cleanup": cleanup,
        "refs": refs,
        "accrued": format(accrued, ".6f"),
        "epochId": epoch_id,
    }


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git_blob(commit: str, path: Path) -> bytes:
    ensure(
        len(commit) == 40 and all(character in "0123456789abcdef" for character in commit),
        "promotion finalization commit is not an exact SHA-1",
    )
    result = subprocess.run(
        ["git", "show", f"{commit}:{path.as_posix()}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return result.stdout


def git_is_ancestor(commit: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def validate_self_hash(document: dict[str, Any], field: str, label: str) -> str:
    claimed = document.get(field)
    unhashed = dict(document)
    unhashed.pop(field, None)
    ensure(
        isinstance(claimed, str) and canonical_sha256(unhashed) == claimed,
        f"{label} self-hash differs",
    )
    return claimed


def validate_post_promotion_cost_reset(
    pre_promotion: dict[str, Any], ledger: dict[str, Any]
) -> None:
    pre_epochs = pre_promotion["budgetEpochs"]
    current_epochs = ledger["budgetEpochs"]
    ensure(
        len(current_epochs) == len(pre_epochs) + 1,
        "post-promotion cost reset did not append exactly one epoch",
    )
    ensure(
        current_epochs[:-2] == pre_epochs[:-1],
        "post-promotion cost reset rewrote older epochs",
    )

    prior_active = pre_epochs[-1]
    closed_prior = current_epochs[-2]
    closure_fields = {"closedAt", "closingReason", "excludedFromActiveAdmission", "status"}
    ensure(
        {
            key: value
            for key, value in closed_prior.items()
            if key not in closure_fields
        }
        == {
            key: value
            for key, value in prior_active.items()
            if key not in closure_fields
        },
        "post-promotion cost reset rewrote the prior active epoch",
    )
    ensure(
        prior_active.get("status") == "active"
        and closed_prior.get("status") == "closed"
        and closed_prior.get("excludedFromActiveAdmission") is True
        and isinstance(closed_prior.get("closedAt"), str)
        and isinstance(closed_prior.get("closingReason"), str),
        "post-promotion cost reset did not close the prior epoch",
    )

    active = current_epochs[-1]
    authorization_ref = active.get("authorizationRecord", {})
    authorization_path = Path(str(authorization_ref.get("path", "")))
    ensure(
        authorization_path.parent
        == Path("performance-tests/phase7_2-stabilization")
        and authorization_path.name.startswith("budget-reset-")
        and authorization_path.suffix == ".json",
        "post-promotion cost reset authorization path differs",
    )
    authorization = read_json(ROOT / authorization_path)
    ensure(
        authorization_ref.get("sha256")
        == file_sha256(ROOT / authorization_path),
        "post-promotion cost reset authorization hash differs",
    )

    attempt_ordinals = [attempt["ordinal"] for attempt in ledger["attempts"]]
    preserved = authorization.get("preservedHistory", {})
    ensure(
        preserved.get("attemptOrdinals") == attempt_ordinals
        and preserved.get("lifetimeChargedUpperBoundUsd")
        == pre_promotion["budget"]["lifetimeChargedUpperBoundUsd"]
        and preserved.get("previousActiveEpochUpperBoundUsd")
        == pre_promotion["budget"]["activeEpochAccruedUpperBoundUsd"]
        and preserved.get("excludedFromNewAdmission") is True
        and preserved.get("attemptEntriesRewritten") is False
        and preserved.get("ledgerHeadSha256") == ledger["ledgerHeadSha256"],
        "post-promotion cost reset history binding differs",
    )

    active_record = authorization.get("activeEpoch", {})
    ensure(
        active.get("status") == "active"
        and active.get("attemptOrdinals") == []
        and active.get("startsAfterAttemptOrdinal") == max(attempt_ordinals)
        and active.get("epochId") == active_record.get("epochId")
        and active.get("accruedUpperBoundUsd")
        == active_record.get("initialAccruedUpperBoundUsd")
        == "0.000000"
        and active.get("hardCapUsd") == active_record.get("hardCapUsd") == "60.000000"
        and active.get("newPaidWorkStopUsd")
        == active_record.get("newPaidWorkStopUsd")
        == "55.000000"
        and active.get("cleanupReserveUsd")
        == active_record.get("cleanupReserveUsd")
        == "5.000000"
        and active.get("effectivePaidBoundary")
        == authorization.get("effectivePaidBoundary")
        and active.get("effectivePaidBoundaryAt")
        == authorization.get("effectivePaidBoundaryAt")
        and active.get("userAuthorizationBasis")
        == authorization.get("authorizationBasis")
        and active.get("userAuthorizedAt") == authorization.get("recordedAt")
        and authorization.get("startsAfterAttemptOrdinal") == max(attempt_ordinals),
        "post-promotion active cost epoch differs from authorization",
    )

    boundary = authorization.get("boundaryEvidence", {})
    post_verification = evidence(
        Path(boundary["phase8PostPromotionVerification"]),
        boundary["phase8PostPromotionVerificationSha256"],
    )
    cleanup = evidence(
        Path(boundary["attempt23CleanupVerification"]),
        boundary["attempt23CleanupVerificationSha256"],
    )
    global_inventory = evidence(
        Path(boundary["globalInventory"]), boundary["globalInventorySha256"]
    )
    ensure(post_verification and cleanup and global_inventory, "cost reset evidence is absent")
    post_document = read_json(ROOT / Path(post_verification["path"]))
    ensure(
        post_document.get("passed") is True
        and post_document.get("awsRequests") == 0
        and post_document.get("paidAwsExperiment") is False
        and boundary.get("authoritativeServiceInventoryZero") is True
        and boundary.get("runIdTaggingApiResidualsZero") is True
        and boundary.get("sessionIdTaggingApiResidualsZero") is True
        and boundary.get("globalRunOwnedTaggingApiResidualsZero") is True
        and boundary.get("phase8AwsRequests") == 0
        and boundary.get("phase8PaidAwsExperiment") is False,
        "post-promotion cost reset boundary is not safe",
    )

    current_admission = authorization.get("currentAdmission", {})
    current_budget = ledger["budget"]
    ensure(
        current_budget.get("activeEpochId") == active["epochId"]
        and current_budget.get("activeEpochAccruedUpperBoundUsd")
        == current_admission.get("activeEpochAccruedUpperBoundUsd")
        == "0.000000"
        and current_budget.get("campaignChargedUpperBoundUsd") == "0.000000"
        and current_budget.get("remainingBeforeHardCapUsd")
        == current_admission.get("remainingBeforeHardCapUsd")
        == "60.000000"
        and current_budget.get("remainingOperationalBeforeReserveUsd")
        == current_admission.get("remainingOperationalBeforeReserveUsd")
        == "55.000000"
        and current_admission.get("newPaidWorkAuthorized") is False,
        "post-promotion reset budget does not start at zero",
    )


def validate_promoted_campaign(
    ledger: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    ensure(ledger.get("schemaVersion") == 1, "promoted ledger schema differs")
    ensure(
        ledger.get("campaign") == "phase7-2-stabilization",
        "promoted ledger campaign differs",
    )
    ensure(ledger.get("status") == "promoted", "campaign is not promoted")
    ensure(ledger.get("activeAttempt") is None, "promoted campaign retains an active attempt")
    ensure(ledger.get("blocker") is None, "promoted campaign retains a blocker")

    promotion = ledger.get("promotion")
    ensure(isinstance(promotion, dict), "promoted campaign has no promotion metadata")
    ensure(
        promotion.get("mode") == "composite-cleanup-recovered-user-authorized",
        "promotion mode differs",
    )
    ensure(promotion.get("phase") == "8", "promotion phase differs")
    ensure(promotion.get("phase5") == "skipped", "Phase 5 changed")
    ensure(
        promotion.get("evidenceBasisAttemptOrdinals") == [17, 23],
        "promotion evidence ordinals differ",
    )
    ensure(
        promotion.get("historicalAttemptVerdicts") == {"17": "failed", "23": "failed"},
        "promotion historical verdicts differ",
    )
    ensure(
        promotion.get("historicalAttemptVerdictsRewritten") is False,
        "promotion claims a historical verdict rewrite",
    )
    ensure(
        promotion.get("strictSingleAttemptPassClaimed") is False,
        "promotion claims a strict single-attempt pass",
    )
    ensure(
        promotion.get("cleanupRecoveredToZero") is True,
        "promotion cleanup recovery is unproven",
    )
    ensure(
        promotion.get("newPaidAwsWorkAuthorized") is False,
        "promotion authorizes new paid AWS work",
    )
    ensure(
        promotion.get("phase8PaidAwsExperimentOperationalUpperBoundUsd") == "0.000000",
        "promotion Phase 8 paid upper bound is nonzero",
    )

    finalization_commit = str(promotion.get("finalizationCommit", ""))
    ensure(git_is_ancestor(finalization_commit), "finalization commit is not an ancestor of HEAD")

    handoff_path = ROOT / HANDOFF
    manifest_path = ROOT / FINAL_DIR / "phase8-manifest.json"
    focused_path = ROOT / FINAL_DIR / "focused-verification.json"
    handoff_bytes = handoff_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    focused_bytes = focused_path.read_bytes()
    handoff = json.loads(handoff_bytes)
    manifest = json.loads(manifest_bytes)
    focused = json.loads(focused_bytes)
    handoff_canonical = validate_self_hash(handoff, "handoffSha256", "handoff")
    manifest_canonical = validate_self_hash(manifest, "manifestSha256", "manifest")

    handoff_ref = promotion.get("handoff", {})
    manifest_ref = promotion.get("manifest", {})
    focused_ref = promotion.get("focusedVerification", {})
    ensure(
        handoff_ref.get("path") == str(HANDOFF)
        and handoff_ref.get("fileSha256") == sha256_bytes(handoff_bytes)
        and handoff_ref.get("canonicalSha256") == handoff_canonical,
        "promotion handoff binding differs",
    )
    ensure(
        manifest_ref.get("path") == str(FINAL_DIR / "phase8-manifest.json")
        and manifest_ref.get("fileSha256") == sha256_bytes(manifest_bytes)
        and manifest_ref.get("canonicalSha256") == manifest_canonical,
        "promotion manifest binding differs",
    )
    ensure(
        focused_ref.get("path") == str(FINAL_DIR / "focused-verification.json")
        and focused_ref.get("sha256") == sha256_bytes(focused_bytes)
        and focused_ref.get("passed") is True
        and focused_ref.get("awsRequests") == 0
        and focused.get("passed") is True
        and focused.get("awsRequests") == 0,
        "promotion focused-verification binding differs",
    )
    ensure(
        handoff.get("phase8Execution", {}).get("paidAwsExperiment") is False
        and manifest.get("phase8PaidAwsExperimentOperationalUpperBoundUsd") == "0.000000",
        "Phase 8 paid AWS execution was enabled",
    )

    committed_handoff = git_blob(finalization_commit, HANDOFF)
    committed_manifest = git_blob(finalization_commit, FINAL_DIR / "phase8-manifest.json")
    committed_focused = git_blob(finalization_commit, FINAL_DIR / "focused-verification.json")
    ensure(committed_handoff == handoff_bytes, "handoff differs from finalization commit")
    ensure(committed_manifest == manifest_bytes, "manifest differs from finalization commit")
    ensure(committed_focused == focused_bytes, "focused verification differs from finalization commit")

    pre_promotion_bytes = git_blob(finalization_commit, LEDGER)
    pre_promotion_sha256 = sha256_bytes(pre_promotion_bytes)
    ensure(
        pre_promotion_sha256 == handoff["campaign"]["ledgerFileSha256AtHandoff"],
        "pre-promotion ledger does not match the handoff",
    )
    pre_promotion = json.loads(pre_promotion_bytes)
    accrued, epoch_id, _ = validate_campaign_ledger(pre_promotion)
    ensure(
        pre_promotion.get("status") == "stabilizing"
        and pre_promotion.get("activeAttempt") is None,
        "finalization commit does not contain the idle pre-promotion ledger",
    )
    ensure(
        handoff["campaign"]["statusAtHandoff"] == "stabilizing"
        and handoff["campaign"]["ledgerHeadSha256"] == pre_promotion["ledgerHeadSha256"]
        and handoff["campaign"]["activeEpochId"] == epoch_id
        and handoff["campaign"]["activeEpochAccruedUpperBoundUsd"]
        == format(accrued, ".6f"),
        "handoff campaign snapshot differs",
    )
    ensure(accrued <= Decimal("60"), "promoted active epoch exceeds 60 USD")

    allowed_top_level_changes = {
        "blocker",
        "budget",
        "budgetEpochs",
        "promotion",
        "status",
        "updatedAt",
    }
    pre_stable = {
        key: value
        for key, value in pre_promotion.items()
        if key not in allowed_top_level_changes
    }
    current_stable = {
        key: value
        for key, value in ledger.items()
        if key not in allowed_top_level_changes
    }
    ensure(current_stable == pre_stable, "promotion rewrote immutable campaign data")
    ensure(
        ledger.get("attempts") == pre_promotion.get("attempts")
        and ledger.get("ledgerHeadSha256") == pre_promotion.get("ledgerHeadSha256"),
        "promotion rewrote the attempt hash chain",
    )

    allowed_budget_changes = {
        "activeEpochAccruedUpperBoundUsd",
        "activeEpochId",
        "campaignChargedUpperBoundUsd",
        "newPaidWorkAuthorized",
        "nextScopedAttemptMaximumIncludingCleanupUsd",
        "nextScopedAttemptOperationalUpperBoundUsd",
        "remainingBeforeHardCapUsd",
        "remainingOperationalBeforeReserveUsd",
    }
    pre_budget = pre_promotion["budget"]
    current_budget = ledger["budget"]
    ensure(
        {
            key: value
            for key, value in current_budget.items()
            if key not in allowed_budget_changes
        }
        == {
            key: value
            for key, value in pre_budget.items()
            if key not in allowed_budget_changes
        },
        "promotion changed non-authorization budget state",
    )
    ensure(
        current_budget.get("newPaidWorkAuthorized") is False
        and current_budget.get("nextScopedAttemptMaximumIncludingCleanupUsd") is None
        and current_budget.get("nextScopedAttemptOperationalUpperBoundUsd") is None
        and current_budget.get("phase8PaidExperimentOperationalUpperBoundUsd") == "0.000000",
        "promoted budget is not fail-closed",
    )
    validate_post_promotion_cost_reset(pre_promotion, ledger)

    return pre_promotion, {
        "finalizationCommit": finalization_commit,
        "prePromotionLedgerFileSha256": pre_promotion_sha256,
        "currentLedgerFileSha256": file_sha256(ROOT / LEDGER),
        "promotionMode": promotion["mode"],
        "checks": {
            "promotedLedgerState": True,
            "prePromotionLedgerCommitAnchor": True,
            "attemptHashChainUnchangedAfterPromotion": True,
            "promotionOnlyChangedCampaignControlState": True,
            "promotedBudgetFailClosed": True,
            "finalizationCommitIsAncestor": True,
            "finalizationCommitArtifactsExact": True,
            "promotionArtifactHashes": True,
            "focusedUnpaidEvidenceBound": True,
            "postPromotionCostResetAuthorized": True,
        },
    }


def build_handoff(context: dict[str, Any], generated_at: str) -> dict[str, Any]:
    fresh = context["fresh"]
    handoff = {
        "schemaVersion": 1,
        "recordType": "phase7-2-composite-phase8-handoff",
        "generatedAt": generated_at,
        "phase": "7-2-to-8",
        "phase5": "skipped",
        "promotionMode": "composite-cleanup-recovered-user-authorized",
        "phase8EntryAuthorized": True,
        "strictSingleAttemptPassClaimed": False,
        "historicalAttemptVerdictsRewritten": False,
        "userAcceptancePolicy": {
            "base": evidence(POLICY),
            "cleanupRecoveredAmendment": evidence(AMENDMENT),
            "decision": context["policy"]["decision"],
        },
        "campaign": {
            "ledgerPath": str(LEDGER),
            "ledgerFileSha256AtHandoff": file_sha256(ROOT / LEDGER),
            "ledgerHeadSha256": context["ledger"]["ledgerHeadSha256"],
            "statusAtHandoff": context["ledger"]["status"],
            "activeAttempt": None,
            "activeEpochId": context["epochId"],
            "activeEpochAccruedUpperBoundUsd": context["accrued"],
            "hardCapUsd": "60.000000",
            "phase8PaidAwsExperimentOperationalUpperBoundUsd": "0.000000",
        },
        "promotionBasis": {
            "performanceCorrectnessAndRecovery": context["performance"],
            "freshMinimalSmokeAndArchive": {
                "attemptOrdinal": fresh["ordinal"],
                "runId": fresh["runId"],
                "sessionId": fresh["sessionId"],
                "attemptType": fresh["attemptType"],
                "runnerVerdict": fresh["verdict"],
                "runnerFirstFailingGate": fresh["firstFailingGate"],
                "runnerVerdictRewritten": False,
                "functionalAcceptance": "passed",
                "cleanupRecoveryAcceptance": "passed",
                "promotionEligible": False,
                "immutableEntryAnchor": context["freshEntryAnchor"],
                "immutableSourceAnchor": context["sourceAnchor"],
                "gitCommit": fresh["gitCommit"],
                "gitTree": fresh["implementationGitTree"],
                "implementationSourceClosureSha256": fresh[
                    "implementationSourceClosureSha256"
                ],
                "imageSourceHashes": fresh["imageSourceHashes"],
                "imageDigests": fresh["imageDigests"],
                "minimalSmoke": {"passed": True, "evidence": context["refs"]["deployment"]},
                "archive": {"passed": True, **context["archive"], "evidence": context["refs"]["archive"]},
                "cleanup": {
                    "allZero": True,
                    "serviceClassCount": len(context["cleanup"]["counts"]),
                    "evidence": context["refs"]["cleanup"],
                    "globalInventory": context["refs"]["globalInventory"],
                },
                "runnerEvidence": {
                    "run": context["refs"]["run"],
                    "execution": context["refs"]["execution"],
                },
            },
        },
        "acceptance": {
            "minimalSmoke": True,
            "correctness1002Inherited": True,
            "replacement900Inherited": True,
            "score50kInheritedWithoutRerun": True,
            "freshArchive15M": True,
            "freshArchiveEquivalence": True,
            "freshSourceRetentionAndNoDrop": True,
            "freshCode241Zero": True,
            "cleanupRecoveredToZero": True,
            "globalRunOwnedInventoryZero": True,
            "withinActiveEpoch60UsdCap": True,
            "phase5Skipped": True,
        },
        "phase8Execution": {
            "paidAwsExperiment": False,
            "awsMutationByDefault": False,
            "repeatWarmupScoreOrArchive": False,
            "verificationScope": context["policy"]["phase8"]["verificationScope"],
        },
    }
    handoff["handoffSha256"] = canonical_sha256(handoff)
    return handoff


def markdown_escape(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "none").split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_documents(context: dict[str, Any], handoff: dict[str, Any]) -> dict[str, str]:
    performance = context["policy"]["inheritedPerformanceEvidence"]
    archive = context["archive"]
    acceptance = """# Phase 8 acceptance summary

This baseline combines immutable Attempt 17 performance evidence with Attempt 23's fresh
minimal-smoke/archive evidence. Attempt 17 and Attempt 23 both remain `failed`; no historical
verdict is rewritten. Attempt 23 is accepted only as cleanup-recovered composite evidence because
all functional stages passed and final authoritative/global cleanup inventories are zero.

| Gate | Required | Measured | Result | Evidence |
|---|---:|---:|---|---|
| Correctness | 1,002 | {correctness:,} | passed | [{correctness_path}](../../{correctness_path}) |
| Consumer replacement | 900 | {replacement:,} | passed | [{correctness_path}](../../{correctness_path}) |
| Scored completions | 15,000,000 | {completed:,} | passed | [{score_path}](../../{score_path}) |
| Actual scored RPS | >=49,500 | {rps:.6f} | passed | [{score_path}](../../{score_path}) |
| Transport / 429 / 5xx | 0 / 0 / 0 | {transport} / {http429} / {http5xx} | passed | [{score_path}](../../{score_path}) |
| Corrected p95 | <300 ms | {p95:.6f} ms | passed | [{score_path}](../../{score_path}) |
| Fresh minimal smoke | all services + TLS + ClickHouse | passed | passed | [{deploy}](../../{deploy}) |
| Fresh archive rows | 15,000,000 | {archive_rows:,} | passed | [{archive_path}](../../{archive_path}) |
| Parquet objects | 3 x 5,000,000 | 3 x 5,000,000 | passed | [{archive_path}](../../{archive_path}) |
| COMMITTED/equivalence | immutable re-read, all differences 0 | all checks true | passed | [{archive_path}](../../{archive_path}) |
| Source retention | 15,000,000 rows, no DROP | {source_rows:,} rows, no DROP | passed | [{archive_path}](../../{archive_path}) |
| Query failures | Code 241 = 0 | 0 | passed | [{archive_path}](../../{archive_path}) |
| Final cleanup | 35 service classes 0, tag residuals 0 | 0 / 0 | passed | [{cleanup}](../../{cleanup}) |
| Global Phase 7 run-owned tags | 0 | 0 | passed | [{global_inventory}](../../{global_inventory}) |
| Active cost epoch | <=$60 | ${accrued} | passed | [{ledger}](../../{ledger}) |
| Phase 8 paid AWS work | $0 | $0 | passed | [{handoff}](../../{handoff}) |

Known deviations remain visible: Attempt 17 warmup had one timeout, and Attempt 23's first cleanup
inventory command observed an immutable stopped-task tag tombstone before the second recovery reached
zero. Neither deviation is rewritten into a strict single-attempt pass. Phase 5 remains `skipped`.
""".format(
        correctness=performance["correctness"]["correctnessInputRecords"],
        replacement=performance["correctness"]["replacementOffered"],
        correctness_path=performance["correctness"]["path"],
        completed=performance["score"]["completedRequests"],
        rps=performance["score"]["actualRps"],
        transport=performance["score"]["transportErrors"],
        http429=performance["score"]["http429"],
        http5xx=performance["score"]["http5xx"],
        p95=performance["score"]["correctedP95Ms"],
        score_path=performance["score"]["path"],
        deploy=context["refs"]["deployment"]["path"],
        archive_rows=archive["rows"],
        archive_path=context["refs"]["archive"]["path"],
        source_rows=archive["sourceRowsAfter"],
        cleanup=context["refs"]["cleanup"]["path"],
        global_inventory=context["refs"]["globalInventory"]["path"],
        accrued=context["accrued"],
        ledger=LEDGER,
        handoff=HANDOFF,
    )

    instances = context["deployment"]["instanceContract"]["instances"]
    topology_rows = []
    for role, values in instances.items():
        types = ", ".join(sorted({item["instanceType"] for item in values}))
        amis = ", ".join(sorted({item["imageId"] for item in values}))
        topology_rows.append(f"| {role} | {len(values)} | {types} | {amis} |")
    operations = """# Phase 8 operations handoff

## Certified topology

The final baseline uses the standard `LoopAdPerfPhase7IntegrationImageStack` and
`LoopAdPerfPhase7IntegrationStack`; no targeted-only stack is part of the baseline.

| Role | Hosts | Instance type | AMI |
|---|---:|---|---|
{topology}

- Region/account: `ap-northeast-2` / `742711170910`.
- Kinesis: 120 shards, verified in the fresh deployment evidence.
- ClickHouse: one `r7g.2xlarge`, encrypted 500 GiB gp3 (3,000 IOPS, 500 MiB/s), container 8 GiB,
  server 7 GiB, archive query operational envelope up to 6.5 GiB with retained reserve.
- Collector, consumer and archive images are digest-pinned in `phase8-manifest.json`.
- All instances require IMDSv2 and have no public IP.

## Deployment and readiness

Use fresh run/session identifiers and exact run-owned repositories. Verify identity, region, source,
image digests, ownership, absent stack, price/cost admission and prepared preflight before deploy.
Deploy each immutable Run ID at most once. Readiness requires stack `CREATE_COMPLETE`, exact hosts and
services, TLS/protocol health, Kinesis 120 shards, ClickHouse `SELECT 1` and the expected schema.

## Observability

Use HAProxy/collector/Kinesis/KCL/ECS/EC2/ClickHouse/CloudWatch/CloudTrail evidence from the immutable
run directory. The 1.1 GiB `metrics-summary.json` remains local raw evidence and is anchored by SHA-256
`{metrics_sha}` in Attempt 23's ledger entry; it is intentionally not duplicated into Git.

## Cleanup and recovery

Delete the runtime stack, exact run-owned ECR images/repositories and image stack in that order. Then
verify all 35 service classes are zero and both exact and global Tagging API inventories are empty.
Stopped ECS tasks can remain immutable tag tombstones for about one hour. Poll them; do not redeploy or
misclassify live cost while waiting. A nonzero intermediate inventory is not terminal if later exact
recovery reaches authoritative zero, but the intermediate failure evidence must remain visible.

## Known limits and hard stops

- Performance acceptance remains >=49,500 actual RPS, zero transport/429/5xx and corrected p95 <300 ms.
- Query-memory settings are operational safety envelopes, not exact equality tests; preserve server and
  container reserve and keep Code 241 at zero.
- Never execute source DROP without immutable COMMITTED re-read and exact bidirectional equivalence.
- Identity, ownership, source/image hash, correctness/accounting, data-loss suspicion, budget and final
  cleanup-zero failures remain hard stops.
- Phase 8 performs no paid AWS experiment by default. Phase 5 remains `skipped`.
""".format(
        topology="\n".join(topology_rows),
        metrics_sha=context["fresh"]["terminalEvidenceHashes"]["metrics"]["sha256"],
    )

    history_lines = [
        "# Phase 7-2 stabilization failure history",
        "",
        "Historical verdicts are immutable. A later composite promotion does not rewrite them.",
        "",
        "| # | Run ID | Verdict | First gate | Root cause | Fix / disposition |",
        "|---:|---|---|---|---|---|",
    ]
    for item in context["ledger"]["attempts"]:
        diagnosis = item.get("failure", {}).get("diagnosis") or item.get("failure", {}).get("rawAwsError")
        fix = item.get("fix", {}).get("description")
        if item.get("ordinal") == 22 and not fix:
            fix = "Validate the local collector Git repository and pinned commit before any paid marker or AWS resource."
        if item.get("ordinal") == 23:
            fix = (
                "No paid rerun: functional stages and archive passed; recovery cleanup reached zero. "
                "Composite Phase 8 amendment preserves the failed runner verdict and records the cleanup-bookkeeping defect."
            )
        history_lines.append(
            "| {ordinal} | `{run}` | `{verdict}` | `{gate}` | {diagnosis} | {fix} |".format(
                ordinal=item["ordinal"],
                run=item["runId"],
                verdict=item["verdict"],
                gate=item.get("firstFailingGate") or "pre-ledger/none",
                diagnosis=markdown_escape(diagnosis),
                fix=markdown_escape(fix),
            )
        )
    history_lines.extend(
        [
            "",
            "Attempt 1 is the required EC2 user-data regression: decoded LaunchTemplate user data was",
            "17,244 bytes, exceeding the 16,384-byte EC2 limit by 860 bytes. The later synth-time gate",
            "decodes every LaunchTemplate UserData value and retains a 15,360-byte load-generator margin.",
            "Phase 5 remained `skipped` throughout.",
        ]
    )
    return {
        "acceptance-summary.md": acceptance,
        "operations-handoff.md": operations,
        "failure-history.md": "\n".join(history_lines),
    }


def build_manifest(
    context: dict[str, Any],
    handoff: dict[str, Any],
    generated_at: str,
    document_refs: dict[str, dict[str, str]],
) -> dict[str, Any]:
    images = read_json(ROOT / ATTEMPT_23_READINESS / "image-manifest.json")["images"]
    preflight = read_json(ROOT / ATTEMPT_23_READINESS / "preflight-prepared.json")
    instances = context["deployment"]["instanceContract"]["instances"]
    topology = {
        role: {
            "hostCount": len(values),
            "instanceTypes": sorted({item["instanceType"] for item in values}),
            "imageIds": sorted({item["imageId"] for item in values}),
        }
        for role, values in sorted(instances.items())
    }
    manifest = {
        "schemaVersion": 1,
        "recordType": "phase8-final-integration-baseline",
        "generatedAt": generated_at,
        "phase": "8",
        "phase5": "skipped",
        "promotionMode": handoff["promotionMode"],
        "strictSingleAttemptPassClaimed": False,
        "handoff": evidence(HANDOFF),
        "promotedSource": {
            "gitCommit": context["fresh"]["gitCommit"],
            "gitTree": context["fresh"]["implementationGitTree"],
            "implementationSourceClosureSha256": context["fresh"][
                "implementationSourceClosureSha256"
            ],
            "sourceSeal": context["sourceAnchor"],
        },
        "configuration": {
            "region": "ap-northeast-2",
            "account": "742711170910",
            "stacks": [
                "LoopAdPerfPhase7IntegrationImageStack",
                "LoopAdPerfPhase7IntegrationStack",
            ],
            "amis": preflight["snapshot"]["amis"],
            "topology": topology,
            "kinesisShards": context["deployment"]["stream"]["openShardCount"],
            "clickHouseVolume": context["deployment"]["instanceContract"]["clickHouseVolume"],
        },
        "containerImages": images,
        "evidenceBasis": handoff["promotionBasis"],
        "acceptance": handoff["acceptance"],
        "cleanup": {
            "serviceInventoryZero": True,
            "taggingApiResidualsZero": True,
            "globalRunOwnedInventoryZero": True,
            "evidence": context["refs"]["cleanup"],
            "globalEvidence": context["refs"]["globalInventory"],
        },
        "campaignCost": handoff["campaign"],
        "phase8PaidAwsExperimentOperationalUpperBoundUsd": "0.000000",
        "documents": document_refs,
        "rawMetrics": {
            "path": context["fresh"]["terminalEvidenceHashes"]["metrics"]["path"],
            "sha256": context["fresh"]["terminalEvidenceHashes"]["metrics"]["sha256"],
            "gitCommitted": False,
            "reason": "1.1 GiB immutable local raw evidence; hash is ledger-anchored",
        },
    }
    manifest["manifestSha256"] = canonical_sha256(manifest)
    return manifest


def verify_created_outputs(
    promotion_verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    handoff = read_json(ROOT / HANDOFF)
    expected_handoff_hash = handoff.pop("handoffSha256")
    ensure(canonical_sha256(handoff) == expected_handoff_hash, "handoff self-hash differs")
    handoff["handoffSha256"] = expected_handoff_hash
    manifest_path = ROOT / FINAL_DIR / "phase8-manifest.json"
    manifest = read_json(manifest_path)
    expected_manifest_hash = manifest.pop("manifestSha256")
    ensure(canonical_sha256(manifest) == expected_manifest_hash, "manifest self-hash differs")
    manifest["manifestSha256"] = expected_manifest_hash
    for reference in manifest["documents"].values():
        evidence(Path(reference["path"]), reference["sha256"])
    checks = {
        "ledgerHashChain": True,
        "historicalVerdictsPreserved": True,
        "attempt17PerformanceEvidence": True,
        "attempt23FunctionalStages": True,
        "attempt23Archive15M": True,
        "attempt23CleanupRecoveredZero": True,
        "globalRunOwnedInventoryZero": True,
        "sourceClosureRevalidated": True,
        "handoffSelfHash": True,
        "manifestSelfHash": True,
        "documentsHashBound": True,
        "phase8PaidAwsUpperBoundZero": True,
        "phase5Skipped": True,
    }
    if promotion_verification is not None:
        checks.update(promotion_verification["checks"])
    result = {
        "schemaVersion": 1,
        "recordType": (
            "phase8-post-promotion-verification"
            if promotion_verification is not None
            else "phase8-focused-unpaid-verification"
        ),
        "verifiedAt": utc_now(),
        "awsRequests": 0,
        "paidAwsExperiment": False,
        "handoffSha256": expected_handoff_hash,
        "manifestSha256": expected_manifest_hash,
        "checks": checks,
        "passed": True,
    }
    if promotion_verification is not None:
        result["promotion"] = {
            key: value
            for key, value in promotion_verification.items()
            if key != "checks"
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-root", type=Path, default=ROOT)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--verification-output", type=Path)
    args = parser.parse_args()
    ensure(args.infra_root.resolve() == ROOT, "finalizer must run against its owning checkout")
    if args.verify_only:
        current_ledger = read_json(ROOT / LEDGER)
        promotion_verification = None
        if current_ledger.get("status") == "promoted":
            pre_promotion, promotion_verification = validate_promoted_campaign(
                current_ledger
            )
            validate_inputs(pre_promotion)
        else:
            validate_inputs(current_ledger)
        verification = verify_created_outputs(promotion_verification)
        if args.verification_output is not None:
            output = (ROOT / args.verification_output).resolve()
            output.relative_to((ROOT / FINAL_DIR).resolve())
            write_json(output, verification)
        print(json.dumps(verification, indent=2, sort_keys=True))
        return 0
    ensure(
        args.verification_output is None,
        "--verification-output is valid only with --verify-only",
    )
    context = validate_inputs()
    existing_handoff = ROOT / HANDOFF
    generated_at = (
        read_json(existing_handoff)["generatedAt"]
        if existing_handoff.is_file()
        else utc_now()
    )
    handoff = build_handoff(context, generated_at)
    if existing_handoff.is_file():
        ensure(read_json(existing_handoff) == handoff, "existing handoff differs on resume")
    else:
        write_json(existing_handoff, handoff)
    documents = build_documents(context, handoff)
    for name, content in documents.items():
        output = ROOT / FINAL_DIR / name
        if output.is_file():
            ensure(
                output.read_text(encoding="utf-8") == content.rstrip() + "\n",
                f"existing Phase 8 document differs on resume: {name}",
            )
        else:
            write_text(output, content)
    document_refs = {
        name.removesuffix(".md").replace("-", "_"): evidence(FINAL_DIR / name)
        for name in documents
    }
    manifest = build_manifest(context, handoff, generated_at, document_refs)
    write_json(ROOT / FINAL_DIR / "phase8-manifest.json", manifest)
    verification = verify_created_outputs()
    write_json(ROOT / FINAL_DIR / "focused-verification.json", verification)
    print(json.dumps(verification, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

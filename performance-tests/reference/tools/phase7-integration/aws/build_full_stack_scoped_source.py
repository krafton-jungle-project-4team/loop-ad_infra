#!/usr/bin/env python3
"""Seal current full-stack sources for one promotion-ineligible scoped diagnostic."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from common import (
    IMAGE_STACK_NAME,
    RUNTIME_STACK_NAME,
    SCOPED_BASELINE_PATH,
    SCOPED_BASELINE_SHA256,
    SCOPED_FOCUSED_GATE_PATHS,
    SCOPED_POLICY_PATH,
    SCOPED_POLICY_SHA256,
    SCOPED_PROMOTION_POLICY_PATH,
    SCOPED_PROMOTION_POLICY_SHA256,
    SCOPED_STAGE_PLAN,
    SCOPED_ZERO_ATTEMPT_STAGES,
    file_sha256,
    read_json,
    utc_now,
    write_json,
)


EXTRA_IMPLEMENTATION_FILES = (
    "performance-tests/phase7-integration/archive/targeted_seed.py",
    "performance-tests/phase7-integration/aws/build_full_stack_scoped_source.py",
    "performance-tests/phase7-integration/aws/build_phase8_composite_handoff.py",
    "performance-tests/phase7-integration/aws/full_stack_scoped_archive.py",
    "performance-tests/phase7-integration/aws/full_stack_scoped_cost_model.py",
    "performance-tests/phase7-integration/aws/seal_full_stack_scoped_attempt.py",
    "performance-tests/phase7-integration/tests/test_full_stack_scoped_archive.py",
    "performance-tests/phase7-integration/tests/test_runner.py",
)


def focused_gate_passes(
    relative: str,
    document: dict[str, Any],
) -> bool:
    if document.get("schemaVersion") != 1:
        return False
    if relative.endswith("attempt-17-fix-verification.json"):
        return (
            document.get("attemptOrdinal") == 17
            and document.get("sourceRunId")
            == "run_20260719_043415_phase7_integration"
            and document.get("status")
            == "focused-fix-passed-awaiting-targeted-aws"
            and document.get("sourceFailure", {}).get("firstFailingGate")
            == "score_archive"
            and document.get("focusedVerification", {}).get("status") == "passed"
        )
    if relative.endswith("attempt-19-fix-verification.json"):
        return (
            document.get("attemptOrdinal") == 19
            and document.get("runId")
            == "run_20260719_083356_phase7_archive_diagnostic"
            and document.get("failure", {}).get("firstFailingGate")
            == "runtime-deploy-readiness"
            and document.get("verification", {}).get("status") == "passed"
            and document.get("verification", {}).get("awsRequests") == 0
        )
    if relative.endswith("attempt-20-fix-verification.json"):
        memory = document.get("changes", {}).get("archiveQueryMemory", {})
        cleanup = document.get("cleanup", {})
        return (
            document.get("recordType") == "focused-fix-verification"
            and document.get("attemptOrdinal") == 20
            and document.get("sourceRunId")
            == "run_20260719_125547_phase7_integration"
            and document.get("status") == "passed"
            and memory.get("currentBytes", 0) >= 6 * 1024**3
            and memory.get("serverBytes", 0) - memory.get("currentBytes", 0)
            >= 512 * 1024**2
            and memory.get("exactPointAcceptance") is False
            and document.get("focusedTests", {}).get("python", {}).get("failed") == 0
            and document.get("focusedTests", {}).get("cdkJest", {}).get("failed") == 0
            and document.get("exactContextSynth", {}).get(
                "allDecodedUserDataAtOrBelowEc2Limit"
            ) is True
            and document.get("exactContextSynth", {}).get(
                "loadGeneratorAtOrBelow15360Bytes"
            ) is True
            and document.get("cfnLint", {}).get("unexpectedFindings") == 0
            and cleanup.get("attemptFinalInventory", {}).get("allZero") is True
            and cleanup.get("globalInventory", {}).get("allZero") is True
        )
    if relative.endswith("attempt-21-fix-verification.json"):
        memory = document.get("changes", {}).get("archiveQueryMemory", {})
        cleanup = document.get("cleanup", {})
        cost = document.get("cost", {})
        return (
            document.get("recordType") == "focused-fix-verification"
            and document.get("attemptOrdinal") == 21
            and document.get("sourceRunId")
            == "run_20260719_144544_phase7_integration"
            and document.get("status") == "passed"
            and document.get("failure", {}).get("firstFailingGate") == "archive"
            and document.get("failure", {}).get("sourceDropExecuted") is False
            and memory.get("currentBytes", 0) >= 6 * 1024**3
            and memory.get("validatorMaximumBytes", 0) >= memory.get(
                "currentBytes", 0
            )
            and memory.get("serverBytes", 0) - memory.get("currentBytes", 0)
            >= 512 * 1024**2
            and memory.get("exactPointAcceptance") is False
            and document.get("changes", {})
            .get("cleanup", {})
            .get("stoppedTaskUntagUnsupported")
            is True
            and document.get("focusedTests", {}).get("python", {}).get("failed")
            == 0
            and cleanup.get("attemptFinalInventory", {}).get("allZero") is True
            and cleanup.get("globalInventory", {}).get("allZero") is True
            and cost.get("passed") is True
            and float(cost.get("nextRetryActiveOperationalMaximumUsd", "inf"))
            < float(cost.get("newPaidWorkStopUsd", "-inf"))
            and float(cost.get("nextRetryMaximumIncludingCleanupUsd", "inf"))
            <= float(cost.get("hardCapUsd", "-inf"))
        )
    if relative.endswith("attempt-22-fix-verification.json"):
        collector = document.get("collectorSource", {})
        cleanup = document.get("cleanup", {})
        return (
            document.get("recordType") == "focused-fix-verification"
            and document.get("attemptOrdinal") == 22
            and document.get("sourceRunId")
            == "run_20260719_162701_phase7_integration"
            and document.get("status") == "passed"
            and document.get("failure", {}).get("firstFailingGate")
            == "image-preparation"
            and document.get("failure", {}).get("sourceDropExecuted") is False
            and collector.get("localRepository") == "../loop-ad_event_collector"
            and collector.get("pinnedCommit")
            == "497315137251af82d0d203ce34702d5543553942"
            and collector.get("pinnedCommitPresent") is True
            and collector.get("validatedBeforePaidBoundary") is True
            and collector.get("cliHelpDistinguishesLocalGitFromEcr") is True
            and document.get("focusedTests", {}).get("python", {}).get("failed")
            == 0
            and cleanup.get("attemptFinalInventory", {}).get("allZero") is True
            and cleanup.get("globalInventory", {}).get("allZero") is True
        )
    if relative.endswith("full-stack-scoped-tooling-verification-20260719.json"):
        contract = document.get("stackContract", {})
        recorded_files = document.get("verifiedImplementationFiles", [])
        recorded_tree = hashlib.sha256()
        recorded_manifest_valid = isinstance(recorded_files, list) and bool(recorded_files)
        if recorded_manifest_valid:
            for item in recorded_files:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("path"), str)
                    or not isinstance(item.get("sha256"), str)
                ):
                    recorded_manifest_valid = False
                    break
                recorded_tree.update(item["path"].encode("utf-8"))
                recorded_tree.update(b"\0")
                recorded_tree.update(item["sha256"].encode("ascii"))
                recorded_tree.update(b"\n")
        return (
            document.get("recordType") == "focused-fix-verification"
            and document.get("status") == "passed"
            and contract.get("imageStack") == IMAGE_STACK_NAME
            and contract.get("runtimeStack") == RUNTIME_STACK_NAME
            and contract.get("dedicatedDiagnosticStackReferencedByExecution") is False
            and contract.get("zeroAttemptStages") == SCOPED_ZERO_ATTEMPT_STAGES
            and document.get("focusedTests", {}).get("python", {}).get("failed") == 0
            and document.get("cfnLint", {}).get("unexpectedFindings") == 0
            and document.get("exactSynth", {}).get(
                "allDecodedUserDataAtOrBelowEc2Limit"
            ) is True
            and document.get("exactSynth", {}).get(
                "loadGeneratorAtOrBelow15360Bytes"
            ) is True
            and recorded_manifest_valid
            and document.get("verifiedImplementationTreeSha256")
            == recorded_tree.hexdigest()
        )
    return False


def git_output(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def implementation_manifest(root: Path, paths: list[str]) -> tuple[list[dict[str, str]], str]:
    canonical = sorted(set(paths))
    if not canonical or any(Path(path).is_absolute() or ".." in Path(path).parts for path in canonical):
        raise RuntimeError("implementation paths must be nonempty repository-relative paths")
    dirty = git_output(root, "status", "--short", "--", *canonical)
    if dirty:
        raise RuntimeError(f"scoped diagnostic implementation paths are not committed:\n{dirty}")
    combined = hashlib.sha256()
    entries: list[dict[str, str]] = []
    for relative in canonical:
        path = (root / relative).resolve()
        path.relative_to(root)
        if not path.is_file():
            raise FileNotFoundError(f"implementation source is missing: {relative}")
        digest = file_sha256(path)
        entries.append({"path": relative, "sha256": digest})
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\n")
    return entries, combined.hexdigest()


def build(
    root: Path,
    baseline_handoff: Path,
    focused_gate_paths: list[Path],
    policy_path: Path,
) -> dict[str, Any]:
    root = root.resolve()
    expected_baseline = (root / SCOPED_BASELINE_PATH).resolve()
    expected_policy = (root / SCOPED_POLICY_PATH).resolve()
    expected_promotion_policy = (root / SCOPED_PROMOTION_POLICY_PATH).resolve()
    if (
        baseline_handoff.resolve() != expected_baseline
        or file_sha256(expected_baseline) != SCOPED_BASELINE_SHA256
    ):
        raise RuntimeError("baseline handoff path or hash is not the pinned Attempt 17 input")
    if (
        policy_path.resolve() != expected_policy
        or file_sha256(expected_policy) != SCOPED_POLICY_SHA256
    ):
        raise RuntimeError("scoped diagnostic policy path or hash is not pinned")
    if (
        not expected_promotion_policy.is_file()
        or file_sha256(expected_promotion_policy)
        != SCOPED_PROMOTION_POLICY_SHA256
    ):
        raise RuntimeError("composite Phase 8 promotion policy path or hash is not pinned")
    baseline = read_json(baseline_handoff.resolve())
    if baseline.get("finalVerdict") != "passed" or baseline.get("awsReady") is not True:
        raise RuntimeError("baseline handoff must be the passed pre-Attempt 17 handoff")
    baseline_files = baseline.get("implementationFiles")
    if not isinstance(baseline_files, list) or not baseline_files:
        raise RuntimeError("baseline handoff implementation manifest is missing")
    paths = [str(item.get("path", "")) for item in baseline_files if isinstance(item, dict)]
    paths.extend(EXTRA_IMPLEMENTATION_FILES)
    manifest, tree_sha = implementation_manifest(root, paths)

    resolved_gate_paths = {
        str(path.resolve().relative_to(root)) for path in focused_gate_paths
    }
    if resolved_gate_paths != SCOPED_FOCUSED_GATE_PATHS:
        raise RuntimeError("focused verification path set is not exact")
    provenance_paths = [
        SCOPED_BASELINE_PATH,
        SCOPED_POLICY_PATH,
        SCOPED_PROMOTION_POLICY_PATH,
        *sorted(SCOPED_FOCUSED_GATE_PATHS),
    ]
    dirty_provenance = git_output(root, "status", "--short", "--", *provenance_paths)
    if dirty_provenance:
        raise RuntimeError(
            f"scoped diagnostic provenance is not committed:\n{dirty_provenance}"
        )

    focused_gates = []
    for gate_path in focused_gate_paths:
        resolved = gate_path.resolve()
        document = read_json(resolved)
        relative = str(resolved.relative_to(root))
        if not focused_gate_passes(relative, document):
            raise RuntimeError(f"focused gate is not passed: {resolved}")
        focused_gates.append({
            "path": relative,
            "sha256": file_sha256(resolved),
            "status": "passed",
        })
    if not focused_gates:
        raise RuntimeError("at least one immutable focused verification is required")

    policy = read_json(policy_path.resolve())
    if (
        policy.get("decision")
        != "retire-dedicated-targeted-stack-use-attempt17-full-stack-definition"
    ):
        raise RuntimeError("full-stack scoped diagnostic policy is not exact")
    return {
        "schemaVersion": 1,
        "recordType": "phase7-full-stack-scoped-diagnostic-source",
        "generatedAt": utc_now(),
        "phase": "7-2",
        "phase5": "skipped",
        "attemptType": "aws-full-stack-scoped-diagnostic",
        "stackDefinitions": [IMAGE_STACK_NAME, RUNTIME_STACK_NAME],
        "topologyBaseline": "Attempt 17",
        "promotionEligible": False,
        "awsDiagnosticReady": True,
        "gitCommit": git_output(root, "rev-parse", "HEAD"),
        "gitTree": git_output(root, "rev-parse", "HEAD^{tree}"),
        "baselineHandoff": {
            "path": str(baseline_handoff.resolve().relative_to(root)),
            "sha256": file_sha256(baseline_handoff.resolve()),
            "implementationTreeSha256": baseline.get("implementationTreeSha256"),
            "scope": "path-list baseline only; this document does not claim a fresh whole-local pass",
        },
        "policy": {
            "path": str(policy_path.resolve().relative_to(root)),
            "sha256": file_sha256(policy_path.resolve()),
        },
        "compositePromotionPolicy": {
            "path": SCOPED_PROMOTION_POLICY_PATH,
            "sha256": file_sha256(expected_promotion_policy),
        },
        "focusedGates": focused_gates,
        "implementationFiles": manifest,
        "implementationTreeSha256": tree_sha,
        "unresolvedFailures": [],
        "deferredWholeLocal": {
            "required": False,
            "status": "superseded-by-user-authorized-composite-phase8-policy",
            "coverage": [],
        },
        "stagePlan": SCOPED_STAGE_PLAN,
        "zeroAttemptStages": SCOPED_ZERO_ATTEMPT_STAGES,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-root", required=True, type=Path)
    parser.add_argument("--baseline-handoff", required=True, type=Path)
    parser.add_argument("--focused-gate", action="append", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("scoped diagnostic source seal is immutable")
    result = build(
        args.infra_root,
        args.baseline_handoff,
        args.focused_gate,
        args.policy,
    )
    write_json(args.output, result)
    print(json.dumps({
        "gitCommit": result["gitCommit"],
        "implementationTreeSha256": result["implementationTreeSha256"],
        "implementationFileCount": len(result["implementationFiles"]),
        "focusedGateCount": len(result["focusedGates"]),
        "promotionEligible": result["promotionEligible"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

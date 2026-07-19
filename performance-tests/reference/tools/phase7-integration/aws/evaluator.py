#!/usr/bin/env python3
"""Pure final evaluator for Phase 7-2 correctness, performance, cost, and cleanup evidence."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

from common import read_json, utc_now, write_json
from cost_model import CLEANUP_RESERVE_USD, HARD_CAP_USD, NEW_LOAD_STOP_USD
from evidence_assembler import required_artifacts_are_valid


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_SCORE_RECORDS = 15_000_000


def evaluate(evidence: dict[str, Any], run_dir: Path | None = None) -> dict[str, Any]:
    identity_mode = evidence.get("identityMode")
    diagnostic_identity = identity_mode == "balanced-pool-sampled-with-replacement"
    if identity_mode not in {
        "globally-unique-event-id",
        "balanced-pool-sampled-with-replacement",
    }:
        diagnostic_identity = False
    performance = evidence.get("performance", {})
    counts = evidence.get("counts", {})
    failures = evidence.get("failures", {})
    drain = evidence.get("drain", {})
    archive = evidence.get("archive", {})
    resources = evidence.get("resources", {})
    haproxy = evidence.get("haproxy", {})
    cloudtrail = evidence.get("cloudTrail", {})
    cost = evidence.get("cost", {})
    cleanup = evidence.get("cleanup", {})
    execution = evidence.get("execution", {})
    host_roles = ("collector", "consumer", "clickHouse")
    identity_contract = evidence.get("identityContract", {})
    exact_pipeline_counts = [
        integer(counts.get(name), -1)
        for name in (
            "http202", "collectorFinalAck", "kinesisAccepted",
            "kclProcessed", "clickHouseInserted",
        )
    ]
    diagnostic_count = exact_pipeline_counts[0]
    if diagnostic_identity:
        score_count_invariant = (
            diagnostic_count > 0
            and all(value == diagnostic_count for value in exact_pipeline_counts)
            and integer(counts.get("fixturePoolRows"), -1) == 480
            and integer(counts.get("clickHouseLiveUnique"), -1) == 480
        )
        identity_contract_valid = (
            identity_contract.get("predeclaredBeforeDeploy") is True
            and identity_contract.get("userApproved") is True
            and identity_contract.get("selectionWithReplacement") is True
            and identity_contract.get("warmupScorePoolsSeparated") is True
            and integer(identity_contract.get("balancedShardCount"), -1) == 120
            and integer(identity_contract.get("fixturePoolRows"), -1) == 480
        )
        live_rows_after_drop = integer(archive.get("liveRowsAfterDrop"), -1) == integer(
            counts.get("clickHouseLiveUnique"), -2
        )
    else:
        score_count_invariant = all(integer(counts.get(name), -1) == EXPECTED_SCORE_RECORDS for name in (
            "http202", "collectorFinalAck", "kinesisAccepted", "clickHouseAccounted", "clickHouseLiveUnique"
        ))
        identity_contract_valid = identity_mode == "globally-unique-event-id"
        live_rows_after_drop = integer(archive.get("liveRowsAfterDrop"), -1) == EXPECTED_SCORE_RECORDS

    duration_seconds = integer(performance.get("durationSeconds"), -1)
    completed_requests = integer(performance.get("completedRequests"), -1)
    attempted_requests = integer(performance.get("attemptedRequests"), -1)
    transport_errors = integer(performance.get("transportErrors"), -1)
    actual_rps = numeric(performance.get("actualRps"))
    volume_rps = (
        completed_requests / duration_seconds
        if completed_requests >= 0 and duration_seconds > 0
        else float("-inf")
    )
    checks = {
        "actualRpsAtLeast49500": numeric(performance.get("actualRps")) >= 49_500,
        "scoreWindowExactly300Seconds": duration_seconds == 300,
        "completedResponsesAll202": (
            completed_requests > 0
            and completed_requests == integer(counts.get("http202"), -1)
        ),
        "processedVolumeAtLeast14850000": integer(counts.get("http202"), -1) >= 14_850_000,
        "requestAccountingConsistent": attempted_requests == completed_requests + transport_errors,
        "actualRpsConsistentWithCompletedVolume": abs(actual_rps - volume_rps) <= max(5, abs(actual_rps) * 0.02),
        "correctedP95Below300Ms": numeric(performance.get("correctedP95Ms"), float("inf")) < 300,
        "transportErrorsZero": transport_errors == 0,
        "transportErrorRateAtMostPoint001": numeric(performance.get("transportErrorRate"), 1) <= 0.001,
        "http429And5xxZero": integer(performance.get("http429"), -1) == 0 and integer(performance.get("http5xx"), -1) == 0,
        "scoreCountInvariantExact": score_count_invariant,
        "identityContractValid": identity_contract_valid,
        "pipelineFailuresZero": all(integer(failures.get(name), -1) == 0 for name in (
            "kinesisThrottle", "collectorFinalFailure", "kclTerminalFailure", "failureObjects",
            "clickHouseInsertErrors", "archiveFailures", "unexpectedRestarts", "oomKills",
            "terminalFailure", "checkpointError",
        )),
        "drainWithin45Minutes": numeric(drain.get("seconds"), 10**9) <= 2700 and drain.get("iteratorAgeProgressed") is True,
        "visibilityPercentilesPresent": all(numeric(drain.get(name), -1) >= 0 for name in ("visibilityP50Ms", "visibilityP95Ms", "visibilityP99Ms")),
        "hostCpuAndMemoryP95Below70": all(
            numeric(resources.get(role, {}).get(metric), 100) < 70
            for role in host_roles for metric in ("cpuP95Percent", "memoryP95Percent")
        ),
        "filesystemBelow80": numeric(resources.get("clickHouse", {}).get("filesystemPeakPercent"), 100) < 80,
        "haproxyEvidenceComplete": (
            SHA256_PATTERN.fullmatch(str(haproxy.get("configSha256", ""))) is not None
            and integer(haproxy.get("activeBackends"), 0) == 6
            and numeric(haproxy.get("maxQueue"), -1) >= 0
            and integer(haproxy.get("http4xx"), -1) == 0
            and integer(haproxy.get("http5xx"), -1) == 0
            and haproxy.get("prometheusCollected") is True
        ),
        "cloudTrailExecutionCardinalityExact": (
            cloudtrail.get("collected") is True
            and all(integer(cloudtrail.get(name), -1) == 1 for name in (
                "deployAttempts", "warmupAttempts", "scoreAttempts", "archiveAttempts",
            ))
            and isinstance(cloudtrail.get("sourcePaths"), list)
            and bool(cloudtrail.get("sourcePaths"))
            and isinstance(cloudtrail.get("sha256"), list)
            and len(cloudtrail.get("sourcePaths")) == len(cloudtrail.get("sha256"))
            and all(isinstance(path, str) and bool(path) for path in cloudtrail.get("sourcePaths"))
            and all(SHA256_PATTERN.fullmatch(str(digest)) is not None for digest in cloudtrail.get("sha256"))
        ),
        "archiveThreeByFiveMillion": (
            integer(archive.get("rows"), -1) == EXPECTED_SCORE_RECORDS
            and integer(archive.get("objects"), -1) == 3
            and archive.get("objectRows") == [5_000_000, 5_000_000, 5_000_000]
        ),
        "archiveEquivalenceAndDrop": (
            all(integer(archive.get(name), -1) == 0 for name in (
                "preDropSourceMinusArchive", "preDropArchiveMinusSource",
                "committedSourceMinusArchive", "committedArchiveMinusSource",
                "postDropReferenceMinusArchive", "postDropArchiveMinusReference", "sourceRowsAfterDrop",
            ))
            and live_rows_after_drop
            and archive.get("committedReRead") is True
        ),
        "archiveOverlapWithin30Minutes": archive.get("overlappedScoreWindow") is True and numeric(archive.get("cycleSeconds"), 10**9) <= 1800,
        "costWithinApprovedLimits": (
            numeric(cost.get("accruedUpperBoundUsd"), 10**9) < float(NEW_LOAD_STOP_USD)
            and numeric(cost.get("maximumIncludingCleanupUsd"), 10**9) <= float(HARD_CAP_USD)
            and numeric(cost.get("cleanupReserveUsd"), -1) >= float(CLEANUP_RESERVE_USD)
        ),
        "requiredExecutionArtifactsPassed": all(
            execution.get(name) is True
            for name in (
                "deploymentVerified",
                "correctness1002Passed",
                "consumerReplacement900Passed",
                "closedPartitionSeed15MPassed",
                "warmup180FullyAccounted",
            )
        ),
        "oneShotRunnerStateValid": all(
            execution.get(name) is True
            for name in (
                "runnerSequenceComplete",
                "singleDeployWarmupScoreArchive",
                "noRecordedHardStop",
                "commandSetSealed",
            )
        ),
        "deadlineContractMet": all(
            execution.get(name) is True
            for name in ("cleanupStartedByMinute160", "hardDeadlineMet")
        ),
        "cleanupExecutionSucceeded": execution.get("cleanupAndInventorySucceeded") is True,
        "requiredArtifactsComplete": required_artifacts_are_valid(evidence, run_dir),
        "phase5Skipped": evidence.get("phase5") == "skipped",
        "cleanupInventoryZero": cleanup.get("allZero") is True,
    }
    if cleanup.get("allZero") is not True:
        verdict = "blocked"
        verdict_basis = "cleanup-not-authoritatively-zero"
    elif all(checks.values()):
        verdict = "passed"
        verdict_basis = "strict-acceptance"
    else:
        verdict = "failed"
        verdict_basis = "strict-acceptance"
    return {
        "schemaVersion": 1,
        "workload": "phase7-end-to-end-integration",
        "runId": evidence.get("runId"),
        "sessionId": evidence.get("sessionId"),
        "phase5": evidence.get("phase5"),
        "identityMode": identity_mode,
        "evaluatedAt": utc_now(),
        "checks": checks,
        "verdict": verdict,
        "verdictBasis": verdict_basis,
        "failedChecks": sorted(name for name, passed in checks.items() if not passed),
    }


def numeric(value: Any, fallback: float = float("-inf")) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def integer(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        result = int(value)
    except (TypeError, ValueError):
        return fallback
    return result if str(result) == str(value) or isinstance(value, int) else fallback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = evaluate(read_json(args.evidence), args.run_dir.resolve())
    write_json(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

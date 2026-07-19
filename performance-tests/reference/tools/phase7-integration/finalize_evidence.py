#!/usr/bin/env python3
"""Build the immutable Phase 7-1 evidence handoff after owned-resource cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


COLLECTOR_COMMIT = "497315137251af82d0d203ce34702d5543553942"
PHASE6_HANDOFF = "performance-tests/run_20260717_050834_phase6_archive_local_bootstrap_fix/local-handoff.json"
IMPLEMENTATION_PATHS = (
    "assets/clickhouse/phase4-schema.sql",
    "bin/loop-ad_aws_cdk.ts",
    "cdk.json",
    "package-lock.json",
    "package.json",
    "performance-tests/phase1-kinesis/aws-observation-retry.mjs",
    "performance-tests/phase1-kinesis/connection-path-destination.mjs",
    "performance-tests/phase1-kinesis/invoke-oha.mjs",
    "performance-tests/phase1-kinesis/oha-load-contract.mjs",
    "performance-tests/phase1-kinesis/oha12k-aggregate.mjs",
    "performance-tests/phase1-kinesis/run-ec2-oha-worker.sh",
    "performance-tests/phase4-clickhouse/consumer/Dockerfile",
    "performance-tests/phase4-clickhouse/consumer/entrypoint.sh",
    "performance-tests/phase4-clickhouse/consumer/pom.xml",
    "performance-tests/phase4-clickhouse/consumer/src",
    "performance-tests/phase4-clickhouse/producer-env/pyproject.toml",
    "performance-tests/phase4-clickhouse/producer-env/uv.lock",
    "performance-tests/phase6-archive/archive.py",
    "performance-tests/phase6-archive/clickhouse-config/memory.xml",
    "performance-tests/phase6-archive/requirements.txt",
    "performance-tests/phase6-archive/seed_partition.py",
    "performance-tests/phase7-integration/archive",
    "performance-tests/phase7-integration/aws",
    "performance-tests/phase7-integration/clickhouse-config",
    "performance-tests/phase7-integration/cleanup_inventory.py",
    "performance-tests/phase7-integration/docker-compose.yml",
    "performance-tests/phase7-integration/finalize_evidence.py",
    "performance-tests/phase7-integration/haproxy-local.cfg",
    "performance-tests/phase7-integration/local_runner.py",
    "performance-tests/phase7-integration/localstack-init.sh",
    "performance-tests/phase7-integration/run-local.sh",
    "performance-tests/phase7-integration/topology-contract.json",
    "src",
    "tsconfig.json",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def json_write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def implementation_files(root: Path, inputs: Iterable[str] = IMPLEMENTATION_PATHS) -> list[Path]:
    files: list[Path] = []
    for relative in inputs:
        path = root / relative
        if path.is_dir():
            files.extend(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file()
                and "__pycache__" not in candidate.parts
                and candidate.suffix != ".pyc"
            )
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(path)
    return sorted(set(files), key=lambda path: path.relative_to(root).as_posix())


def implementation_digest(
    root: Path,
    inputs: Iterable[str] = IMPLEMENTATION_PATHS,
) -> tuple[str, list[dict[str, str]]]:
    combined = hashlib.sha256()
    manifest: list[dict[str, str]] = []
    for path in implementation_files(root, inputs):
        relative = path.relative_to(root).as_posix()
        digest = file_sha256(path)
        manifest.append({"path": relative, "sha256": digest})
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\n")
    return combined.hexdigest(), manifest


def verdict(local: dict[str, Any] | None, cleanup: dict[str, Any] | None) -> tuple[str, list[str]]:
    failures: list[str] = []
    if not local or local.get("status") != "passed":
        failures.append("local integration result is missing or failed")
    elif local.get("awsNetworkAudit", {}).get("realAwsRequests") != 0:
        failures.append("non-local AWS SDK request was attempted")
    if not cleanup or cleanup.get("status") != "passed":
        failures.append("owned Docker cleanup verification is missing or failed")
    elif any(cleanup.get(key) for key in ("containers", "volumes", "networks")):
        failures.append("owned Docker inventory is not empty")
    return ("passed" if not failures else "failed", failures)


def git_output(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def finalize(args: argparse.Namespace) -> int:
    root = args.infra_root.resolve()
    run_dir = args.run_dir.resolve()
    local = read_json(run_dir / "local-result.json")
    archive = read_json(run_dir / "archive-result.json")
    cleanup = read_json(run_dir / "cleanup-verification.json")
    final_verdict, failures = verdict(local, cleanup)
    implementation_sha, manifest = implementation_digest(root)
    phase6 = read_json(root / PHASE6_HANDOFF)
    if not phase6 or phase6.get("finalVerdict") != "passed":
        failures.append("passed Phase 6 handoff is missing")
        final_verdict = "failed"
    else:
        phase6_manifest = {
            entry.get("path"): entry.get("sha256")
            for entry in phase6.get("implementationFiles", [])
            if isinstance(entry, dict)
        }
        for relative in (
            "performance-tests/phase6-archive/archive.py",
            "performance-tests/phase6-archive/seed_partition.py",
        ):
            if phase6_manifest.get(relative) != file_sha256(root / relative):
                failures.append(f"Phase 6 frozen implementation mismatch: {relative}")
                final_verdict = "failed"

    finished_at = (local or {}).get("finishedAt", utc_now())
    started_stamp = args.run_id.removeprefix("run_").split("_phase7", 1)[0]
    try:
        started_at = datetime.strptime(started_stamp, "%Y%m%d_%H%M%S").replace(
            tzinfo=timezone.utc
        ).isoformat().replace("+00:00", "Z")
    except ValueError:
        started_at = "unknown"
    git_head = git_output(root, "rev-parse", "HEAD")
    git_dirty = bool(git_output(root, "status", "--porcelain"))

    run = {
        "schemaVersion": "1.0",
        "runId": args.run_id,
        "localSessionId": args.session_id,
        "phase": "7-1",
        "executionMode": "local-only",
        "status": "completed",
        "verdict": final_verdict,
        "awsReady": final_verdict == "passed",
        "startedAt": started_at,
        "finishedAt": finished_at,
        "gitHeadAtFinalization": git_head,
        "dirtyWorktreePreserved": git_dirty,
        "awsApiCallsMade": (local or {}).get("awsNetworkAudit", {}).get("realAwsRequests"),
        "unresolvedFailures": failures,
    }
    correctness = {
        "schemaVersion": "1.0",
        "status": final_verdict,
        "correctness": (local or {}).get("correctness"),
        "replacement": (local or {}).get("replacement"),
        "finalLiveRows": (local or {}).get("finalLiveRows"),
        "countInvariantPassed": final_verdict == "passed",
    }
    metrics = {
        "schemaVersion": "1.0",
        "status": final_verdict,
        "overlap": (local or {}).get("overlap"),
        "localThroughputIsCapacityGate": False,
        "awsNetworkAudit": (local or {}).get("awsNetworkAudit"),
        "haproxy": (local or {}).get("haproxy"),
    }
    archive_validation = {
        "schemaVersion": "1.0",
        "status": final_verdict,
        "summary": (local or {}).get("archive"),
        "workerResult": archive,
    }
    handoff = {
        "schemaVersion": "1.0",
        "localRunId": args.run_id,
        "localRunPath": str(run_dir),
        "localSessionId": args.session_id,
        "finalVerdict": final_verdict,
        "awsReady": final_verdict == "passed",
        "collectorCommit": COLLECTOR_COMMIT,
        "gitHeadAtFinalization": git_head,
        "implementationTreeSha256": implementation_sha,
        "implementationTreeDefinition": "SHA-256 of sorted path, NUL, and per-file SHA-256 entries",
        "implementationFiles": manifest,
        "phase6Handoff": {
            "path": PHASE6_HANDOFF,
            "implementationCodeSha256": (phase6 or {}).get("implementationCodeSha256"),
            "archiveSchemaSha256": (phase6 or {}).get("clickHouse", {}).get("archiveSchemaSha256"),
        },
        "gates": {
            "localIntegration": (local or {}).get("status"),
            "archivePostDrop": (local or {}).get("archive", {}).get("postDropPassed"),
            "realAwsRequests": (local or {}).get("awsNetworkAudit", {}).get("realAwsRequests"),
            "cleanup": (cleanup or {}).get("status"),
            "haproxyObservability": (local or {}).get("haproxy", {}).get("prometheusCollected"),
        },
        "cleanup": cleanup,
        "unresolvedFailures": failures,
        "nextAction": "Recompute every hash before a fresh Phase 7-2 AWS deployment.",
    }
    for name, value in (
        ("run.json", run),
        ("correctness-summary.json", correctness),
        ("metrics-summary.json", metrics),
        ("archive-validation.json", archive_validation),
        ("local-handoff.json", handoff),
    ):
        json_write(run_dir / name, value)

    failure_text = "# Phase 7-1 failures\n\n"
    failure_text += (
        "None.\n" if not failures else "\n".join(f"- {failure}" for failure in failures) + "\n"
    )
    (run_dir / "failures.md").write_text(failure_text, encoding="utf-8")
    (run_dir / "commands.md").write_text(
        "# Phase 7-1 commands\n\n`npm run phase7:local`\n", encoding="utf-8"
    )
    (run_dir / "infra.md").write_text(
        "# Phase 7-1 topology\n\n"
        "HAProxy -> 4 collectors -> LocalStack Kinesis -> 2 Java KCL consumers -> "
        "ClickHouse -> archive worker -> LocalStack S3.\n",
        encoding="utf-8",
    )
    overlap = (local or {}).get("overlap", {})
    report = (
        "# Phase 7-1 local integration\n\n"
        f"Verdict: {final_verdict}\n\n"
        f"The run accounted for {(local or {}).get('finalLiveRows', 'unknown')} live-path rows, "
        f"archived {(local or {}).get('archive', {}).get('seedRows', 'unknown')} closed-partition rows, "
        f"and observed {overlap.get('achievedRps', 'unknown')} ACK-completed RPS for a requested "
        f"{overlap.get('requestedRps', 'unknown')} RPS. Local throughput is not an AWS capacity gate.\n\n"
        f"Real AWS SDK attempts: {(local or {}).get('awsNetworkAudit', {}).get('realAwsRequests', 'unknown')}. "
        f"Cleanup: {(cleanup or {}).get('status', 'missing')}.\n"
    )
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    return 0 if final_verdict == "passed" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-root", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(finalize(parse_args()))

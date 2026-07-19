#!/usr/bin/env python3
"""Retry whole 15M local attempts while preserving every attempt as evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--code-sha256", required=True)
    parser.add_argument("--retry-delay-seconds", type=float, default=10.0)
    parser.add_argument("--start-attempt", type=int, default=1)
    args = parser.parse_args()
    if args.retry_delay_seconds < 0 or args.start_attempt <= 0:
        parser.error("retry delay must be non-negative and start attempt must be positive")

    phase6 = Path(__file__).resolve().parent
    run_dir = args.run_dir.resolve()
    summary_path = run_dir / "evidence" / "scale-15m-retries" / "retry-summary.json"
    attempts: List[Dict[str, Any]] = []
    attempt = args.start_attempt
    started_at = utc_now()
    if summary_path.exists():
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
        attempts = list(previous.get("attempts", []))
        started_at = str(previous.get("startedAt", started_at))
        if any(int(item["attempt"]) >= attempt for item in attempts):
            parser.error("start attempt must be greater than every preserved attempt")
    while True:
        evidence_name = f"scale-15m-attempt-{attempt:03d}"
        command = [
            sys.executable,
            str(phase6 / "local_gate.py"),
            "--gate",
            "15m",
            "--project",
            args.project,
            "--session-id",
            args.session_id,
            "--run-id",
            f"{args.run_id}-attempt-{attempt:03d}",
            "--run-dir",
            str(run_dir),
            "--image-digest",
            args.image_digest,
            "--code-sha256",
            args.code_sha256,
            "--evidence-name",
            evidence_name,
        ]
        attempt_started = utc_now()
        try:
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
        except KeyboardInterrupt:
            attempts.append(
                {
                    "attempt": attempt,
                    "startedAt": attempt_started,
                    "finishedAt": utc_now(),
                    "exitCode": 130,
                    "status": "interrupted",
                    "evidence": f"evidence/{evidence_name}",
                    "command": command,
                }
            )
            write_json(
                summary_path,
                {
                    "schemaVersion": "1.0",
                    "status": "interrupted",
                    "startedAt": started_at,
                    "updatedAt": utc_now(),
                    "successfulAttempt": None,
                    "attempts": attempts,
                    "retryPolicy": {
                        "mode": "until-success",
                        "partialResume": False,
                        "newRunIdAndBucketPerAttempt": True,
                        "resourceGuardStopsRetries": True,
                    },
                },
            )
            return 130
        evidence_dir = run_dir / "evidence" / evidence_name
        (evidence_dir / "controller.stdout").write_text(completed.stdout, encoding="utf-8")
        (evidence_dir / "controller.stderr").write_text(completed.stderr, encoding="utf-8")
        gate_path = evidence_dir / "gate-summary.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path.exists() else {}
        record = {
            "attempt": attempt,
            "startedAt": attempt_started,
            "finishedAt": utc_now(),
            "exitCode": completed.returncode,
            "status": gate.get("status", "failed"),
            "evidence": f"evidence/{evidence_name}",
            "command": command,
        }
        attempts.append(record)
        passed = completed.returncode == 0 and gate.get("status") == "passed"
        guard = gate.get("guard", {})
        guard_blocked = bool(guard) and guard.get("passed") is not True
        write_json(
            summary_path,
            {
                "schemaVersion": "1.0",
                "status": "passed" if passed else "blocked" if guard_blocked else "retrying",
                "startedAt": started_at,
                "updatedAt": utc_now(),
                "successfulAttempt": attempt if passed else None,
                "attempts": attempts,
                "retryPolicy": {
                    "mode": "until-success",
                    "partialResume": False,
                    "newRunIdAndBucketPerAttempt": True,
                    "resourceGuardStopsRetries": True,
                },
            },
        )
        if passed:
            return 0
        if guard_blocked:
            print("resource guard blocked further retries", file=sys.stderr)
            return 2
        attempt += 1
        time.sleep(args.retry_delay_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

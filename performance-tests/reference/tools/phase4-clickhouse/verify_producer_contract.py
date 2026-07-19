#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
QUALIFIED_RUN = ROOT / "performance-tests/run_20260716_110956_locust_kinesis_generator_qualification"
IMPLEMENTATION = QUALIFIED_RUN / "implementation"
PAYLOAD = ROOT / "performance-tests/phase1-kinesis/payloads/sdk-compatible-event-bodies.ndjson"

EXPECTED_HASHES = {
    "producer.py": "1e0fb887198eb76b7214bc7738bda6d35a4ccd84c78da360c6958fd121247979",
    "locustfile.py": "d769d1c912dee884396678f2a58277aaf8f4db9fa7e7ad8f753809f716f76837",
    "payload_contract.py": "1c250238851138c9f49625c728ebd77e4ebd9e75d845c32552cc767add621f42",
    "requirements.lock": "ad04335b0924d2d3b0099517ce374034039b813932e86c690c9b8e6482402612",
    "run_stage.sh": "039cd56ea51a11a417482c2927f49942177e04f1317346537840a9857732c4a5",
}
EXPECTED_PAYLOAD_SHA256 = "93704c35ef7ca24c9c887a439dbea011c94a852f98e12b2d51b4bf6d4f3322b7"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def verify_contract() -> dict[str, Any]:
    observed_hashes = {name: sha256(IMPLEMENTATION / name) for name in EXPECTED_HASHES}
    if observed_hashes != EXPECTED_HASHES:
        raise ValueError(f"qualified producer source hash mismatch: {observed_hashes}")

    payload_hash = sha256(PAYLOAD)
    if payload_hash != EXPECTED_PAYLOAD_SHA256:
        raise ValueError(f"payload SHA-256 mismatch: {payload_hash}")

    manifest = read_json(QUALIFIED_RUN / "asset-manifest.json")
    manifest_members = {
        member["name"]: member["sha256"]
        for member in manifest.get("members", [])
        if isinstance(member, dict) and "name" in member and "sha256" in member
    }
    for name, expected_hash in EXPECTED_HASHES.items():
        if manifest_members.get(name) != expected_hash:
            raise ValueError(f"asset manifest mismatch for {name}")
    if manifest_members.get("payloads.ndjson") != EXPECTED_PAYLOAD_SHA256:
        raise ValueError("asset manifest payload hash mismatch")

    run = read_json(QUALIFIED_RUN / "run.json")
    result = read_json(QUALIFIED_RUN / "result-summary.json")
    candidates = run.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("qualified run candidate list is missing")
    selected = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("status") == "qualified"
    ]
    if selected != [{
        "instanceType": "c7g.2xlarge",
        "vcpus": 8,
        "workers": 8,
        "status": "qualified",
    }]:
        raise ValueError(f"qualified candidate contract changed: {selected}")
    final = result.get("final")
    if result.get("status") != "QUALIFIED" or result.get("cleanupPassed") is not True:
        raise ValueError("source run is not qualified with verified cleanup")
    if not isinstance(final, dict) or final.get("successfulLogicalRecords") != 15_000_000:
        raise ValueError("source run final record count changed")
    for key in ("retryRecords", "partialFailureRecords", "finalFailedRecords"):
        if final.get(key) != 0:
            raise ValueError(f"source run {key} must remain zero")

    stage_script = (IMPLEMENTATION / "run_stage.sh").read_text(encoding="utf-8")
    required_fragments = [
        'LOCUST_BATCH_SIZE="500"',
        "for ((worker = 0; worker < WORKERS; worker += 1))",
        '--master --expect-workers "${WORKERS}"',
    ]
    if any(fragment not in stage_script for fragment in required_fragments):
        raise ValueError("qualified run_stage.sh worker/batch contract changed")

    return {
        "status": "passed",
        "qualifiedRunId": result["runId"],
        "candidate": "c7g.2xlarge",
        "workers": 8,
        "targetRecordsPerSecond": 50_000,
        "measurementSeconds": 300,
        "expectedRecords": 15_000_000,
        "payloadSha256": payload_hash,
        "sourceHashes": observed_hashes,
        "entrypoint": str((IMPLEMENTATION / "run_stage.sh").relative_to(ROOT)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify_contract()
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

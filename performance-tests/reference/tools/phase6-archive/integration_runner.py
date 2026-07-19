#!/usr/bin/env python3
"""Run local ClickHouse/S3-compatible Phase 6 gates inside the Compose network."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from archive import ArchiveConfig, ClickHouseHttp, build_s3_client, is_precondition_failure
from seed_partition import (
    DEFAULT_SEED,
    FULL_SCALE_PART_ROWS,
    FULL_SCALE_ROWS,
    GENERATOR_VERSION,
    GeneratorContract,
    seed_insert_sql,
    utc_source_partition,
)

CLICKHOUSE_URL = "http://clickhouse:8123"
S3_ENDPOINT = "http://localstack:4566"
SCHEMA_PATH = Path("/work/schema/phase4-schema.sql")
QUIESCENCE_TIMEOUT_SECONDS = 900
QUIESCENCE_POLL_SECONDS = 2
QUIESCENCE_CONSECUTIVE_OBSERVATIONS = 2
LOCAL_QUERY_MEMORY_BYTES = 9 * 1024 ** 3 // 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def initialize_schema(clickhouse: ClickHouseHttp) -> None:
    clickhouse.execute("DROP DATABASE IF EXISTS loopad SYNC")
    for statement in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            clickhouse.execute(statement.strip())


def make_config(
    *,
    bucket: str,
    run_id: str,
    rows: int,
    rows_per_part: int,
    part_count: int,
    image_digest: str,
    code_sha256: str,
    production: bool,
) -> ArchiveConfig:
    today = datetime.now(timezone.utc).date()
    partition = utc_source_partition(today)
    return ArchiveConfig(
        clickhouse_url=CLICKHOUSE_URL,
        bucket=bucket,
        run_id=run_id,
        partition=partition.isoformat(),
        today=today.isoformat(),
        s3_endpoint_url=S3_ENDPOINT,
        s3_url_base=f"{S3_ENDPOINT}/{bucket}",
        s3_unsigned=True,
        expected_rows=rows,
        rows_per_part=rows_per_part,
        part_count=part_count,
        seed=DEFAULT_SEED,
        generator_version=GENERATOR_VERSION,
        fingerprint_interval_seconds=300 if production else 0,
        export_bandwidth_mibps=100,
        clickhouse_memory_bytes=LOCAL_QUERY_MEMORY_BYTES,
        clickhouse_image_digest=image_digest,
        code_sha256=code_sha256,
        test_mode=not production,
    )


def s3_for(config: ArchiveConfig):
    return build_s3_client(config)


def create_bucket(s3: Any, bucket: str, region: str) -> None:
    s3.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": region},
    )


def list_keys(s3: Any, bucket: str) -> List[str]:
    keys: List[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        keys.extend(str(item["Key"]) for item in page.get("Contents", []))
    return sorted(keys)


def source_count(clickhouse: ClickHouseHttp, partition: str) -> int:
    row = clickhouse.one(
        "SELECT count() AS rows FROM loopad.events FINAL "
        f"WHERE event_date = toDate('{partition}')"
    )
    return int(row["rows"])


def seed(clickhouse: ClickHouseHttp, config: ArchiveConfig) -> float:
    contract = GeneratorContract(
        version=config.generator_version,
        seed=config.seed,
        partition=config.partition,
        rows=config.expected_rows,
        run_id=config.run_id,
    )
    started = time.monotonic()
    clickhouse.execute(seed_insert_sql(contract))
    duration = time.monotonic() - started
    if source_count(clickhouse, config.partition) != config.expected_rows:
        raise RuntimeError("seeded source count mismatch")
    return duration


def background_activity(clickhouse: ClickHouseHttp) -> Dict[str, int]:
    row = clickhouse.one(
        "SELECT\n"
        "  (SELECT count() FROM system.merges "
        "WHERE database = 'loopad' AND table = 'events') AS merges,\n"
        "  (SELECT count() FROM system.mutations "
        "WHERE database = 'loopad' AND table = 'events' AND is_done = 0) AS mutations"
    )
    return {"activeMerges": int(row["merges"]), "activeMutations": int(row["mutations"])}


def wait_for_quiescence(
    clickhouse: ClickHouseHttp,
    *,
    timeout_seconds: float = QUIESCENCE_TIMEOUT_SECONDS,
    poll_seconds: float = QUIESCENCE_POLL_SECONDS,
    consecutive_observations: int = QUIESCENCE_CONSECUTIVE_OBSERVATIONS,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    if timeout_seconds < 0 or poll_seconds <= 0 or consecutive_observations <= 0:
        raise ValueError("invalid quiescence wait configuration")
    started = monotonic_fn()
    samples: List[Dict[str, Any]] = []
    consecutive = 0
    while True:
        activity = background_activity(clickhouse)
        elapsed = monotonic_fn() - started
        quiet = activity["activeMerges"] == 0 and activity["activeMutations"] == 0
        consecutive = consecutive + 1 if quiet else 0
        samples.append(
            {
                "measuredAt": utc_now(),
                "elapsedSeconds": round(elapsed, 6),
                **activity,
                "consecutiveQuietObservations": consecutive,
            }
        )
        if consecutive >= consecutive_observations:
            return {
                "status": "passed",
                "waitedSeconds": round(elapsed, 6),
                "timeoutSeconds": timeout_seconds,
                "pollSeconds": poll_seconds,
                "requiredConsecutiveObservations": consecutive_observations,
                "samples": samples,
            }
        if elapsed >= timeout_seconds:
            return {
                "status": "failed",
                "reason": "source background activity did not become quiescent before timeout",
                "waitedSeconds": round(elapsed, 6),
                "timeoutSeconds": timeout_seconds,
                "pollSeconds": poll_seconds,
                "requiredConsecutiveObservations": consecutive_observations,
                "samples": samples,
            }
        sleep_fn(min(poll_seconds, max(0.0, timeout_seconds - elapsed)))


def run_worker(
    config: ArchiveConfig,
    directory: Path,
    *,
    fault: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    config_path = directory / "archive-config.json"
    output_path = directory / ("worker-result.json" if not fault else f"worker-{fault}.json")
    write_json(config_path, config.__dict__)
    command = [sys.executable, "archive.py", "--config", str(config_path), "--output", str(output_path)]
    if fault:
        command.extend(["--test-fault", fault])
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    (directory / ("worker.stdout" if not fault else f"worker-{fault}.stdout")).write_text(
        completed.stdout, encoding="utf-8"
    )
    (directory / ("worker.stderr" if not fault else f"worker-{fault}.stderr")).write_text(
        completed.stderr, encoding="utf-8"
    )
    return completed


def read_worker_result(directory: Path, fault: Optional[str] = None) -> Dict[str, Any]:
    name = "worker-result.json" if not fault else f"worker-{fault}.json"
    return json.loads((directory / name).read_text(encoding="utf-8"))


def scenario_bucket(run_id: str, name: str) -> str:
    suffix = hashlib.sha256(f"{run_id}:{name}".encode("utf-8")).hexdigest()[:12]
    return f"phase6-{name}-{suffix}"


def run_normal_gate(
    *,
    gate: str,
    run_id: str,
    directory: Path,
    image_digest: str,
    code_sha256: str,
) -> Dict[str, Any]:
    if gate == "small":
        rows, rows_per_part, part_count, production = 3_000, 1_000, 3, False
    elif gate == "1m":
        rows, rows_per_part, part_count, production = 1_000_000, 1_000_000, 1, False
    elif gate == "15m":
        rows, rows_per_part, part_count, production = (
            FULL_SCALE_ROWS,
            FULL_SCALE_PART_ROWS,
            3,
            True,
        )
    else:
        raise ValueError(f"unsupported normal gate: {gate}")
    clickhouse = ClickHouseHttp(CLICKHOUSE_URL)
    initialize_schema(clickhouse)
    config = make_config(
        bucket=scenario_bucket(run_id, gate),
        run_id=f"{run_id}-{gate}",
        rows=rows,
        rows_per_part=rows_per_part,
        part_count=part_count,
        image_digest=image_digest,
        code_sha256=code_sha256,
        production=production,
    )
    s3 = s3_for(config)
    create_bucket(s3, config.bucket, config.region)
    seed_seconds = seed(clickhouse, config)
    quiescence: Dict[str, Any] = {"status": "not-required"}
    if production:
        quiescence = wait_for_quiescence(clickhouse)
        write_json(directory / "pre-worker-quiescence.json", quiescence)
        if quiescence["status"] != "passed":
            raise RuntimeError(str(quiescence["reason"]))
    completed = run_worker(config, directory)
    result = read_worker_result(directory)
    passed = (
        completed.returncode == 0
        and result.get("status") == "passed"
        and result.get("sourceRowsAfter") == 0
        and len(result.get("parts", [])) == part_count
        and sum(int(part["rows"]) for part in result.get("parts", [])) == rows
        and result.get("preDrop", {}).get("passed") is True
        and result.get("postDrop", {}).get("passed") is True
    )
    if not passed:
        raise RuntimeError(f"{gate} gate failed: exit={completed.returncode}, result={result}")
    return {
        "status": "passed",
        "gate": gate,
        "startedAt": result["startedAt"],
        "finishedAt": result["finishedAt"],
        "seedSeconds": round(seed_seconds, 6),
        "archiveSeconds": result["durationSeconds"],
        "preWorkerQuiescence": quiescence,
        "rows": rows,
        "objectCount": part_count,
        "parts": result["parts"],
        "preDrop": result["preDrop"],
        "postDrop": result["postDrop"],
        "sourceFingerprints": result.get("sourceFingerprints", []),
        "generator": {
            "version": config.generator_version,
            "seed": config.seed,
            "referenceSha256": config.generator.reference_sha256(),
        },
        "bucket": config.bucket,
        "objectKeys": list_keys(s3, config.bucket),
    }


def failed_fault_scenario(
    *,
    name: str,
    fault: str,
    clickhouse: ClickHouseHttp,
    root: Path,
    run_id: str,
    image_digest: str,
    code_sha256: str,
) -> Dict[str, Any]:
    directory = root / name
    initialize_schema(clickhouse)
    config = make_config(
        bucket=scenario_bucket(run_id, name),
        run_id=f"{run_id}-fault-{name}",
        rows=3_000,
        rows_per_part=1_000,
        part_count=3,
        image_digest=image_digest,
        code_sha256=code_sha256,
        production=False,
    )
    s3 = s3_for(config)
    create_bucket(s3, config.bucket, config.region)
    seed(clickhouse, config)
    completed = run_worker(config, directory, fault=fault)
    result = read_worker_result(directory, fault=fault)
    commit_exists = config.commit_key in list_keys(s3, config.bucket)
    passed = (
        completed.returncode != 0
        and result.get("status") == "failed"
        and source_count(clickhouse, config.partition) == config.expected_rows
        and not commit_exists
    )
    if not passed:
        raise RuntimeError(f"fault scenario {name} did not block deletion: {result}")
    return {
        "status": "passed",
        "workerExit": completed.returncode,
        "observedFailure": result,
        "sourceRowsPreserved": config.expected_rows,
        "commitCreated": commit_exists,
        "objectKeys": list_keys(s3, config.bucket),
    }


def duplicate_commit_scenario(
    *,
    clickhouse: ClickHouseHttp,
    root: Path,
    run_id: str,
    image_digest: str,
    code_sha256: str,
) -> Dict[str, Any]:
    directory = root / "duplicate-commit"
    initialize_schema(clickhouse)
    config = make_config(
        bucket=scenario_bucket(run_id, "duplicate"),
        run_id=f"{run_id}-fault-duplicate",
        rows=3_000,
        rows_per_part=1_000,
        part_count=3,
        image_digest=image_digest,
        code_sha256=code_sha256,
        production=False,
    )
    s3 = s3_for(config)
    create_bucket(s3, config.bucket, config.region)
    seed(clickhouse, config)
    first = run_worker(config, directory / "first")
    if first.returncode != 0:
        raise RuntimeError("duplicate scenario initial archive failed")
    original = s3.get_object(Bucket=config.bucket, Key=config.commit_key)["Body"]
    try:
        original_bytes = original.read()
    finally:
        original.close()
    duplicate_rejected = False
    try:
        s3.put_object(
            Bucket=config.bucket,
            Key=config.commit_key,
            Body=b'{"different":true}\n',
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except BaseException as error:
        if is_precondition_failure(error):
            duplicate_rejected = True
        else:
            raise
    after = s3.get_object(Bucket=config.bucket, Key=config.commit_key)["Body"]
    try:
        after_bytes = after.read()
    finally:
        after.close()
    seed(clickhouse, config)
    recovery = run_worker(config, directory / "recovery")
    recovery_result = read_worker_result(directory / "recovery")
    passed = (
        duplicate_rejected
        and original_bytes == after_bytes
        and recovery.returncode == 0
        and recovery_result.get("recoveryState") == "commit-present-source-present"
        and source_count(clickhouse, config.partition) == 0
    )
    if not passed:
        raise RuntimeError("duplicate COMMITTED scenario failed")
    return {
        "status": "passed",
        "conditionalCreateRejected": duplicate_rejected,
        "commitUnchanged": original_bytes == after_bytes,
        "recoveryState": recovery_result["recoveryState"],
        "sourceRowsAfter": 0,
    }


def kill_and_restart_scenario(
    *,
    clickhouse: ClickHouseHttp,
    root: Path,
    run_id: str,
    image_digest: str,
    code_sha256: str,
) -> Dict[str, Any]:
    directory = root / "process-termination"
    initialize_schema(clickhouse)
    config = make_config(
        bucket=scenario_bucket(run_id, "kill"),
        run_id=f"{run_id}-fault-kill",
        rows=3_000,
        rows_per_part=1_000,
        part_count=3,
        image_digest=image_digest,
        code_sha256=code_sha256,
        production=False,
    )
    s3 = s3_for(config)
    create_bucket(s3, config.bucket, config.region)
    seed(clickhouse, config)
    killed = run_worker(config, directory / "killed", fault="kill-after-first-part")
    keys_after_kill = list_keys(s3, config.bucket)
    source_preserved = source_count(clickhouse, config.partition) == config.expected_rows
    no_commit = config.commit_key not in keys_after_kill
    restarted = run_worker(config, directory / "restart")
    restart_result = read_worker_result(directory / "restart")
    keys_after_restart = list_keys(s3, config.bucket)
    attempts = sorted(
        {key.split("/")[4] for key in keys_after_restart if key.startswith("attempts/")}
    )
    passed = (
        killed.returncode != 0
        and source_preserved
        and no_commit
        and restarted.returncode == 0
        and restart_result.get("status") == "passed"
        and len(attempts) == 2
        and source_count(clickhouse, config.partition) == 0
    )
    if not passed:
        raise RuntimeError("process termination whole-partition restart failed")
    return {
        "status": "passed",
        "terminatedExit": killed.returncode,
        "sourcePreservedAfterTermination": source_preserved,
        "commitAbsentAfterTermination": no_commit,
        "attemptCountAfterRestart": len(attempts),
        "attempts": attempts,
        "restartStatus": restart_result["status"],
        "sourceRowsAfterRestart": 0,
    }


def run_faults(
    *,
    run_id: str,
    directory: Path,
    image_digest: str,
    code_sha256: str,
) -> Dict[str, Any]:
    clickhouse = ClickHouseHttp(CLICKHOUSE_URL)
    scenarios = {
        "missingPart": failed_fault_scenario(
            name="missing-part",
            fault="missing-part",
            clickhouse=clickhouse,
            root=directory,
            run_id=run_id,
            image_digest=image_digest,
            code_sha256=code_sha256,
        ),
        "checksumMismatch": failed_fault_scenario(
            name="checksum-mismatch",
            fault="checksum-mismatch",
            clickhouse=clickhouse,
            root=directory,
            run_id=run_id,
            image_digest=image_digest,
            code_sha256=code_sha256,
        ),
        "duplicateCommit": duplicate_commit_scenario(
            clickhouse=clickhouse,
            root=directory,
            run_id=run_id,
            image_digest=image_digest,
            code_sha256=code_sha256,
        ),
        "processTerminationAndWholePartitionRestart": kill_and_restart_scenario(
            clickhouse=clickhouse,
            root=directory,
            run_id=run_id,
            image_digest=image_digest,
            code_sha256=code_sha256,
        ),
    }
    return {
        "schemaVersion": "1.0",
        "status": "passed" if all(value["status"] == "passed" for value in scenarios.values()) else "failed",
        "startedAt": utc_now(),
        "scenarios": scenarios,
        "finishedAt": utc_now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", choices=["small", "faults", "1m", "15m"], required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--code-sha256", required=True)
    args = parser.parse_args()
    started_at = utc_now()
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.gate == "faults":
            result = run_faults(
                run_id=args.run_id,
                directory=args.output.parent,
                image_digest=args.image_digest,
                code_sha256=args.code_sha256,
            )
        else:
            result = run_normal_gate(
                gate=args.gate,
                run_id=args.run_id,
                directory=args.output.parent,
                image_digest=args.image_digest,
                code_sha256=args.code_sha256,
            )
        write_json(args.output, result)
        return 0
    except BaseException as error:
        write_json(
            args.output,
            {
                "schemaVersion": "1.0",
                "status": "failed",
                "gate": args.gate,
                "startedAt": started_at,
                "finishedAt": utc_now(),
                "errorType": type(error).__name__,
                "error": str(error),
            },
        )
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

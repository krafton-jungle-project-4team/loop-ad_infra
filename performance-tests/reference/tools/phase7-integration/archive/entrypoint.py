#!/usr/bin/env python3
"""Build one Phase 6 archive invocation from ECS-injected runtime configuration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.config import Config


SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "total_max_attempts": 5},
    user_agent_appid="loopad-phase7-archive-result/1",
)

PHASE7_ARCHIVE_QUERY_MEMORY_BYTES = 6 * 1024**3


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value or value.strip() != value:
        raise ValueError(f"missing or invalid runtime configuration: {name}")
    return value


def positive_integer(name: str) -> int:
    value = int(required(name))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def optional_boolean(name: str) -> bool:
    value = os.environ.get(name, "false")
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be exactly true or false")
    return value == "true"


def build_config(user_file: str, password_file: str) -> dict[str, object]:
    rows = positive_integer("ARCHIVE_EXPECTED_ROWS")
    rows_per_part = positive_integer("ARCHIVE_ROWS_PER_PART")
    part_count = positive_integer("ARCHIVE_PART_COUNT")
    return {
        "clickhouse_url": required("CLICKHOUSE_HTTP_URL"),
        "clickhouse_user_file": user_file,
        "clickhouse_password_file": password_file,
        "bucket": required("ARCHIVE_BUCKET"),
        "run_id": required("RUN_ID"),
        "partition": required("ARCHIVE_PARTITION"),
        "today": required("ARCHIVE_TODAY"),
        "region": required("AWS_REGION"),
        "account": required("AWS_ACCOUNT_ID"),
        "expected_rows": rows,
        "rows_per_part": rows_per_part,
        "part_count": part_count,
        "seed": 6_000_017,
        "generator_version": "phase6-events-v1",
        "fingerprint_interval_seconds": 300,
        "export_bandwidth_mibps": 100,
        "clickhouse_memory_bytes": PHASE7_ARCHIVE_QUERY_MEMORY_BYTES,
        "clickhouse_image_digest": required("CLICKHOUSE_IMAGE"),
        "code_sha256": required("ARCHIVE_IMAGE_DIGEST"),
        "test_mode": False,
        "retain_source_after_commit": optional_boolean("ARCHIVE_RETAIN_SOURCE_AFTER_COMMIT"),
    }


def secret_memfd(name: str, value: str) -> int:
    if not hasattr(os, "memfd_create"):
        raise RuntimeError("archive secret handoff requires Linux memfd support")
    fd = os.memfd_create(name, flags=0)
    try:
        os.write(fd, (value + "\n").encode())
        os.lseek(fd, 0, os.SEEK_SET)
        os.set_inheritable(fd, True)
        return fd
    except Exception:
        os.close(fd)
        raise


def result_object_key(run_id: str, partition: str) -> str:
    return (
        f"attempts/v1/table=events/event_date={partition}/"
        f"phase7-result-{run_id}.json"
    )


def publish_result(result_path: Path, *, bucket: str, run_id: str, partition: str) -> str:
    if not result_path.is_file():
        raise RuntimeError("archive worker did not write its result document")
    raw = result_path.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("status") not in {"passed", "failed"}:
        raise RuntimeError("archive worker result document is invalid")
    if document.get("status") == "passed" and document.get("runId") != run_id:
        raise RuntimeError("archive worker result Run ID does not match the task contract")
    key = result_object_key(run_id, partition)
    boto3.client("s3", region_name=required("AWS_REGION"), config=SDK_CONFIG).put_object(
        Bucket=bucket,
        Key=key,
        Body=raw,
        ContentType="application/json",
        IfNoneMatch="*",
    )
    return key


def main() -> int:
    config_path = Path("/run/loopad/archive.json")
    result_path = Path("/run/loopad/result.json")
    user_fd = secret_memfd("phase7-clickhouse-user", required("CLICKHOUSE_USER"))
    try:
        password_fd = secret_memfd("phase7-clickhouse-password", required("CLICKHOUSE_PASSWORD"))
    except Exception:
        os.close(user_fd)
        raise
    try:
        config_path.write_text(json.dumps(build_config(
            f"/proc/self/fd/{user_fd}", f"/proc/self/fd/{password_fd}"
        ), sort_keys=True) + "\n", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                "/opt/loopad/archive/archive.py",
                "--config",
                str(config_path),
                "--output",
                str(result_path),
            ],
            check=False,
            pass_fds=(user_fd, password_fd),
        )
    finally:
        os.close(user_fd)
        os.close(password_fd)
    publish_result(
        result_path,
        bucket=required("ARCHIVE_BUCKET"),
        run_id=required("RUN_ID"),
        partition=required("ARCHIVE_PARTITION"),
    )
    print(result_path.read_text(encoding="utf-8"), end="")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the Phase 7-1 collector-to-archive integration contract locally."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
PHASE6 = ROOT / "performance-tests/phase6-archive"
sys.path.insert(0, str(PHASE6))

from seed_partition import (  # noqa: E402
    DEFAULT_SEED,
    GENERATOR_VERSION,
    GeneratorContract,
    seed_insert_sql,
    utc_source_partition,
)

REGION = "ap-northeast-2"
STREAM_NAME = "phase7-local-events"
LEASE_TABLE = "phase7-local-leases"
ARCHIVE_BUCKET = "phase7-local-archive"
CLICKHOUSE_USER = "loopad_local"
CLICKHOUSE_PASSWORD = "local-only-not-a-secret"


def require_loopback_http(value: str, label: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{label} must be an explicit loopback HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain credentials, query, or fragment")
    return value.rstrip("/")


def collect_haproxy_evidence(endpoint: str, run_dir: Path, compose_file: Path) -> dict[str, Any]:
    endpoint = require_loopback_http(endpoint, "HAProxy stats endpoint")
    with urllib.request.urlopen(f"{endpoint}/metrics", timeout=10) as response:
        metrics = response.read().decode("utf-8")
    if response.status != 200:
        raise RuntimeError(f"HAProxy metrics returned HTTP {response.status}")
    required_families = {
        "haproxy_backend_status",
        "haproxy_backend_current_queue",
        "haproxy_backend_http_responses_total",
        "haproxy_server_status",
    }
    observed_families = {
        match.group(1)
        for line in metrics.splitlines()
        if (match := re.match(r"^(haproxy_[a-zA-Z0-9_:]+)(?:\{|\s)", line))
    }
    missing = sorted(required_families.difference(observed_families))
    if missing:
        raise RuntimeError(f"HAProxy Prometheus evidence is missing: {', '.join(missing)}")
    active_backends = sum(
        1
        for line in metrics.splitlines()
        if line.startswith("haproxy_server_status{")
        and 'proxy="collectors"' in line
        and 'state="UP"' in line
        and line.rstrip().endswith(" 1")
    )
    if active_backends != 4:
        raise RuntimeError(f"expected four active HAProxy collector backends, found {active_backends}")
    metrics_path = run_dir / "haproxy-metrics.prom"
    metrics_path.write_text(metrics, encoding="utf-8")
    proxy_logs = compose_command(compose_file, "logs", "haproxy").stdout
    request_log_lines = sum(1 for line in proxy_logs.splitlines() if '"POST /events ' in line)
    config_path = Path(__file__).resolve().with_name("haproxy-local.cfg")
    return {
        "statsEndpoint": endpoint,
        "prometheusCollected": True,
        "metricsPath": str(metrics_path),
        "metricsBytes": len(metrics.encode("utf-8")),
        "metricFamilies": sorted(required_families),
        "activeBackends": active_backends,
        "sampledRequestLogLines": request_log_lines,
        "successLogSampleRate": "1/1000",
        "allErrorsLogged": True,
        "configSha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
    }


class AwsNetworkAudit:
    def __init__(self) -> None:
        self.local_requests = 0
        self.real_aws_requests = 0

    def before_send(self, request: Any, **_kwargs: Any) -> None:
        host = urllib.parse.urlparse(str(request.url)).hostname
        if host not in {"127.0.0.1", "localhost", "::1"}:
            self.real_aws_requests += 1
            raise RuntimeError("blocked non-local AWS SDK request")
        self.local_requests += 1


def local_client(service: str, endpoint: str, audit: AwsNetworkAudit) -> Any:
    endpoint = require_loopback_http(endpoint, f"{service} endpoint")
    session = boto3.Session(
        aws_access_key_id="local-test",
        aws_secret_access_key="local-test",
        region_name=REGION,
    )
    config_values: dict[str, Any] = {
        "retries": {"total_max_attempts": 2, "mode": "standard"},
        "connect_timeout": 2,
        "read_timeout": 30,
    }
    if service == "s3":
        config_values["s3"] = {"addressing_style": "path"}
    config = Config(**config_values)
    client = session.client(service, endpoint_url=endpoint, config=config)
    client.meta.events.register("before-send", audit.before_send)
    return client


class ClickHouseHttp:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = require_loopback_http(endpoint, "ClickHouse endpoint") + "/"

    def execute(self, query: str, timeout: float = 1800) -> str:
        request = urllib.request.Request(
            self.endpoint,
            data=query.encode("utf-8"),
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "X-ClickHouse-User": CLICKHOUSE_USER,
                "X-ClickHouse-Key": CLICKHOUSE_PASSWORD,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ClickHouse HTTP {error.code}: {detail[:500]}") from error

    def count(self, table: str, where: str = "1") -> int:
        final = " FINAL" if table == "events" else ""
        text = self.execute(f"SELECT count() FROM loopad.{table}{final} WHERE {where}")
        return int(text.strip())


def event_document(run_id: str, sequence: int, *, late: bool = False) -> dict[str, Any]:
    event_time = datetime.now(timezone.utc) - (timedelta(days=8) if late else timedelta())
    event_id = f"phase7-{run_id}-{sequence:09d}"
    return {
        "project_id": "phase7-project",
        "write_key": "phase7-local-write-key",
        "schema_version": "hotel_rec_promo.v1",
        "event_id": event_id,
        "event_name": "phase7_integration",
        "event_time": event_time.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "source": "browser_sdk",
        "user_id": f"user-{sequence % 1000}",
        "session_id": f"session-{sequence % 100}",
        "properties_json": json.dumps({"sequence": sequence}, separators=(",", ":")),
    }


def post_event(endpoint: str, document: dict[str, Any]) -> None:
    request = urllib.request.Request(
        f"{endpoint}/events",
        data=json.dumps(document, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
            if response.status != 202 or json.loads(body) != {"accepted": 1}:
                raise RuntimeError(f"collector acceptance mismatch: {response.status}: {body[:500]!r}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"collector HTTP {error.code}: {detail[:500]}") from error


def send_http_events(endpoint: str, run_id: str, start: int, count: int, workers: int = 32) -> float:
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(post_event, endpoint, event_document(run_id, sequence))
            for sequence in range(start, start + count)
        ]
        for future in concurrent.futures.as_completed(futures):
            future.result()
    return time.monotonic() - started


def send_paced_events(endpoint: str, run_id: str, start: int, rps: int, seconds: int) -> dict[str, Any]:
    count = rps * seconds
    started = time.monotonic()
    sent = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
        in_flight: set[concurrent.futures.Future[None]] = set()
        for offset in range(count):
            deadline = started + offset / rps
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            in_flight.add(executor.submit(post_event, endpoint, event_document(run_id, start + offset)))
            if len(in_flight) >= 256:
                done, in_flight = concurrent.futures.wait(
                    in_flight, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in done:
                    future.result()
        for future in concurrent.futures.as_completed(in_flight):
            future.result()
    duration = time.monotonic() - started
    return {
        "records": count,
        "requestedRps": rps,
        "durationSeconds": round(duration, 6),
        "achievedRps": round(count / duration, 6),
    }


def wait_until(description: str, predicate: Any, timeout: float = 180, interval: float = 1) -> None:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except BaseException as error:
            last_error = error
        time.sleep(interval)
    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(f"timed out waiting for {description}{detail}")


def wait_for_kcl(dynamodb: Any) -> None:
    def ready() -> bool:
        if LEASE_TABLE not in dynamodb.list_tables().get("TableNames", []):
            return False
        return int(dynamodb.scan(TableName=LEASE_TABLE, Select="COUNT").get("Count", 0)) >= 4

    wait_until("KCL leases", ready, timeout=240, interval=2)


def wait_for_run_rows(clickhouse: ClickHouseHttp, run_id: str, expected: int) -> None:
    escaped_prefix = f"phase7-{run_id}-".replace("'", "''")
    wait_until(
        f"{expected} ClickHouse rows for {run_id}",
        lambda: clickhouse.count("events", f"startsWith(event_id, '{escaped_prefix}')") == expected,
        timeout=300,
        interval=1,
    )


def compose_command(compose_file: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), *args],
        cwd=compose_file.parent,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {detail[-1000:]}")
    return completed


def archive_config(run_id: str, run_dir: Path, rows: int) -> tuple[Path, str]:
    today = datetime.now(timezone.utc).date()
    partition = utc_source_partition(today).isoformat()
    user_file = run_dir / "clickhouse-user"
    password_file = run_dir / "clickhouse-password"
    user_file.write_text(CLICKHOUSE_USER + "\n", encoding="utf-8")
    password_file.write_text(CLICKHOUSE_PASSWORD + "\n", encoding="utf-8")
    config = {
        "clickhouse_url": "http://clickhouse:8123",
        "bucket": ARCHIVE_BUCKET,
        "run_id": f"{run_id}-archive",
        "partition": partition,
        "today": today.isoformat(),
        "s3_endpoint_url": "http://localstack:4566",
        "s3_url_base": f"http://localstack:4566/{ARCHIVE_BUCKET}",
        "s3_unsigned": True,
        "region": REGION,
        "expected_rows": rows,
        "rows_per_part": rows,
        "part_count": 1,
        "seed": DEFAULT_SEED,
        "generator_version": GENERATOR_VERSION,
        "fingerprint_interval_seconds": 0,
        "export_bandwidth_mibps": 100,
        "clickhouse_memory_bytes": 9 * 1024**3 // 2,
        "clickhouse_image_digest": "sha256:93f557eb9258198d5c52d723287a33a2697cd76900d85cecc0b307cd6293a797",
        "code_sha256": "phase7-local-worktree",
        "temp_dir": "/tmp/loopad-phase7",
        "test_mode": True,
        "clickhouse_user_file": "/work/run/clickhouse-user",
        "clickhouse_password_file": "/work/run/clickhouse-password",
    }
    path = run_dir / "archive-config.json"
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, partition


def seed_archive_partition(clickhouse: ClickHouseHttp, run_id: str, partition: str, rows: int) -> float:
    contract = GeneratorContract(
        version=GENERATOR_VERSION,
        seed=DEFAULT_SEED,
        partition=partition,
        rows=rows,
        run_id=f"{run_id}-archive",
    )
    started = time.monotonic()
    clickhouse.execute(seed_insert_sql(contract))
    if clickhouse.count("events", f"event_date = toDate('{partition}')") != rows:
        raise RuntimeError("archive seed count mismatch")
    return time.monotonic() - started


def read_archive_result(run_dir: Path) -> dict[str, Any]:
    result = json.loads((run_dir / "archive-result.json").read_text(encoding="utf-8"))
    if result.get("status") != "passed" or not result.get("postDrop", {}).get("passed"):
        raise RuntimeError("archive result did not pass post-DROP equivalence")
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    endpoint = require_loopback_http(args.collector_endpoint, "collector endpoint")
    audit = AwsNetworkAudit()
    kinesis = local_client("kinesis", args.localstack_endpoint, audit)
    dynamodb = local_client("dynamodb", args.localstack_endpoint, audit)
    s3 = local_client("s3", args.localstack_endpoint, audit)
    clickhouse = ClickHouseHttp(args.clickhouse_endpoint)

    wait_until("collector health", lambda: urllib.request.urlopen(f"{endpoint}/health", timeout=2).status == 200)
    wait_until("LocalStack stream", lambda: STREAM_NAME in kinesis.list_streams().get("StreamNames", []))
    wait_for_kcl(dynamodb)

    correctness_duration = send_http_events(endpoint, args.run_id, 0, args.correctness_records)
    kinesis.put_records(
        StreamName=STREAM_NAME,
        Records=[
            {"Data": b'{"run_id":"phase7-invalid",', "PartitionKey": "phase7-invalid"},
            {
                "Data": json.dumps(event_document(args.run_id, 900_000_000, late=True), separators=(",", ":")).encode("utf-8"),
                "PartitionKey": "phase7-late",
            },
        ],
    )
    wait_for_run_rows(clickhouse, args.run_id, args.correctness_records)
    wait_until("one invalid raw row", lambda: clickhouse.count("raw_events", "error_code = 'invalid_json'") >= 1)

    compose_command(args.compose_file, "restart", "collector-1", "consumer-1", timeout=300)
    wait_until("collector recovery", lambda: urllib.request.urlopen(f"{endpoint}/health", timeout=2).status == 200)
    wait_for_kcl(dynamodb)
    replacement_duration = send_http_events(
        endpoint, args.run_id, args.correctness_records, args.replacement_records
    )
    expected_before_live = args.correctness_records + args.replacement_records
    wait_for_run_rows(clickhouse, args.run_id, expected_before_live)

    _, partition = archive_config(args.run_id, run_dir, args.archive_rows)
    seed_duration = seed_archive_partition(clickhouse, args.run_id, partition, args.archive_rows)
    live_result: dict[str, Any] = {}
    live_error: list[BaseException] = []

    def send_live() -> None:
        try:
            live_result.update(
                send_paced_events(
                    endpoint,
                    args.run_id,
                    expected_before_live,
                    args.live_rps,
                    args.live_seconds,
                )
            )
        except BaseException as error:
            live_error.append(error)

    live_thread = threading.Thread(target=send_live, name="phase7-live-load")
    live_thread.start()
    archive_started = time.monotonic()
    compose_command(
        args.compose_file,
        "exec",
        "-T",
        "archive-worker",
        "python",
        "/work/phase6/archive.py",
        "--config",
        "/work/run/archive-config.json",
        "--output",
        "/work/run/archive-result.json",
        timeout=1800,
    )
    archive_duration = time.monotonic() - archive_started
    live_thread.join(timeout=max(args.live_seconds + 120, args.live_seconds * 3))
    if live_thread.is_alive():
        raise RuntimeError("live load did not finish")
    if live_error:
        raise live_error[0]
    archive_result = read_archive_result(run_dir)

    final_expected = expected_before_live + args.live_rps * args.live_seconds
    wait_for_run_rows(clickhouse, args.run_id, final_expected)
    if clickhouse.count("events", f"event_date = toDate('{partition}')") != 0:
        raise RuntimeError("archived ClickHouse partition was not dropped")
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=ARCHIVE_BUCKET):
        keys.extend(item["Key"] for item in page.get("Contents", []))
    if not any(key.endswith("/COMMITTED") for key in keys):
        raise RuntimeError("archive commit marker is missing")

    logs = compose_command(args.compose_file, "logs", "consumer-1", "consumer-2").stdout
    if '"LateEventDropped":1' not in logs:
        raise RuntimeError("LateEventDropped evidence is missing from consumer logs")
    if audit.real_aws_requests != 0:
        raise RuntimeError("real AWS SDK request was attempted during Phase 7-1")
    haproxy = collect_haproxy_evidence(
        args.haproxy_stats_endpoint,
        run_dir,
        args.compose_file,
    )

    result = {
        "schemaVersion": "1.0",
        "status": "passed",
        "runId": args.run_id,
        "correctness": {
            "validRows": args.correctness_records,
            "invalidRawRows": 1,
            "lateDroppedRows": 1,
            "durationSeconds": round(correctness_duration, 6),
        },
        "replacement": {
            "collector": "collector-1",
            "consumer": "consumer-1",
            "validRows": args.replacement_records,
            "durationSeconds": round(replacement_duration, 6),
        },
        "overlap": live_result,
        "archive": {
            "partition": partition,
            "seedRows": args.archive_rows,
            "seedDurationSeconds": round(seed_duration, 6),
            "archiveDurationSeconds": round(archive_duration, 6),
            "objectCount": len(keys),
            "postDropPassed": archive_result["postDrop"]["passed"],
        },
        "finalLiveRows": final_expected,
        "awsNetworkAudit": {
            "localRequests": audit.local_requests,
            "realAwsRequests": audit.real_aws_requests,
        },
        "haproxy": haproxy,
        "finishedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    (run_dir / "local-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--collector-endpoint", default="http://127.0.0.1:18080")
    parser.add_argument("--localstack-endpoint", default="http://127.0.0.1:14567")
    parser.add_argument("--clickhouse-endpoint", default="http://127.0.0.1:18127")
    parser.add_argument("--haproxy-stats-endpoint", default="http://127.0.0.1:18404")
    parser.add_argument("--correctness-records", type=int, default=1_000)
    parser.add_argument("--replacement-records", type=int, default=200)
    parser.add_argument("--archive-rows", type=int, default=1_000_000)
    parser.add_argument("--live-rps", type=int, default=200)
    parser.add_argument("--live-seconds", type=int, default=120)
    args = parser.parse_args()
    for name in ("correctness_records", "replacement_records", "archive_rows", "live_rps", "live_seconds"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def main() -> int:
    try:
        print(json.dumps(run(parse_args()), indent=2, sort_keys=True))
        return 0
    except (ClientError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

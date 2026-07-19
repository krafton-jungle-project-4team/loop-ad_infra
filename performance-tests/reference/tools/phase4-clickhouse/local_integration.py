#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import boto3
from botocore.config import Config


ROOT = Path(__file__).resolve().parents[2]
QUALIFIED_IMPLEMENTATION = (
    ROOT
    / "performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation"
)
sys.path.insert(0, str(QUALIFIED_IMPLEMENTATION))

from payload_contract import EXPECTED_POOL_SHA256, PayloadFactory, compact_json  # noqa: E402
from producer import KinesisBatchSender  # noqa: E402


REGION = "ap-northeast-2"
RECEIVED_AT = "2026-07-16T12:00:00.000Z"
PAYLOAD_PATH = ROOT / "performance-tests/phase1-kinesis/payloads/sdk-compatible-event-bodies.ndjson"
HANDLER_HARNESS = ROOT / "performance-tests/phase4-clickhouse/local-handler-harness.ts"
LOCAL_DUMMY_ACCESS_KEY = "test"
LOCAL_DUMMY_SECRET_KEY = "test"
CORRECTNESS_RECORDS = 1_000
ASYNC_RECORDS = 50_000


def assert_loopback_endpoint(value: str, label: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{label} must be an explicit loopback HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain credentials, query, or fragment")
    return value.rstrip("/")


@dataclass
class AwsNetworkAudit:
    local_sdk_attempts: int = 0
    real_aws_attempts: int = 0

    def before_send(self, request: Any, **_kwargs: Any) -> None:
        parsed = urllib.parse.urlparse(str(request.url))
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            self.real_aws_attempts += 1
            raise RuntimeError("blocked non-local AWS SDK request")
        self.local_sdk_attempts += 1


class ClickHouseHttp:
    def __init__(self, base_url: str) -> None:
        self.base_url = assert_loopback_endpoint(base_url, "ClickHouse URL")

    def execute(self, query: str, *, timeout: float = 60) -> str:
        request = urllib.request.Request(
            f"{self.base_url}/?database=loopad",
            data=query.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            error.read()
            raise RuntimeError(f"ClickHouse HTTP status {error.code}") from None

    def json_rows(self, query: str, *, timeout: float = 60) -> list[dict[str, Any]]:
        body = self.execute(f"{query.rstrip().rstrip(';')} FORMAT JSONEachRow", timeout=timeout)
        return [json.loads(line) for line in body.splitlines() if line]

    def one(self, query: str, *, timeout: float = 60) -> dict[str, Any]:
        rows = self.json_rows(query, timeout=timeout)
        if len(rows) != 1:
            raise RuntimeError(f"expected one ClickHouse result row, received {len(rows)}")
        return rows[0]


def create_local_client(service: str, endpoint: str, audit: AwsNetworkAudit) -> Any:
    endpoint = assert_loopback_endpoint(endpoint, f"{service} endpoint")
    session = boto3.session.Session(
        aws_access_key_id=LOCAL_DUMMY_ACCESS_KEY,
        aws_secret_access_key=LOCAL_DUMMY_SECRET_KEY,
        region_name=REGION,
    )
    config_kwargs: dict[str, Any] = {
        "connect_timeout": 2,
        # The in-process LocalStack Kinesis provider can take longer than the
        # AWS service to persist a full 500-record request on Docker Desktop.
        "read_timeout": 60,
        "retries": {"total_max_attempts": 1, "mode": "standard"},
    }
    if service == "s3":
        config_kwargs["s3"] = {"addressing_style": "path"}
    client = session.client(service, endpoint_url=endpoint, config=Config(**config_kwargs))
    client.meta.events.register("before-send", audit.before_send)
    return client


def reset_stream(client: Any, stream_name: str, shard_count: int) -> None:
    existing = set(client.list_streams(Limit=100).get("StreamNames", []))
    if stream_name in existing:
        client.delete_stream(StreamName=stream_name, EnforceConsumerDeletion=True)
        deadline = time.monotonic() + 30
        while stream_name in set(client.list_streams(Limit=100).get("StreamNames", [])):
            if time.monotonic() >= deadline:
                raise RuntimeError("local stream deletion timed out")
            time.sleep(0.2)
    client.create_stream(StreamName=stream_name, ShardCount=shard_count)
    client.get_waiter("stream_exists").wait(
        StreamName=stream_name,
        WaiterConfig={"Delay": 1, "MaxAttempts": 30},
    )


def send_qualified_records(
    client: Any,
    stream_name: str,
    factory: PayloadFactory,
    count: int,
) -> tuple[int, bytes]:
    if count % 500 != 0:
        raise ValueError("qualified producer count must be divisible by 500")
    sender = KinesisBatchSender(client, stream_name, max_attempts=3)
    successful = 0
    sample = b""
    for _ in range(count // 500):
        records = factory.create_batch(500)
        if not sample:
            sample = records[0].data
        result = sender.send(records)
        if (
            result.successful_logical_records != 500
            or result.final_failed_records != 0
            or result.retry_records != 0
            or result.partial_failure_records != 0
            or result.exception
        ):
            raise RuntimeError(
                "qualified producer did not receive clean local acceptance: "
                f"successful={result.successful_logical_records}; "
                f"final_failed={result.final_failed_records}; "
                f"retry_records={result.retry_records}; "
                f"partial_failures={result.partial_failure_records}; "
                f"error_codes={sorted(result.error_codes)}; "
                f"exception_category={result.exception.split(':', 1)[0] if result.exception else 'none'}"
            )
        successful += result.successful_logical_records
    return successful, sample


def mutate_payload(source: bytes, changes: dict[str, Any], removals: Iterable[str] = ()) -> bytes:
    payload = json.loads(source)
    for key in removals:
        payload.pop(key, None)
    payload.update(changes)
    return compact_json(payload)


def put_fixture_records(client: Any, stream_name: str, records: list[tuple[bytes, str]]) -> None:
    response = client.put_records(
        StreamName=stream_name,
        Records=[{"Data": data, "PartitionKey": partition_key} for data, partition_key in records],
    )
    if response.get("FailedRecordCount") != 0 or len(response.get("Records", [])) != len(records):
        raise RuntimeError("local fixture PutRecords failed")


def to_lambda_record(record: dict[str, Any], shard_id: str) -> dict[str, Any]:
    arrival = record.get("ApproximateArrivalTimestamp")
    if isinstance(arrival, datetime):
        arrival_seconds = arrival.timestamp()
    elif isinstance(arrival, (float, int)):
        arrival_seconds = float(arrival)
    else:
        arrival_seconds = datetime.now(timezone.utc).timestamp()
    sequence = str(record["SequenceNumber"])
    data = bytes(record["Data"])
    return {
        "awsRegion": REGION,
        "eventID": f"{shard_id}:{sequence}",
        "eventName": "aws:kinesis:record",
        "eventSource": "aws:kinesis",
        "eventSourceARN": f"arn:aws:kinesis:{REGION}:000000000000:stream/local",
        "eventVersion": "1.0",
        "invokeIdentityArn": "arn:aws:iam::000000000000:role/local",
        "kinesis": {
            "approximateArrivalTimestamp": arrival_seconds,
            "data": base64.b64encode(data).decode("ascii"),
            "kinesisSchemaVersion": "1.0",
            "partitionKey": str(record["PartitionKey"]),
            "sequenceNumber": sequence,
        },
    }


def direct_lambda_record(data: bytes, sequence: int, partition_key: str) -> dict[str, Any]:
    return to_lambda_record(
        {
            "ApproximateArrivalTimestamp": datetime.fromisoformat(RECEIVED_AT.replace("Z", "+00:00")),
            "Data": data,
            "PartitionKey": partition_key,
            "SequenceNumber": str(sequence),
        },
        "shardId-local-direct",
    )


def consume_stream(client: Any, stream_name: str, expected_count: int) -> list[dict[str, Any]]:
    shards = client.list_shards(StreamName=stream_name).get("Shards", [])
    consumed: list[dict[str, Any]] = []
    deadline = time.monotonic() + 90
    for shard in shards:
        shard_id = shard["ShardId"]
        iterator = client.get_shard_iterator(
            StreamName=stream_name,
            ShardId=shard_id,
            ShardIteratorType="TRIM_HORIZON",
        )["ShardIterator"]
        empty_reads = 0
        while iterator and empty_reads < 3:
            if time.monotonic() >= deadline:
                raise RuntimeError("local Kinesis consumption timed out")
            response = client.get_records(ShardIterator=iterator, Limit=10_000)
            iterator = response.get("NextShardIterator")
            records = response.get("Records", [])
            if records:
                consumed.extend(to_lambda_record(record, shard_id) for record in records)
                empty_reads = 0
            else:
                empty_reads += 1
                time.sleep(0.05)
    if len(consumed) != expected_count:
        raise RuntimeError(
            f"local Kinesis count mismatch: expected {expected_count}, received {len(consumed)}"
        )
    return consumed


def split_shard_batches(records: list[dict[str, Any]], size: int = 1_000) -> list[list[dict[str, Any]]]:
    by_shard: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        shard_id = str(record["eventID"]).split(":", 1)[0]
        by_shard[shard_id].append(record)
    return [
        shard_records[index : index + size]
        for shard_records in by_shard.values()
        for index in range(0, len(shard_records), size)
    ]


def invoke_handler(
    batches: list[dict[str, Any]],
    clickhouse_url: str,
    *,
    concurrency: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="loopad-phase4-handler-") as temp_dir:
        input_path = Path(temp_dir) / "input.json"
        output_path = Path(temp_dir) / "output.json"
        input_path.write_text(
            json.dumps(
                {
                    "batches": batches,
                    "clickHouseUrl": clickhouse_url,
                    "concurrency": concurrency,
                    "receivedAt": RECEIVED_AT,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        environment = {
            **os.environ,
            "AWS_ACCESS_KEY_ID": LOCAL_DUMMY_ACCESS_KEY,
            "AWS_SECRET_ACCESS_KEY": LOCAL_DUMMY_SECRET_KEY,
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_REGION": REGION,
        }
        environment.pop("AWS_PROFILE", None)
        environment.pop("AWS_SESSION_TOKEN", None)
        command = [
            str(ROOT / "node_modules/.bin/ts-node"),
            "--transpile-only",
            str(HANDLER_HARNESS),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0 or not output_path.exists():
            detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "no detail"
            raise RuntimeError(f"local TypeScript handler harness failed: {detail[:160]}")
        result = json.loads(output_path.read_text(encoding="utf-8"))
        if result.get("status") != "passed" or result.get("awsSdkCalls") != 0:
            raise RuntimeError("local TypeScript handler harness result failed")
        return result


def quoted(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def run_correctness(kinesis: Any, clickhouse: ClickHouseHttp, clickhouse_url: str) -> dict[str, Any]:
    clickhouse.execute("TRUNCATE TABLE loopad.events", timeout=30)
    clickhouse.execute("TRUNCATE TABLE loopad.raw_events", timeout=30)
    stream_name = "phase4-local-correctness"
    reset_stream(kinesis, stream_name, shard_count=4)
    factory = PayloadFactory(
        PAYLOAD_PATH,
        "run_local_correctness",
        expected_sha256=EXPECTED_POOL_SHA256,
        clock=lambda: RECEIVED_AT,
    )
    accepted, sample = send_qualified_records(
        kinesis,
        stream_name,
        factory,
        CORRECTNESS_RECORDS,
    )
    sample_payload = json.loads(sample)
    expected_properties = sample_payload["properties_json"]
    expected_event_id = sample_payload["event_id"]

    invalid_json = b'{"run_id":"run_local_correctness",'
    missing_required = mutate_payload(factory.create_record().data, {}, removals=("event_id",))
    invalid_timestamp = mutate_payload(
        factory.create_record().data,
        {"event_time": "2026-07-16 11:59:59"},
    )
    late = mutate_payload(
        factory.create_record().data,
        {"event_time": "2026-07-08T23:59:59.999Z"},
    )
    boundary = mutate_payload(
        factory.create_record().data,
        {"event_time": "2026-07-09T00:00:00.000Z"},
    )
    fixture_records = [
        (invalid_json, "invalid-json"),
        (missing_required, "missing-required"),
        (invalid_timestamp, "invalid-timestamp"),
        (late, "late"),
        (boundary, "boundary"),
    ]
    put_fixture_records(kinesis, stream_name, fixture_records)
    emulator_input = accepted + len(fixture_records)
    consumed = consume_stream(kinesis, stream_name, emulator_input)

    duplicate_factory = PayloadFactory(
        PAYLOAD_PATH,
        "run_local_duplicate",
        expected_sha256=EXPECTED_POOL_SHA256,
        clock=lambda: RECEIVED_AT,
    )
    duplicate = duplicate_factory.create_record()
    retry_factory = PayloadFactory(
        PAYLOAD_PATH,
        "run_local_retry",
        expected_sha256=EXPECTED_POOL_SHA256,
        clock=lambda: RECEIVED_AT,
    )
    retry = retry_factory.create_record()
    batches = [
        {"records": records, "mode": "normal"}
        for records in split_shard_batches(consumed)
    ]
    batches.append(
        {
            "records": [direct_lambda_record(retry.data, 9_000_003, retry.partition_key)],
            "mode": "fail-first-then-retry",
        }
    )
    # Keep both physical copies observable until FINAL semantics are measured.
    # ReplacingMergeTree may otherwise merge the tiny local fixture immediately.
    clickhouse.execute("SYSTEM STOP MERGES loopad.events", timeout=30)
    handler = invoke_handler(batches, clickhouse_url, concurrency=8)
    # Deliver the same logical event in two completed invocations. This models
    # an ESM redelivery and keeps async-insert from coalescing both rows into one part.
    for sequence in (9_000_001, 9_000_002):
        invoke_handler(
            [{
                "records": [direct_lambda_record(duplicate.data, sequence, duplicate.partition_key)],
                "mode": "normal",
            }],
            clickhouse_url,
            concurrency=1,
        )

    correctness_counts = clickhouse.one("""
        SELECT
            count() AS physical,
            uniqExact(event_id) AS unique_events
        FROM loopad.events
        WHERE run_id = 'run_local_correctness'
    """)
    correctness_final = clickhouse.one("""
        SELECT count() AS final_rows
        FROM loopad.events FINAL
        WHERE run_id = 'run_local_correctness'
    """)
    raw_rows = clickhouse.json_rows("""
        SELECT raw_payload_base64
        FROM loopad.raw_events
        ORDER BY sequence_number
    """)
    raw_hashes = sorted(
        hashlib.sha256(base64.b64decode(row["raw_payload_base64"])).hexdigest()
        for row in raw_rows
    )
    expected_raw_hashes = sorted(
        hashlib.sha256(data).hexdigest()
        for data in (invalid_json, missing_required, invalid_timestamp)
    )
    preserved = clickhouse.one(f"""
        SELECT properties_json
        FROM loopad.events FINAL
        WHERE event_id = {quoted(expected_event_id)}
    """)
    duplicate_counts = clickhouse.one("""
        SELECT count() AS physical, uniqExact(event_id) AS unique_events
        FROM loopad.events
        WHERE run_id = 'run_local_duplicate'
    """)
    duplicate_final = clickhouse.one("""
        SELECT count() AS final_rows
        FROM loopad.events FINAL
        WHERE run_id = 'run_local_duplicate'
    """)
    retry_counts = clickhouse.one("""
        SELECT count() AS physical, count() AS final_rows
        FROM loopad.events FINAL
        WHERE run_id = 'run_local_retry'
    """)
    clickhouse.execute("SYSTEM START MERGES loopad.events", timeout=30)
    invariant_right = (
        int(correctness_final["final_rows"])
        + len(raw_rows)
        + int(handler["lateEventDropped"])
    )
    checks = {
        "events_physical": int(correctness_counts["physical"]) == 1_001,
        "events_unique": int(correctness_counts["unique_events"]) == 1_001,
        "events_final": int(correctness_final["final_rows"]) == 1_001,
        "raw_count": len(raw_rows) == 3,
        "raw_hashes": raw_hashes == expected_raw_hashes,
        "properties_json": preserved["properties_json"] == expected_properties,
        "duplicate_physical": int(duplicate_counts["physical"]) == 2,
        "duplicate_unique": int(duplicate_counts["unique_events"]) == 1,
        "duplicate_final": int(duplicate_final["final_rows"]) == 1,
        "retry_physical": int(retry_counts["physical"]) == 1,
        "retry_final": int(retry_counts["final_rows"]) == 1,
        "retry_batches": handler["retryBatchCount"] == 1,
        "retry_initial_failures": handler["retryInitialFailures"] == 1,
        "late_metric": handler["lateEventDropped"] == 1,
        "input_invariant": emulator_input == invariant_right,
    }
    failed_checks = [name for name, matched in checks.items() if not matched]
    if failed_checks:
        raise RuntimeError(
            "local correctness invariant failed: "
            + ",".join(failed_checks)
            + f"; duplicate_physical={duplicate_counts['physical']}"
            + f"; late={handler['lateEventDropped']}"
            + f"; input={emulator_input}; invariant_right={invariant_right}"
        )
    return {
        "status": "passed",
        "emulator": "LocalStack Kinesis",
        "streamShards": 4,
        "qualifiedProducerAccepted": accepted,
        "emulatorInput": emulator_input,
        "handlerConsumed": len(consumed),
        "eventsPhysical": int(correctness_counts["physical"]),
        "eventsUnique": int(correctness_counts["unique_events"]),
        "eventsFinal": int(correctness_final["final_rows"]),
        "rawEvents": len(raw_rows),
        "lateEventDropped": int(handler["lateEventDropped"]),
        "countInvariantRight": invariant_right,
        "rawPayloadSha256Matched": True,
        "propertiesJsonByteStringMatched": True,
        "duplicate": {
            "physical": int(duplicate_counts["physical"]),
            "final": int(duplicate_final["final_rows"]),
        },
        "retry": {
            "initialFailures": handler["retryInitialFailures"],
            "finalRows": int(retry_counts["final_rows"]),
        },
    }


def part_snapshot(clickhouse: ClickHouseHttp) -> dict[str, int]:
    row = clickhouse.one("""
        SELECT
            count() AS active_parts,
            coalesce(sum(rows), 0) AS rows,
            (SELECT count() FROM system.merges WHERE database = 'loopad' AND table = 'events') AS merges
        FROM system.parts
        WHERE active AND database = 'loopad' AND table = 'events'
    """)
    return {key: int(row[key]) for key in ("active_parts", "rows", "merges")}


def async_flush_evidence(clickhouse: ClickHouseHttp, started_at: str) -> dict[str, Any]:
    clickhouse.execute("SYSTEM FLUSH LOGS")
    description = clickhouse.json_rows("DESCRIBE TABLE system.asynchronous_insert_log")
    columns = {str(row["name"]) for row in description}
    required = {"event_time", "database", "table", "rows", "bytes"}
    if not required.issubset(columns):
        raise RuntimeError("asynchronous_insert_log is missing required evidence columns")
    group_column = "flush_query_id" if "flush_query_id" in columns else "query_id"
    groups = clickhouse.json_rows(f"""
        SELECT
            {group_column} AS flush_id,
            sum(rows) AS rows,
            sum(bytes) AS bytes,
            count() AS contributing_inserts,
            min(event_time) AS first_event_time,
            max(event_time) AS last_event_time
        FROM system.asynchronous_insert_log
        WHERE event_time >= parseDateTimeBestEffort({quoted(started_at)})
          AND database = 'loopad'
          AND table = 'events'
        GROUP BY {group_column}
        ORDER BY first_event_time
    """)
    if not groups:
        raise RuntimeError("no asynchronous insert flush evidence was recorded")
    flush_rows = [int(group["rows"]) for group in groups]
    flush_bytes = [int(group["bytes"]) for group in groups]
    return {
        "availableColumns": sorted(columns),
        "groupColumn": group_column,
        "flushCount": len(groups),
        "maxRows": max(flush_rows),
        "minRows": min(flush_rows),
        "totalRows": sum(flush_rows),
        "maxBytes": max(flush_bytes),
        "combinedSmallBatchesObserved": max(flush_rows) > 1_000,
        "targetRowsPerFlush": "10000-15000",
        "targetRowsDeviation": max(flush_rows) - 12_500,
    }


def run_async(kinesis: Any, clickhouse: ClickHouseHttp, clickhouse_url: str) -> dict[str, Any]:
    stream_name = "phase4-local-async"
    reset_stream(kinesis, stream_name, shard_count=8)
    factory = PayloadFactory(
        PAYLOAD_PATH,
        "run_local_async",
        expected_sha256=EXPECTED_POOL_SHA256,
        clock=lambda: RECEIVED_AT,
    )
    accepted, _sample = send_qualified_records(
        kinesis,
        stream_name,
        factory,
        ASYNC_RECORDS,
    )
    consumed = consume_stream(kinesis, stream_name, ASYNC_RECORDS)
    batches = [
        {"records": records, "mode": "normal"}
        for records in split_shard_batches(consumed, size=1_000)
    ]
    started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    started_monotonic = time.monotonic()
    handler = invoke_handler(batches, clickhouse_url, concurrency=16)
    insert_seconds = time.monotonic() - started_monotonic
    counts = clickhouse.one("""
        SELECT count() AS physical, uniqExact(event_id) AS unique_events
        FROM loopad.events
        WHERE run_id = 'run_local_async'
    """, timeout=60)
    final_count = clickhouse.one("""
        SELECT count() AS final_rows
        FROM loopad.events FINAL
        WHERE run_id = 'run_local_async'
    """, timeout=60)
    first_parts = part_snapshot(clickhouse)
    time.sleep(5)
    second_parts = part_snapshot(clickhouse)
    flushes = async_flush_evidence(clickhouse, started_at)
    flushes["observedFlushesPerSecond"] = round(flushes["flushCount"] / max(insert_seconds, 0.001), 6)
    flushes["targetFlushesPerSecond"] = 4
    flushes["flushRateDeviation"] = round(flushes["observedFlushesPerSecond"] - 4, 6)
    persistently_growing = (
        second_parts["active_parts"] > first_parts["active_parts"]
        and second_parts["merges"] > 0
    )
    passed = all([
        accepted == ASYNC_RECORDS,
        len(consumed) == ASYNC_RECORDS,
        handler["inputLogicalRecords"] == ASYNC_RECORDS,
        handler["lateEventDropped"] == 0,
        int(counts["physical"]) == ASYNC_RECORDS,
        int(counts["unique_events"]) == ASYNC_RECORDS,
        int(final_count["final_rows"]) == ASYNC_RECORDS,
        flushes["combinedSmallBatchesObserved"] is True,
        not persistently_growing,
    ])
    if not passed:
        raise RuntimeError("local async flush invariant failed")
    return {
        "status": "passed",
        "emulatorInput": accepted,
        "handlerConsumed": len(consumed),
        "batchCount": len(batches),
        "handlerConcurrency": 16,
        "eventsPhysical": int(counts["physical"]),
        "eventsUnique": int(counts["unique_events"]),
        "eventsFinal": int(final_count["final_rows"]),
        "insertSeconds": round(insert_seconds, 6),
        "flushes": flushes,
        "partsFirst": first_parts,
        "partsSecond": second_parts,
        "persistentlyGrowingBacklog": persistently_growing,
    }


def archive_metrics(clickhouse: ClickHouseHttp, source: str) -> dict[str, Any]:
    return clickhouse.one(f"""
        SELECT
            count() AS rows,
            uniqExact(tuple(project_id, event_id)) AS unique_events,
            min(toUnixTimestamp64Milli(event_time)) AS min_event_time_ms,
            max(toUnixTimestamp64Milli(event_time)) AS max_event_time_ms,
            toString(sum(cityHash64(project_id, event_id, toString(event_time), properties_json))) AS checksum
        FROM {source}
    """)


def reset_archive_bucket(s3: Any, bucket: str) -> None:
    buckets = {item["Name"] for item in s3.list_buckets().get("Buckets", [])}
    if bucket in buckets:
        objects = s3.list_objects_v2(Bucket=bucket).get("Contents", [])
        if objects:
            s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": item["Key"]} for item in objects]},
            )
        s3.delete_bucket(Bucket=bucket)
    s3.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )


def run_archive(s3: Any, clickhouse: ClickHouseHttp, clickhouse_url: str) -> dict[str, Any]:
    bucket = "phase4-local-archive"
    key = "loopad/events/event_date=2026-07-01/fixture.parquet"
    reset_archive_bucket(s3, bucket)
    clickhouse.execute("""
        INSERT INTO loopad.events
        (
            project_id, write_key, schema_version, event_id, event_name, event_time,
            source, user_id, session_id, properties_json, producer_sent_at, run_id,
            kinesis_shard_id, kinesis_sequence_number
        )
        SELECT
            'archive-project',
            'archive-fixture-write-key',
            'hotel_rec_promo.v1',
            concat('archive-event-', toString(number)),
            'archive_fixture',
            toDateTime64('2026-07-01 12:00:00.000', 3, 'UTC') + toIntervalSecond(number),
            'phase4-local',
            NULL,
            NULL,
            '{"fixture":true}',
            NULL,
            'run_local_archive',
            'shardId-local-archive',
            number + 1
        FROM numbers(25)
    """)
    source_relation = """
        (SELECT * FROM loopad.events FINAL
         WHERE run_id = 'run_local_archive' AND event_date = toDate('2026-07-01'))
    """
    source_before = archive_metrics(clickhouse, source_relation)
    s3_url = f"http://localstack:4566/{bucket}/{key}"
    s3_function = (
        f"s3({quoted(s3_url)}, {quoted(LOCAL_DUMMY_ACCESS_KEY)}, "
        f"{quoted(LOCAL_DUMMY_SECRET_KEY)}, 'Parquet')"
    )
    clickhouse.execute(f"""
        INSERT INTO FUNCTION {s3_function}
        SELECT * FROM loopad.events FINAL
        WHERE run_id = 'run_local_archive' AND event_date = toDate('2026-07-01')
    """, timeout=60)
    head = s3.head_object(Bucket=bucket, Key=key)
    s3_before = archive_metrics(clickhouse, s3_function)
    if source_before != s3_before:
        raise RuntimeError("archive source/S3 pre-DROP equivalence failed")

    # This statement is intentionally below the equality gate.
    clickhouse.execute("ALTER TABLE loopad.events DROP PARTITION '2026-07-01'")
    remaining = clickhouse.one("""
        SELECT count() AS rows
        FROM loopad.events
        WHERE event_date = toDate('2026-07-01')
    """)
    s3_after = archive_metrics(clickhouse, s3_function)
    if int(remaining["rows"]) != 0 or s3_after != source_before:
        raise RuntimeError("archive post-DROP direct S3 equivalence failed")

    late_factory = PayloadFactory(
        PAYLOAD_PATH,
        "run_local_archive_late",
        expected_sha256=EXPECTED_POOL_SHA256,
        clock=lambda: RECEIVED_AT,
    )
    late = late_factory.create_record()
    late_data = mutate_payload(late.data, {"event_time": "2026-07-01T12:00:00.000Z"})
    handler = invoke_handler(
        [{
            "records": [direct_lambda_record(late_data, 9_100_001, late.partition_key)],
            "mode": "normal",
        }],
        clickhouse_url,
        concurrency=1,
    )
    partition_after_late = clickhouse.one("""
        SELECT count() AS rows
        FROM loopad.events
        WHERE event_date = toDate('2026-07-01')
    """)
    if handler["lateEventDropped"] != 1 or int(partition_after_late["rows"]) != 0:
        raise RuntimeError("late event recreated the archived partition")
    return {
        "status": "passed",
        "fixtureRows": int(source_before["rows"]),
        "objectKey": key,
        "objectBytes": int(head["ContentLength"]),
        "preDropEquivalent": source_before == s3_before,
        "dropIssuedAfterPrecheck": True,
        "sourceRowsAfterDrop": int(remaining["rows"]),
        "postDropDirectS3Equivalent": s3_after == source_before,
        "lateEventDropped": int(handler["lateEventDropped"]),
        "partitionRowsAfterLate": int(partition_after_late["rows"]),
        "metrics": source_before,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("all", "correctness", "async", "archive"), default="all")
    parser.add_argument("--kinesis-endpoint", default="http://127.0.0.1:14566")
    parser.add_argument("--clickhouse-url", default="http://127.0.0.1:18123")
    parser.add_argument("--output", type=Path, default=Path("/tmp/loopad-phase4-local-result.json"))
    args = parser.parse_args()

    started = datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "status": "running",
        "startedAt": started.isoformat(timespec="seconds"),
        "suite": args.suite,
        "region": REGION,
        "fixedReceivedAt": RECEIVED_AT,
        "payloadSha256": EXPECTED_POOL_SHA256,
        "clickHouseImage": "clickhouse/clickhouse-server:26.3.13.31",
        "kinesisEmulatorImage": "localstack/localstack:3.8.1",
    }
    audit = AwsNetworkAudit()
    try:
        kinesis_endpoint = assert_loopback_endpoint(args.kinesis_endpoint, "Kinesis endpoint")
        clickhouse_url = assert_loopback_endpoint(args.clickhouse_url, "ClickHouse URL")
        kinesis = create_local_client("kinesis", kinesis_endpoint, audit)
        s3 = create_local_client("s3", kinesis_endpoint, audit)
        clickhouse = ClickHouseHttp(clickhouse_url)
        result["clickHouseVersion"] = clickhouse.one("SELECT version() AS version")["version"]
        if args.suite in {"all", "correctness"}:
            result["correctness"] = run_correctness(kinesis, clickhouse, clickhouse_url)
        if args.suite in {"all", "async"}:
            result["asyncFlush"] = run_async(kinesis, clickhouse, clickhouse_url)
        if args.suite in {"all", "archive"}:
            result["archive"] = run_archive(s3, clickhouse, clickhouse_url)
        if audit.real_aws_attempts != 0:
            raise RuntimeError("real AWS API attempt count is nonzero")
        result["networkAudit"] = {
            "localAwsSdkAttempts": audit.local_sdk_attempts,
            "realAwsApiAttempts": audit.real_aws_attempts,
            "sdkEndpointGuard": "loopback-only",
            "metadataDisabledForNodeHarness": True,
        }
        result["status"] = "passed"
    except Exception as error:
        result["status"] = "failed"
        result["failureCategory"] = type(error).__name__
        result["failureMessage"] = str(error)[:256]
        result["networkAudit"] = {
            "localAwsSdkAttempts": audit.local_sdk_attempts,
            "realAwsApiAttempts": audit.real_aws_attempts,
            "sdkEndpointGuard": "loopback-only",
        }
    completed = datetime.now(timezone.utc)
    result["completedAt"] = completed.isoformat(timespec="seconds")
    result["elapsedSeconds"] = round((completed - started).total_seconds(), 6)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(args.output)}, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

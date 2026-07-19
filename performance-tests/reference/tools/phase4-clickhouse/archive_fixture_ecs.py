#!/usr/bin/env python3
"""Prove archive equivalence before and after a guarded ClickHouse partition drop."""

from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from cleanup_inventory_ecs import owned, tag_map
from ecs_run_support import (
    AwsRun,
    load_bundle,
    make_valid_record,
    wait_until,
    write_private,
)
from evaluate_full_load_ecs import remaining_validation_seconds
from run_full_load_ecs import one_row


FIXTURE_ROWS = 25
MINIMUM_REMAINING_SECONDS = 300


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_bundle(args.run_dir)
    run_document = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
    remaining = remaining_validation_seconds(run_document, datetime.now(UTC))
    if remaining < MINIMUM_REMAINING_SECONDS:
        raise RuntimeError("insufficient time remains before the 100-minute cleanup deadline")

    aws = AwsRun(bundle)
    identity = aws.assert_identity()
    bucket = bundle.outputs["ArchiveBucketName"]
    verify_bucket_ownership(aws, bucket, bundle.run_id, bundle.session_id)
    fixture_date = archive_fixture_date(datetime.now(UTC).date())
    prefix = f"loopad/events/run_id={bundle.run_id}/event_date={fixture_date.isoformat()}"
    parquet_key = f"{prefix}/fixture.parquet"
    manifest_key = f"{prefix}/manifest.json"
    s3_url = archive_s3_url(bucket, parquet_key)

    initial_partition = one_row(aws.clickhouse_rows(partition_count_query(fixture_date)))
    if int(initial_partition["rows"]) != 0:
        raise RuntimeError("archive fixture partition is not empty; refusing to mutate it")
    clickhouse_execute(aws, archive_insert_query(bundle.run_id, fixture_date))
    source_relation = archive_source_relation(bundle.run_id, fixture_date)
    source_before = one_row(aws.clickhouse_rows(archive_metrics_query(source_relation)))
    partition_after_insert = one_row(aws.clickhouse_rows(partition_count_query(fixture_date)))
    if (
        int(source_before["rows"]) != FIXTURE_ROWS
        or int(source_before["unique_events"]) != FIXTURE_ROWS
        or int(partition_after_insert["rows"]) != FIXTURE_ROWS
    ):
        raise RuntimeError("archive fixture source safety gate failed")

    clickhouse_execute(aws, archive_export_query(s3_url, bundle.run_id, fixture_date))
    parquet_head = aws.client("s3").head_object(Bucket=bucket, Key=parquet_key)
    s3_relation = archive_s3_relation(s3_url)
    s3_before = one_row(aws.clickhouse_rows(archive_metrics_query(s3_relation)))
    pre_drop_equivalent = normalize_metrics(source_before) == normalize_metrics(s3_before)
    if not pre_drop_equivalent:
        raise RuntimeError("archive source/S3 equivalence failed; partition was not dropped")

    manifest = {
        "schemaVersion": 1,
        "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "runId": bundle.run_id,
        "sessionId": bundle.session_id,
        "partition": fixture_date.isoformat(),
        "parquet": {
            "bucket": bucket,
            "key": parquet_key,
            "bytes": int(parquet_head["ContentLength"]),
            "etag": str(parquet_head["ETag"]).strip('"'),
        },
        "sourceMetrics": normalize_metrics(source_before),
        "s3MetricsBeforeDrop": normalize_metrics(s3_before),
        "preDropEquivalent": True,
        "credentialMode": "ClickHouse EC2 instance role via S3 gateway endpoint",
    }
    local_manifest = args.run_dir / "archive-manifest-ecs.json"
    if local_manifest.exists():
        raise FileExistsError("archive manifest evidence already exists")
    write_private(local_manifest, manifest)
    upload_manifest_from_clickhouse(aws, bucket, manifest_key, manifest)
    manifest_head = aws.client("s3").head_object(Bucket=bucket, Key=manifest_key)

    if remaining_validation_seconds(run_document, datetime.now(UTC)) <= 0:
        raise RuntimeError("cleanup deadline reached after archive copy; partition was not dropped")
    clickhouse_execute(aws, archive_drop_query(fixture_date))
    source_after = one_row(aws.clickhouse_rows(partition_count_query(fixture_date)))
    s3_after = one_row(aws.clickhouse_rows(archive_metrics_query(s3_relation)))
    post_drop_equivalent = (
        int(source_after["rows"]) == 0
        and normalize_metrics(s3_after) == normalize_metrics(source_before)
    )
    if not post_drop_equivalent:
        raise RuntimeError("archive post-DROP direct S3 equivalence failed")

    late_before = aws.metric_sum(
        "LoopAd/Phase4",
        "LateEventDropped",
        [{"Name": "RunId", "Value": bundle.run_id}],
        minutes=30,
    )
    late_data, late_event_id = make_late_archive_record(bundle.run_id, fixture_date)
    accepted = aws.put_records([{"Data": late_data, "PartitionKey": late_event_id}])
    late_after = wait_until(
        "archive late-event metric",
        args.timeout_seconds,
        15,
        lambda: aws.metric_sum(
            "LoopAd/Phase4",
            "LateEventDropped",
            [{"Name": "RunId", "Value": bundle.run_id}],
            minutes=30,
        ),
        lambda value: value >= late_before + 1,
    )
    partition_after_late = one_row(aws.clickhouse_rows(partition_count_query(fixture_date)))
    s3_after_late = one_row(aws.clickhouse_rows(archive_metrics_query(s3_relation)))
    failure_objects = aws.failure_object_count()
    checks = {
        "fixtureRowsExact": int(source_before["rows"]) == FIXTURE_ROWS,
        "preDropEquivalent": pre_drop_equivalent,
        "manifestUploadedBeforeDrop": int(manifest_head["ContentLength"]) > 0,
        "sourceDeletedAfterGuard": int(source_after["rows"]) == 0,
        "postDropDirectS3Equivalent": post_drop_equivalent,
        "lateInputAccepted": accepted["inputRecords"] == 1,
        "lateMetricDeltaExact": int(late_after - late_before) == 1,
        "lateDidNotRecreatePartition": int(partition_after_late["rows"]) == 0,
        "s3StableAfterLate": normalize_metrics(s3_after_late) == normalize_metrics(source_before),
        "terminalFailureObjectsZero": failure_objects == 0,
    }
    result = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "runId": bundle.run_id,
        "sessionId": bundle.session_id,
        "identity": identity,
        "partition": fixture_date.isoformat(),
        "parquet": manifest["parquet"],
        "manifest": {"bucket": bucket, "key": manifest_key, "bytes": int(manifest_head["ContentLength"])},
        "metrics": {
            "sourceBefore": normalize_metrics(source_before),
            "s3BeforeDrop": normalize_metrics(s3_before),
            "sourceRowsAfterDrop": int(source_after["rows"]),
            "s3AfterDrop": normalize_metrics(s3_after),
            "s3AfterLate": normalize_metrics(s3_after_late),
            "lateEventDroppedBefore": late_before,
            "lateEventDroppedAfter": late_after,
        },
        "dropIssuedAfterPrecheck": True,
        "lateEventId": late_event_id,
        "failureObjects": failure_objects,
        "checks": checks,
        "pass": all(checks.values()),
    }
    write_private(args.run_dir / "archive-fixture-ecs.json", result)
    print(json.dumps({"checks": checks, "pass": result["pass"]}, indent=2))
    return 0 if result["pass"] else 2


def archive_fixture_date(today: date) -> date:
    return today - timedelta(days=8)


def validate_bucket_name(bucket: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("invalid S3 bucket name")


def archive_s3_url(bucket: str, key: str) -> str:
    validate_bucket_name(bucket)
    if not re.fullmatch(r"[A-Za-z0-9._=/-]+", key) or ".." in key.split("/"):
        raise ValueError("invalid archive object key")
    return f"https://{bucket}.s3.ap-northeast-2.amazonaws.com/{key}"


def verify_bucket_ownership(
    aws: AwsRun,
    bucket: str,
    run_id: str,
    session_id: str,
) -> None:
    tags = tag_map(aws.client("s3").get_bucket_tagging(Bucket=bucket)["TagSet"])
    if not owned(tags, run_id, session_id):
        raise RuntimeError("archive bucket ownership mismatch")


def clickhouse_execute(aws: AwsRun, query: str, timeout_seconds: int = 600) -> str:
    encoded = base64.b64encode(query.encode("utf-8")).decode("ascii")
    command = (
        f"printf '%s' {shlex.quote(encoded)} | base64 -d | "
        "docker exec -i phase4-clickhouse clickhouse-client --multiquery"
    )
    return aws.run_ssm([command], timeout_seconds=timeout_seconds)


def partition_count_query(fixture_date: date) -> str:
    return (
        "SELECT count() AS rows FROM loopad.events "
        f"WHERE event_date = toDate('{fixture_date.isoformat()}')"
    )


def archive_insert_query(run_id: str, fixture_date: date) -> str:
    if not re.fullmatch(r"run_[0-9]{8}_[0-9]{6}_phase4_clickhouse_ecs", run_id):
        raise ValueError("invalid run id")
    day = fixture_date.isoformat()
    return f"""
INSERT INTO loopad.events
(
    project_id, write_key, schema_version, event_id, event_name, event_time,
    source, user_id, session_id, properties_json, producer_sent_at, run_id,
    kinesis_shard_id, kinesis_sequence_number
)
SELECT
    'archive-project-{run_id}',
    'archive-fixture-write-key',
    'hotel_rec_promo.v1',
    concat('archive-{run_id}-', toString(number)),
    'archive_fixture',
    toDateTime64('{day} 12:00:00.000', 3, 'UTC') + toIntervalSecond(number),
    'phase4-ecs-archive',
    NULL,
    NULL,
    '{{"fixture":true}}',
    NULL,
    '{run_id}',
    'archive-fixture',
    number + 9000000
FROM numbers({FIXTURE_ROWS})
""".strip()


def archive_source_relation(run_id: str, fixture_date: date) -> str:
    return (
        "(SELECT * FROM loopad.events FINAL "
        f"WHERE run_id = '{run_id}' AND event_date = toDate('{fixture_date.isoformat()}'))"
    )


def archive_s3_relation(s3_url: str) -> str:
    if "'" in s3_url:
        raise ValueError("invalid S3 URL")
    return f"s3('{s3_url}', 'Parquet')"


def archive_metrics_query(relation: str) -> str:
    return f"""
SELECT
    count() AS rows,
    uniqExact(tuple(project_id, event_id)) AS unique_events,
    min(toUnixTimestamp64Milli(event_time)) AS min_event_time_ms,
    max(toUnixTimestamp64Milli(event_time)) AS max_event_time_ms,
    toString(sum(cityHash64(project_id, event_id, toString(event_time), properties_json))) AS checksum
FROM {relation}
""".strip()


def archive_export_query(s3_url: str, run_id: str, fixture_date: date) -> str:
    return (
        f"INSERT INTO FUNCTION {archive_s3_relation(s3_url)} "
        "SELECT * FROM loopad.events FINAL "
        f"WHERE run_id = '{run_id}' AND event_date = toDate('{fixture_date.isoformat()}')"
    )


def archive_drop_query(fixture_date: date) -> str:
    return f"ALTER TABLE loopad.events DROP PARTITION '{fixture_date.isoformat()}'"


def normalize_metrics(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "rows": int(document["rows"]),
        "uniqueEvents": int(document["unique_events"]),
        "minEventTimeMs": int(document["min_event_time_ms"]),
        "maxEventTimeMs": int(document["max_event_time_ms"]),
        "checksum": str(document["checksum"]),
    }


def upload_manifest_from_clickhouse(
    aws: AwsRun,
    bucket: str,
    key: str,
    manifest: dict[str, Any],
) -> None:
    body = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    encoded = base64.b64encode(body).decode("ascii")
    uri = f"s3://{bucket}/{key}"
    command = (
        f"printf '%s' {shlex.quote(encoded)} | base64 -d | "
        f"aws s3 cp - {shlex.quote(uri)} --only-show-errors --sse AES256 "
        "--content-type application/json"
    )
    aws.run_ssm([command], timeout_seconds=180)


def make_late_archive_record(run_id: str, fixture_date: date) -> tuple[bytes, str]:
    data, event_id = make_valid_record(run_id)
    body = json.loads(data)
    timestamp = f"{fixture_date.isoformat()}T12:00:00.000Z"
    body["event_time"] = timestamp
    body["producer_sent_at"] = timestamp
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), event_id


if __name__ == "__main__":
    raise SystemExit(main())

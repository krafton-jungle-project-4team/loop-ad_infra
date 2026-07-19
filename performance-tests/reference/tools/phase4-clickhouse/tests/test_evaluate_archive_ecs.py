from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from performance_tests_import import load_phase4_module


EVALUATE = load_phase4_module("evaluate_full_load_ecs.py", "phase4_evaluate_full_load_ecs")
ARCHIVE = load_phase4_module("archive_fixture_ecs.py", "phase4_archive_fixture_ecs")


def test_remaining_validation_seconds_uses_hard_100_minute_boundary() -> None:
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    run = {"paidWallClockStartedAt": (now - timedelta(minutes=99)).isoformat()}
    assert EVALUATE.remaining_validation_seconds(run, now) == 60
    assert EVALUATE.remaining_validation_seconds(
        {"paidWallClockStartedAt": (now - timedelta(minutes=100)).isoformat()},
        now,
    ) == 0


def test_completion_query_isolates_only_the_measurement_window() -> None:
    query = EVALUATE.completion_query(
        "run_20260716_120000_phase4_clickhouse_ecs",
        100,
        400,
    )
    assert "producer_sent_at >= toDateTime(100, 'UTC')" in query
    assert "producer_sent_at < toDateTime(400, 'UTC')" in query
    assert "loopad.events FINAL" in query
    assert "loopad.raw_events" in query


def test_completion_requires_exact_logical_count_and_bounded_iterator_age() -> None:
    snapshot = {
        "events_final": 15_000_000,
        "events_unique": 15_000_000,
        "raw_events": 0,
        "iterator_age_ms": 0,
    }
    assert EVALUATE.completion_ready(snapshot)
    assert not EVALUATE.completion_ready(dict(snapshot, events_unique=14_999_999))
    assert not EVALUATE.completion_ready(dict(snapshot, iterator_age_ms=1_001))


def test_clickhouse_log_query_covers_async_and_insert_errors() -> None:
    query = EVALUATE.clickhouse_log_query(
        100,
        datetime.fromtimestamp(500, UTC),
    )
    assert "system.asynchronous_insert_log" in query
    assert "system.query_log" in query
    assert "ExceptionWhileProcessing" in query


def test_kcl_metric_stats_preserve_success_and_lag_semantics() -> None:
    assert EVALUATE.kcl_metric_stat("Success") == "Minimum"
    assert EVALUATE.kcl_metric_stat("RecordsProcessed") == "Sum"
    assert EVALUATE.kcl_metric_stat("MillisBehindLatest") == "Maximum"
    assert EVALUATE.kcl_metric_stat("ProcessTask.Time") == "Maximum"


def test_kcl_summary_counts_only_most_detailed_record_series() -> None:
    series = [
        {
            "metricName": "RecordsProcessed",
            "datapoints": 1,
            "dimensions": [{"Name": "Operation", "Value": "ProcessTask"}],
            "sum": 100,
            "minimum": 100,
            "maximum": 100,
        },
        {
            "metricName": "RecordsProcessed",
            "datapoints": 1,
            "dimensions": [
                {"Name": "Operation", "Value": "ProcessTask"},
                {"Name": "ShardId", "Value": "shard-1"},
            ],
            "sum": 60,
            "minimum": 60,
            "maximum": 60,
        },
        {
            "metricName": "RecordsProcessed",
            "datapoints": 1,
            "dimensions": [
                {"Name": "Operation", "Value": "ProcessTask"},
                {"Name": "ShardId", "Value": "shard-2"},
            ],
            "sum": 40,
            "minimum": 40,
            "maximum": 40,
        },
        {
            "metricName": "Success",
            "datapoints": 2,
            "dimensions": [{"Name": "Operation", "Value": "ProcessTask"}],
            "sum": 2,
            "minimum": 1,
            "maximum": 1,
        },
    ]
    summary = EVALUATE.summarize_kcl_metrics(series)
    assert summary["recordsProcessed"] == 100
    assert summary["recordsProcessedSeries"] == 2
    assert summary["successMinimum"] == 1


def test_kcl_worker_identifier_catalog_is_bounded_by_actual_dimension_values() -> None:
    series = [
        {
            "dimensions": [
                {"Name": "Operation", "Value": "ProcessTask"},
                {"Name": "WorkerIdentifier", "Value": "worker-b"},
            ]
        },
        {
            "dimensions": [
                {"Name": "WorkerIdentifier", "Value": "worker-a"},
            ]
        },
        {"dimensions": [{"Name": "Operation", "Value": "ShardSyncTask"}]},
    ]
    assert EVALUATE.worker_identifiers(series) == ["worker-a", "worker-b"]


def test_host_metric_evidence_requires_two_complete_host_timelines() -> None:
    complete = {
        "cpu": {"datapoints": 5},
        "memory": {"datapoints": 5},
        "networkIn": {"datapoints": 5},
        "networkOut": {"datapoints": 5},
    }
    assert EVALUATE.host_metric_evidence_present([complete, complete])
    assert not EVALUATE.host_metric_evidence_present([complete])
    assert not EVALUATE.host_metric_evidence_present([
        complete,
        {**complete, "memory": {"datapoints": 0}},
    ])


def test_archive_fixture_uses_eight_day_old_partition_and_role_only_s3_shape() -> None:
    fixture_date = ARCHIVE.archive_fixture_date(date(2026, 7, 16))
    assert fixture_date == date(2026, 7, 8)
    url = ARCHIVE.archive_s3_url(
        "phase4-owned-bucket",
        "loopad/events/run_id=run/event_date=2026-07-08/fixture.parquet",
    )
    relation = ARCHIVE.archive_s3_relation(url)
    assert "s3('https://phase4-owned-bucket.s3.ap-northeast-2.amazonaws.com/" in relation
    assert "AWS_ACCESS_KEY" not in relation
    assert "Parquet" in relation


def test_archive_queries_freeze_guarded_fixture_shape() -> None:
    run_id = "run_20260716_120000_phase4_clickhouse_ecs"
    fixture_date = date(2026, 7, 8)
    insert = ARCHIVE.archive_insert_query(run_id, fixture_date)
    export = ARCHIVE.archive_export_query(
        "https://bucket-name.s3.ap-northeast-2.amazonaws.com/loopad/events/fixture.parquet",
        run_id,
        fixture_date,
    )
    assert "FROM numbers(25)" in insert
    assert "INSERT INTO FUNCTION s3(" in export
    assert "events FINAL" in export
    assert ARCHIVE.archive_drop_query(fixture_date).endswith("'2026-07-08'")


def test_late_archive_record_keeps_event_id_and_targets_dropped_partition() -> None:
    data, event_id = ARCHIVE.make_late_archive_record(
        "run_20260716_120000_phase4_clickhouse_ecs",
        date(2026, 7, 8),
    )
    body = json.loads(data)
    assert body["event_id"] == event_id
    assert body["event_time"] == "2026-07-08T12:00:00.000Z"
    assert body["producer_sent_at"] == body["event_time"]

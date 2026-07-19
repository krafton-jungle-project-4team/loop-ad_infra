#!/usr/bin/env python3
"""Evaluate the immutable producer evidence and live Phase 4 ECS full-load result."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aws_correctness_smoke_ecs import leases_balanced, service_ready
from ecs_run_support import (
    AwsRun,
    QUALIFIED_IMPLEMENTATION,
    load_bundle,
    parse_utc,
    wait_until,
    write_private,
)
from run_full_load_ecs import EXPECTED_RECORDS, clickhouse_snapshot_query, one_row


COLLECTOR = QUALIFIED_IMPLEMENTATION / "collect_cloudwatch.mjs"
ANALYZER = QUALIFIED_IMPLEMENTATION / "analyze.py"
TARGET_RPS = 50_000
EXPECTED_WORKERS = 8
PRODUCER_NETWORK_GBPS = 3.75
EXPECTED_LEASES = 120
ITERATOR_AGE_LIMIT_MS = 1_000
CLEANUP_START_MINUTES = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--drain-timeout-seconds", type=int, default=1_800)
    parser.add_argument("--cloudwatch-timeout-seconds", type=int, default=300)
    parser.add_argument("--settle-seconds", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_bundle(args.run_dir)
    run_document = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
    full_load = json.loads(
        (args.run_dir / "producer-full-load-ecs.json").read_text(encoding="utf-8")
    )
    manifest = full_load["stageManifest"]
    start_epoch = int(manifest["measurementStartEpoch"])
    end_epoch = int(manifest["measurementEndEpoch"])
    if end_epoch - start_epoch != 300:
        raise RuntimeError("producer measurement window is not exactly 300 seconds")
    remaining = remaining_validation_seconds(run_document, datetime.now(UTC))
    if remaining <= 0:
        raise RuntimeError("100-minute cleanup deadline reached before full-load evaluation")

    aws = AwsRun(bundle)
    identity = aws.assert_identity()
    producer_analysis = collect_original_producer_analysis(
        args.run_dir,
        bundle.outputs["StreamName"],
        manifest,
        args.cloudwatch_timeout_seconds,
    )
    producer_exact = producer_analysis.get("producer", {}).get(
        "successfulLogicalRecords"
    ) == EXPECTED_RECORDS

    completion_timed_out = False
    drain_timeout = max(1, min(args.drain_timeout_seconds, remaining))
    if producer_exact:
        try:
            completion = wait_until(
                "15,000,000 logical ClickHouse rows and zero iterator age",
                drain_timeout,
                15,
                lambda: completion_snapshot(aws, bundle.run_id, start_epoch, end_epoch),
                completion_ready,
            )
        except TimeoutError:
            completion_timed_out = True
            completion = completion_snapshot(aws, bundle.run_id, start_epoch, end_epoch)
    else:
        completion = completion_snapshot(aws, bundle.run_id, start_epoch, end_epoch)

    completed_at = datetime.now(UTC)
    drain_seconds = max(0.0, completed_at.timestamp() - end_epoch)
    service = aws.service_snapshot()
    leases = aws.lease_snapshot()
    stopped_tasks = stopped_task_snapshot(aws, start_epoch, completed_at)
    logs = consumer_error_logs(aws, start_epoch, completed_at)
    terminal_failure = metric_window_sum(
        aws,
        "LoopAd/Phase4",
        "TerminalFailure",
        [{"Name": "RunId", "Value": bundle.run_id}],
        start_epoch,
        completed_at,
    )
    checkpoint_error = metric_window_sum(
        aws,
        "LoopAd/Phase4",
        "CheckpointError",
        [{"Name": "RunId", "Value": bundle.run_id}],
        start_epoch,
        completed_at,
    )
    read_throttle = metric_window_sum(
        aws,
        "AWS/Kinesis",
        "ReadProvisionedThroughputExceeded",
        [{"Name": "StreamName", "Value": bundle.outputs["StreamName"]}],
        start_epoch,
        completed_at,
    )
    task_cpu = percentile_metric(
        aws,
        "AWS/ECS",
        "CPUUtilization",
        service_dimensions(bundle.outputs),
        start_epoch,
        end_epoch,
    )
    task_memory = percentile_metric(
        aws,
        "AWS/ECS",
        "MemoryUtilization",
        service_dimensions(bundle.outputs),
        start_epoch,
        end_epoch,
    )
    host_metrics = collect_host_metrics(aws, service, start_epoch, end_epoch)
    container_insights = collect_container_insights(
        aws,
        bundle.outputs["ConsumerClusterName"],
        start_epoch,
        end_epoch,
    )
    kcl_metrics = collect_kcl_metrics(
        aws,
        start_epoch,
        int(completed_at.timestamp()) + 60,
    )

    flush_clickhouse_logs(aws)
    clickhouse_logs = one_row(aws.clickhouse_rows(
        clickhouse_log_query(start_epoch, completed_at)
    ))
    parts_first = one_row(aws.clickhouse_rows(clickhouse_snapshot_query()))
    settle = max(0, min(args.settle_seconds, remaining_validation_seconds(
        run_document, datetime.now(UTC)
    )))
    if settle:
        time.sleep(settle)
    parts_second = one_row(aws.clickhouse_rows(clickhouse_snapshot_query()))
    parts_persistently_growing = (
        int(parts_second["active_parts"]) > int(parts_first["active_parts"])
        and int(parts_second["active_merges"]) > 0
    )
    failure_objects = aws.failure_object_count()

    producer_checks = producer_analysis.get("checks", {})
    checks = {
        "producerOriginalAnalysisPassed": producer_analysis.get("passed") is True,
        "producerLogicalSuccessExact": producer_analysis.get("producer", {}).get(
            "successfulLogicalRecords"
        ) == EXPECTED_RECORDS,
        "producerRetryAndFailureZero": all(
            producer_analysis.get("producer", {}).get(name) == 0
            for name in [
                "partialFailureRecords",
                "declaredFailedRecords",
                "retryRecords",
                "finalFailedRecords",
            ]
        ),
        "producerCloudWatchExact": (
            producer_analysis.get("cloudWatch", {}).get("IncomingRecords")
            == EXPECTED_RECORDS
            and producer_checks.get("cloudWatchRequiredMetricsCoverMeasurementWindow") is True
        ),
        "clickHouseLogicalCountExact": (
            int(completion["events_final"]) == EXPECTED_RECORDS
            and int(completion["events_unique"]) == EXPECTED_RECORDS
        ),
        "clickHouseRawUnexpectedZero": int(completion["raw_events"]) == 0,
        "drainWithinThirtyMinutes": (
            not completion_timed_out
            and drain_seconds <= 1_800
            and completion.get("iterator_age_ms") is not None
            and float(completion["iterator_age_ms"]) <= ITERATOR_AGE_LIMIT_MS
        ),
        "serviceStillReady": service_ready(service),
        "leasesAndCheckpointsComplete": (
            leases_balanced(leases)
            and leases["numericCheckpointCount"] == EXPECTED_LEASES
        ),
        "taskCpuP95Below70": task_cpu["p95Maximum"] is not None
        and float(task_cpu["p95Maximum"]) < 70,
        "taskMemoryP95Below70": task_memory["p95Maximum"] is not None
        and float(task_memory["p95Maximum"]) < 70,
        "hostCpuMemoryNetworkEvidencePresent": host_metric_evidence_present(host_metrics),
        "unplannedTaskRestartAndOomZero": len(stopped_tasks) == 0,
        "kinesisReadThrottleZero": read_throttle["sum"] == 0,
        "consumerRetryProtocolAndCheckpointErrorsZero": (
            logs["errorEvents"] == 0
            and terminal_failure["sum"] == 0
            and checkpoint_error["sum"] == 0
        ),
        "kclDetailedMetricsPresent": (
            kcl_metrics["series"] > 0
            and kcl_metrics["seriesWithDatapoints"] > 0
            and kcl_metrics["recordsProcessed"] >= EXPECTED_RECORDS
        ),
        "kclOperationSuccessZeroFailures": (
            kcl_metrics["successSeries"] > 0
            and kcl_metrics["successMinimum"] is not None
            and float(kcl_metrics["successMinimum"]) >= 1
        ),
        "kclWorkerIdentitiesAtMostThree": (
            1 <= len(kcl_metrics["workerIdentifiers"]) <= 3
        ),
        "terminalFailureObjectsZero": failure_objects == 0,
        "clickHouseInsertErrorsZero": int(clickhouse_logs["insert_errors"]) == 0,
        "asyncInsertEvidencePresent": (
            int(clickhouse_logs["async_log_entries"]) > 0
            and int(clickhouse_logs["async_rows"]) > 0
        ),
        "partsAndMergesNotPersistentlyGrowing": not parts_persistently_growing,
        "diskBelow80": float(parts_second["disk_used_percent"]) < 80,
    }
    result = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "runId": bundle.run_id,
        "sessionId": bundle.session_id,
        "identity": identity,
        "measurement": {
            "startEpoch": start_epoch,
            "endEpoch": end_epoch,
            "expectedRecords": EXPECTED_RECORDS,
            "completionTimedOut": completion_timed_out,
            "drainSeconds": round(drain_seconds, 3),
        },
        "producerAnalysis": producer_analysis,
        "completion": completion,
        "duplicates": {
            "physical": int(completion["events_physical"]) - int(completion["events_final"]),
            "logicalAfterFinal": int(completion["events_final"]) - int(completion["events_unique"]),
            "interpretation": (
                "Physical duplicates are expected only from KCL at-least-once redelivery; "
                "ReplacingMergeTree FINAL must converge to one row per event_id."
            ),
        },
        "service": service,
        "leases": leases,
        "stoppedTasksDuringMeasurementAndDrain": stopped_tasks,
        "consumerLogs": logs,
        "metrics": {
            "taskCpu": task_cpu,
            "taskMemory": task_memory,
            "hosts": host_metrics,
            "containerInsights": container_insights,
            "kcl": kcl_metrics,
            "readThrottle": read_throttle,
            "terminalFailure": terminal_failure,
            "checkpointError": checkpoint_error,
        },
        "clickHouse": {
            "logs": clickhouse_logs,
            "before": full_load.get("readiness", {}).get("clickHouseBefore"),
            "afterFirst": parts_first,
            "afterSecond": parts_second,
            "partsPersistentlyGrowing": parts_persistently_growing,
        },
        "failureObjects": failure_objects,
        "checks": checks,
        "pass": all(checks.values()),
    }
    write_private(args.run_dir / "full-load-evaluation-ecs.json", result)
    print(json.dumps({"checks": checks, "pass": result["pass"]}, indent=2))
    return 0 if result["pass"] else 2


def remaining_validation_seconds(run_document: dict[str, Any], now: datetime) -> int:
    started = parse_utc(str(run_document["paidWallClockStartedAt"]))
    deadline = started + timedelta(minutes=CLEANUP_START_MINUTES)
    return math.floor((deadline - now.astimezone(UTC)).total_seconds())


def collect_original_producer_analysis(
    run_dir: Path,
    stream_name: str,
    manifest: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    final_cloudwatch = run_dir / "producer-kinesis-cloudwatch.json"
    final_analysis = run_dir / "producer-analysis.json"
    if final_cloudwatch.exists() or final_analysis.exists():
        raise FileExistsError("producer analysis evidence already exists")
    start_epoch = int(manifest["measurementStartEpoch"])
    end_epoch = int(manifest["measurementEndEpoch"])
    start = datetime.fromtimestamp(start_epoch, UTC).isoformat().replace("+00:00", "Z")
    end = datetime.fromtimestamp(end_epoch, UTC).isoformat().replace("+00:00", "Z")
    stage_dir = run_dir / "producer-full-load"
    deadline = time.monotonic() + timeout_seconds
    with tempfile.TemporaryDirectory(prefix="phase4-producer-analysis-") as temporary:
        temporary_dir = Path(temporary)
        cloudwatch = temporary_dir / "cloudwatch.json"
        analysis = temporary_dir / "analysis.json"
        while True:
            subprocess.run([
                "node", str(COLLECTOR),
                "--stream-name", stream_name,
                "--start", start,
                "--end", end,
                "--output", str(cloudwatch),
            ], check=True)
            subprocess.run([
                sys.executable, str(ANALYZER),
                "--stage-dir", str(stage_dir),
                "--stage", "50k_final",
                "--target-rps", str(TARGET_RPS),
                "--measurement-start-epoch", str(start_epoch),
                "--measurement-end-epoch", str(end_epoch),
                "--network-gbps", str(PRODUCER_NETWORK_GBPS),
                "--expected-workers", str(EXPECTED_WORKERS),
                "--cloudwatch-json", str(cloudwatch),
                "--output", str(analysis),
            ], check=False)
            document = json.loads(analysis.read_text(encoding="utf-8"))
            complete = document.get("checks", {}).get(
                "cloudWatchRequiredMetricsCoverMeasurementWindow"
            ) is True
            if complete or time.monotonic() >= deadline:
                shutil.copyfile(cloudwatch, final_cloudwatch)
                shutil.copyfile(analysis, final_analysis)
                return document
            time.sleep(15)


def completion_query(run_id: str, start_epoch: int, end_epoch: int) -> str:
    if not run_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("invalid run id")
    return f"""
SELECT
    (SELECT count() FROM loopad.events FINAL
      WHERE run_id = '{run_id}'
        AND producer_sent_at >= toDateTime({start_epoch}, 'UTC')
        AND producer_sent_at < toDateTime({end_epoch}, 'UTC')) AS events_final,
    (SELECT uniqExact(event_id) FROM loopad.events FINAL
      WHERE run_id = '{run_id}'
        AND producer_sent_at >= toDateTime({start_epoch}, 'UTC')
        AND producer_sent_at < toDateTime({end_epoch}, 'UTC')) AS events_unique,
    (SELECT count() FROM loopad.events
      WHERE run_id = '{run_id}'
        AND producer_sent_at >= toDateTime({start_epoch}, 'UTC')
        AND producer_sent_at < toDateTime({end_epoch}, 'UTC')) AS events_physical,
    (SELECT count() FROM loopad.raw_events
      WHERE run_id = '{run_id}'
        AND lambda_received_at >= toDateTime({start_epoch}, 'UTC')
        AND lambda_received_at < toDateTime({end_epoch}, 'UTC')) AS raw_events
""".strip()


def completion_snapshot(
    aws: AwsRun,
    run_id: str,
    start_epoch: int,
    end_epoch: int,
) -> dict[str, Any]:
    counts = one_row(aws.clickhouse_rows(completion_query(run_id, start_epoch, end_epoch)))
    return {**counts, "iterator_age_ms": aws.iterator_age_latest(minutes=15)}


def completion_ready(snapshot: dict[str, Any]) -> bool:
    return (
        int(snapshot["events_final"]) == EXPECTED_RECORDS
        and int(snapshot["events_unique"]) == EXPECTED_RECORDS
        and int(snapshot["raw_events"]) == 0
        and snapshot.get("iterator_age_ms") is not None
        and float(snapshot["iterator_age_ms"]) <= ITERATOR_AGE_LIMIT_MS
    )


def service_dimensions(outputs: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"Name": "ClusterName", "Value": outputs["ConsumerClusterName"]},
        {"Name": "ServiceName", "Value": outputs["ConsumerServiceName"]},
    ]


def percentile_metric(
    aws: AwsRun,
    namespace: str,
    metric_name: str,
    dimensions: list[dict[str, str]],
    start_epoch: int,
    end_epoch: int,
) -> dict[str, Any]:
    response = aws.client("cloudwatch").get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=datetime.fromtimestamp(start_epoch, UTC),
        EndTime=datetime.fromtimestamp(end_epoch, UTC),
        Period=60,
        ExtendedStatistics=["p95"],
    )
    points = sorted(response.get("Datapoints", []), key=lambda value: value["Timestamp"])
    values = [float(point["ExtendedStatistics"]["p95"]) for point in points]
    return {
        "namespace": namespace,
        "metricName": metric_name,
        "dimensions": dimensions,
        "datapoints": len(values),
        "p95Values": values,
        "p95Maximum": max(values) if values else None,
    }


def sum_metric(
    aws: AwsRun,
    namespace: str,
    metric_name: str,
    dimensions: list[dict[str, str]],
    start_epoch: int,
    end_epoch: int,
) -> dict[str, Any]:
    response = aws.client("cloudwatch").get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=datetime.fromtimestamp(start_epoch, UTC),
        EndTime=datetime.fromtimestamp(end_epoch, UTC),
        Period=60,
        Statistics=["Sum"],
    )
    points = sorted(response.get("Datapoints", []), key=lambda value: value["Timestamp"])
    values = [float(point["Sum"]) for point in points]
    return {
        "namespace": namespace,
        "metricName": metric_name,
        "dimensions": dimensions,
        "datapoints": len(values),
        "values": values,
        "sum": sum(values),
    }


def metric_window_sum(
    aws: AwsRun,
    namespace: str,
    metric_name: str,
    dimensions: list[dict[str, str]],
    start_epoch: int,
    end: datetime,
) -> dict[str, Any]:
    return sum_metric(aws, namespace, metric_name, dimensions, start_epoch, int(end.timestamp()))


def collect_host_metrics(
    aws: AwsRun,
    service: dict[str, Any],
    start_epoch: int,
    end_epoch: int,
) -> list[dict[str, Any]]:
    task_hosts = sorted({
        (str(task["taskArn"]), str(task["ec2InstanceId"]))
        for task in service.get("tasks", [])
        if task.get("taskArn") and task.get("ec2InstanceId")
    })
    return [
        {
            "taskArn": task_arn,
            "instanceId": instance_id,
            "cpu": percentile_metric(
                aws,
                "AWS/EC2",
                "CPUUtilization",
                [{"Name": "InstanceId", "Value": instance_id}],
                start_epoch,
                end_epoch,
            ),
            "memory": percentile_metric(
                aws,
                "LoopAd/Phase4",
                "HostMemoryUtilization",
                [
                    {"Name": "RunId", "Value": aws.bundle.run_id},
                    {"Name": "TaskArn", "Value": task_arn},
                ],
                start_epoch,
                end_epoch,
            ),
            "networkIn": sum_metric(
                aws,
                "AWS/EC2",
                "NetworkIn",
                [{"Name": "InstanceId", "Value": instance_id}],
                start_epoch,
                end_epoch,
            ),
            "networkOut": sum_metric(
                aws,
                "AWS/EC2",
                "NetworkOut",
                [{"Name": "InstanceId", "Value": instance_id}],
                start_epoch,
                end_epoch,
            ),
        }
        for task_arn, instance_id in task_hosts
    ]


def host_metric_evidence_present(metrics: list[dict[str, Any]]) -> bool:
    return len(metrics) == 2 and all(
        item["cpu"]["datapoints"] > 0
        and item["memory"]["datapoints"] > 0
        and item["networkIn"]["datapoints"] > 0
        and item["networkOut"]["datapoints"] > 0
        for item in metrics
    )


def collect_container_insights(
    aws: AwsRun,
    cluster_name: str,
    start_epoch: int,
    end_epoch: int,
) -> dict[str, Any]:
    dimensions = [{"Name": "ClusterName", "Value": cluster_name}]
    return {
        metric_name: sum_metric(
            aws,
            "ECS/ContainerInsights",
            metric_name,
            dimensions,
            start_epoch,
            end_epoch,
        )
        for metric_name in ["MemoryUtilized", "MemoryReserved", "NetworkRxBytes", "NetworkTxBytes"]
    }


def collect_kcl_metrics(
    aws: AwsRun,
    start_epoch: int,
    end_epoch: int,
) -> dict[str, Any]:
    namespace = f"loopad-phase4-{aws.bundle.run_id}"
    cloudwatch = aws.client("cloudwatch")
    metrics = [
        metric
        for page in cloudwatch.get_paginator("list_metrics").paginate(Namespace=namespace)
        for metric in page.get("Metrics", [])
    ]
    unique: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        dimensions = sorted(
            [
                {"Name": str(item["Name"]), "Value": str(item["Value"])}
                for item in metric.get("Dimensions", [])
            ],
            key=lambda item: (item["Name"], item["Value"]),
        )
        key = json.dumps(
            {"name": metric["MetricName"], "dimensions": dimensions},
            sort_keys=True,
            separators=(",", ":"),
        )
        unique[key] = {"metricName": str(metric["MetricName"]), "dimensions": dimensions}
    catalog = [unique[key] for key in sorted(unique)]
    series: list[dict[str, Any]] = []
    for offset in range(0, len(catalog), 500):
        batch = catalog[offset:offset + 500]
        queries = [
            {
                "Id": f"m{index}",
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": item["metricName"],
                        "Dimensions": item["dimensions"],
                    },
                    "Period": 60,
                    "Stat": kcl_metric_stat(item["metricName"]),
                },
                "ReturnData": True,
            }
            for index, item in enumerate(batch)
        ]
        if not queries:
            continue
        results_by_id: dict[str, dict[str, Any]] = {
            query["Id"]: {"timestamps": [], "values": [], "statusCodes": [], "messages": []}
            for query in queries
        }
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "MetricDataQueries": queries,
                "StartTime": datetime.fromtimestamp(start_epoch, UTC),
                "EndTime": datetime.fromtimestamp(end_epoch, UTC),
                "ScanBy": "TimestampAscending",
            }
            if token:
                kwargs["NextToken"] = token
            response = cloudwatch.get_metric_data(**kwargs)
            for result in response.get("MetricDataResults", []):
                target = results_by_id[str(result["Id"])]
                target["timestamps"].extend(
                    value.astimezone(UTC).isoformat().replace("+00:00", "Z")
                    for value in result.get("Timestamps", [])
                )
                target["values"].extend(float(value) for value in result.get("Values", []))
                target["statusCodes"].append(str(result.get("StatusCode", "")))
                target["messages"].extend(result.get("Messages", []))
            token = response.get("NextToken")
            if not token:
                break
        for index, item in enumerate(batch):
            observed = results_by_id[f"m{index}"]
            values = observed["values"]
            series.append({
                **item,
                "stat": kcl_metric_stat(item["metricName"]),
                "datapoints": len(values),
                "timestamps": observed["timestamps"],
                "values": values,
                "sum": sum(values),
                "minimum": min(values) if values else None,
                "maximum": max(values) if values else None,
                "statusCodes": observed["statusCodes"],
                "messages": observed["messages"],
            })
    summary = summarize_kcl_metrics(series)
    worker_ids = worker_identifiers(series)
    return {
        "namespace": namespace,
        "startEpoch": start_epoch,
        "endEpoch": end_epoch,
        "series": len(series),
        "seriesWithDatapoints": sum(item["datapoints"] > 0 for item in series),
        "workerIdentifiers": worker_ids,
        **summary,
        "catalog": series,
    }


def kcl_metric_stat(metric_name: str) -> str:
    if metric_name == "Success":
        return "Minimum"
    if metric_name == "RecordsProcessed" or metric_name.endswith("Count"):
        return "Sum"
    if "Time" in metric_name or "Latency" in metric_name or "BehindLatest" in metric_name:
        return "Maximum"
    return "Sum"


def worker_identifiers(series: list[dict[str, Any]]) -> list[str]:
    return sorted({
        str(dimension["Value"])
        for item in series
        for dimension in item["dimensions"]
        if dimension["Name"] == "WorkerIdentifier"
    })


def summarize_kcl_metrics(series: list[dict[str, Any]]) -> dict[str, Any]:
    record_series = [
        item for item in series
        if item["metricName"] == "RecordsProcessed" and item["datapoints"] > 0
    ]
    maximum_dimensions = max(
        (len(item["dimensions"]) for item in record_series),
        default=0,
    )
    most_detailed_records = [
        item for item in record_series if len(item["dimensions"]) == maximum_dimensions
    ]
    success_series = [
        item for item in series
        if item["metricName"] == "Success" and item["datapoints"] > 0
    ]
    success_values = [
        float(item["minimum"])
        for item in success_series
        if item["minimum"] is not None
    ]
    lag_values = [
        float(item["maximum"])
        for item in series
        if item["metricName"] == "MillisBehindLatest"
        and item["datapoints"] > 0
        and item["maximum"] is not None
    ]
    return {
        "recordsProcessed": sum(float(item["sum"]) for item in most_detailed_records),
        "recordsProcessedSeries": len(most_detailed_records),
        "recordsProcessedDimensionCount": maximum_dimensions,
        "successSeries": len(success_series),
        "successMinimum": min(success_values) if success_values else None,
        "millisBehindLatestMaximum": max(lag_values) if lag_values else None,
    }


def stopped_task_snapshot(
    aws: AwsRun,
    start_epoch: int,
    end: datetime,
) -> list[dict[str, Any]]:
    ecs = aws.client("ecs")
    cluster = aws.bundle.outputs["ConsumerClusterName"]
    service = aws.bundle.outputs["ConsumerServiceName"]
    arns = [
        arn
        for page in ecs.get_paginator("list_tasks").paginate(
            cluster=cluster,
            serviceName=service,
            desiredStatus="STOPPED",
        )
        for arn in page.get("taskArns", [])
    ]
    documents: list[dict[str, Any]] = []
    for offset in range(0, len(arns), 100):
        for task in ecs.describe_tasks(cluster=cluster, tasks=arns[offset:offset + 100])["tasks"]:
            stopped_at = task.get("stoppedAt")
            if not isinstance(stopped_at, datetime):
                continue
            if not (start_epoch <= stopped_at.timestamp() <= end.timestamp()):
                continue
            documents.append({
                "taskArn": task["taskArn"],
                "stoppedAt": stopped_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "stopCode": task.get("stopCode"),
                "stoppedReason": task.get("stoppedReason"),
                "containers": [
                    {
                        "name": container.get("name"),
                        "exitCode": container.get("exitCode"),
                        "reason": container.get("reason"),
                    }
                    for container in task.get("containers", [])
                ],
            })
    return sorted(documents, key=lambda value: value["stoppedAt"])


def consumer_error_logs(
    aws: AwsRun,
    start_epoch: int,
    end: datetime,
) -> dict[str, Any]:
    patterns = [
        "phase4_batch_retry",
        "phase4_protocol_error",
        "phase4_protocol_socket_error",
        "phase4_checkpoint_error",
        "phase4_consumer_startup_error",
        "phase4_terminal_failure",
        "phase4_terminal_archive_error",
    ]
    client = aws.client("logs")
    log_group = f"/loop-ad/perf-phase4/{aws.bundle.run_id}/consumer"
    events: list[dict[str, Any]] = []
    for pattern in patterns:
        kwargs: dict[str, Any] = {
            "logGroupName": log_group,
            "startTime": start_epoch * 1_000,
            "endTime": int(end.timestamp() * 1_000),
            "filterPattern": f'"{pattern}"',
        }
        while True:
            response = client.filter_log_events(**kwargs)
            events.extend({
                "pattern": pattern,
                "timestamp": item.get("timestamp"),
                "logStreamName": item.get("logStreamName"),
                "eventId": item.get("eventId"),
            } for item in response.get("events", []))
            token = response.get("nextToken")
            if not token or token == kwargs.get("nextToken"):
                break
            kwargs["nextToken"] = token
    return {"logGroupName": log_group, "errorEvents": len(events), "events": events}


def flush_clickhouse_logs(aws: AwsRun) -> None:
    aws.run_ssm([
        "docker exec phase4-clickhouse clickhouse-client --query 'SYSTEM FLUSH LOGS'",
    ])


def clickhouse_log_query(start_epoch: int, end: datetime) -> str:
    end_epoch = int(end.timestamp())
    return f"""
SELECT
    (SELECT count() FROM system.asynchronous_insert_log
      WHERE event_time >= toDateTime({start_epoch}, 'UTC')
        AND event_time <= toDateTime({end_epoch}, 'UTC')
        AND database = 'loopad' AND table = 'events') AS async_log_entries,
    (SELECT coalesce(sum(rows), 0) FROM system.asynchronous_insert_log
      WHERE event_time >= toDateTime({start_epoch}, 'UTC')
        AND event_time <= toDateTime({end_epoch}, 'UTC')
        AND database = 'loopad' AND table = 'events') AS async_rows,
    (SELECT count() FROM system.query_log
      WHERE event_time >= toDateTime({start_epoch}, 'UTC')
        AND event_time <= toDateTime({end_epoch}, 'UTC')
        AND type IN ('ExceptionBeforeStart', 'ExceptionWhileProcessing')
        AND positionCaseInsensitive(query, 'INSERT INTO loopad') > 0) AS insert_errors
""".strip()


if __name__ == "__main__":
    raise SystemExit(main())

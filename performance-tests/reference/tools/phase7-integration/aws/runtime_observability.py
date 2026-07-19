#!/usr/bin/env python3
"""Run-scoped Phase 7 score telemetry and fail-closed observability collection."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from common import EXPECTED_OPERATOR_ARN, read_json, tag_map, write_json


EXPECTED_HOSTS = {"collector": 6, "consumer": 2, "clickHouse": 1}
CLOUDTRAIL_WAIT_SECONDS = 300
CLOUDTRAIL_POLL_SECONDS = 10
SSM_COMMAND_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
PROMETHEUS_LINE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+"
    r"(?P<value>(?:[-+0-9.eE]+|NaN|[+-]Inf))$"
)
LABEL = re.compile(r'(?:^|,)\s*([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')
HAPROXY_METRIC_FILTER = (
    r"^(haproxy_backend_agg_server_status|haproxy_backend_max_queue|"
    r"haproxy_frontend_http_responses_total)\{"
)


def start_score_capture(aws: Any, evidence_dir: Path) -> dict[str, Any]:
    raw_dir = evidence_dir
    raw_dir.mkdir(parents=True, exist_ok=False)
    instances = {
        "collector": aws.asg_instances("CollectorAutoScalingGroupName", EXPECTED_HOSTS["collector"]),
        "haproxy": aws.asg_instances("HaproxyAutoScalingGroupName", 2),
        "consumer": aws.asg_instances("ConsumerAutoScalingGroupName", EXPECTED_HOSTS["consumer"]),
        "clickHouse": aws.asg_instances("ClickHouseAutoScalingGroupName", EXPECTED_HOSTS["clickHouse"]),
    }
    context = {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "instances": instances,
        "servicesBefore": {
            role: aws.service_snapshot(role)
            for role in ("Collector", "Haproxy", "Consumer", "ClickHouse")
        },
        "collectorBefore": capture_collectors(aws, instances["collector"]),
        "haproxyBefore": capture_haproxy(aws, instances["haproxy"]),
        "telemetryUnits": {},
    }
    for role in EXPECTED_HOSTS:
        for instance_id in instances[role]:
            unit = telemetry_unit(aws.bundle.run_id, role, instance_id)
            aws.run_ssm(instance_id, [start_telemetry_command(unit)], timeout=60)
            context["telemetryUnits"][instance_id] = {"role": role, "unit": unit}
    write_json(raw_dir / "before.json", context)
    return context


def finish_score_capture(aws: Any, evidence_dir: Path, context: dict[str, Any]) -> dict[str, Any]:
    if context.get("runId") != aws.bundle.run_id or context.get("sessionId") != aws.bundle.session_id:
        raise RuntimeError("score telemetry context belongs to another run")
    telemetry: dict[str, dict[str, str]] = {role: {} for role in EXPECTED_HOSTS}
    for instance_id, item in context.get("telemetryUnits", {}).items():
        role = str(item["role"])
        telemetry[role][instance_id] = aws.run_ssm(
            instance_id, [stop_telemetry_command(str(item["unit"]))], timeout=90
        )
    result = {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "servicesBefore": context["servicesBefore"],
        "servicesAfter": {
            role: aws.service_snapshot(role)
            for role in ("Collector", "Haproxy", "Consumer", "ClickHouse")
        },
        "collectorBefore": context["collectorBefore"],
        "collectorAfter": capture_collectors(aws, context["instances"]["collector"]),
        "haproxyBefore": context["haproxyBefore"],
        "haproxyAfter": capture_haproxy(aws, context["instances"]["haproxy"]),
        "hostTelemetry": telemetry,
    }
    write_json(evidence_dir / "after.json", result)
    return result


def start_telemetry_command(unit: str) -> str:
    script_path = f"/run/{unit}.sh"
    data_path = f"/var/lib/loopad-phase7/{unit}.tsv"
    script = f"""#!/bin/bash
set -euo pipefail
while true; do
  read -r _ user nice system idle iowait irq softirq steal _ < /proc/stat
  read -r mem_total mem_available < <(awk '/^MemTotal:/ {{t=$2}} /^MemAvailable:/ {{a=$2}} END {{print t,a}}' /proc/meminfo)
  read -r fs_blocks fs_used < <(df -Pk /var/lib/docker | awk 'NR==2 {{print $2,$3}}')
  printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$(date +%s)" "$user" "$nice" "$system" "$idle" "$iowait" "$irq" "$softirq" "$steal" "$mem_total" "$mem_available" "$fs_blocks" "$fs_used"
  sleep 5
done >> {data_path}
"""
    encoded = base64.b64encode(script.encode()).decode()
    return (
        "set -euo pipefail; install -d -m 0700 /var/lib/loopad-phase7; "
        f"test ! -e '{data_path}'; printf '%s' '{encoded}' | base64 -d > '{script_path}'; "
        f"chmod 0700 '{script_path}'; systemd-run --quiet --collect --unit='{unit}' '{script_path}'"
    )


def stop_telemetry_command(unit: str) -> str:
    return (
        "set -euo pipefail; "
        f"systemctl stop '{unit}.service' >/dev/null 2>&1 || true; "
        f"test -s '/var/lib/loopad-phase7/{unit}.tsv'; cat '/var/lib/loopad-phase7/{unit}.tsv'"
    )


def telemetry_unit(run_id: str, role: str, instance_id: str) -> str:
    stamp = "".join(character for character in run_id if character.isdigit())[-14:]
    safe_role = re.sub(r"[^a-z0-9]", "", role.lower())
    suffix = instance_id.removeprefix("i-")[-8:]
    value = f"loopad-p7-{stamp}-{safe_role}-{suffix}"
    if len(value) > 63 or re.fullmatch(r"[a-z0-9-]+", value) is None:
        raise ValueError("invalid score telemetry systemd unit")
    return value


def capture_collectors(aws: Any, instance_ids: list[str]) -> dict[str, Any]:
    command = (
        "set -euo pipefail; container=$(docker ps --filter "
        "label=com.amazonaws.ecs.container-name=collector --format '{{.ID}}' | head -1); "
        "test -n \"$container\"; docker exec \"$container\" wget -qO- http://127.0.0.1:8080/debug/vars"
    )
    result = {}
    for instance_id in instance_ids:
        document = json.loads(aws.run_ssm(instance_id, [command], timeout=60))
        if not isinstance(document, dict):
            raise RuntimeError("collector debug vars must be a JSON object")
        result[instance_id] = document
    return result


def capture_haproxy(aws: Any, instance_ids: list[str]) -> dict[str, Any]:
    command = (
        "set -euo pipefail; container=$(docker ps --filter "
        "label=com.amazonaws.ecs.container-name=haproxy --format '{{.ID}}' | head -1); "
        "test -n \"$container\"; docker exec \"$container\" sha256sum "
        "/usr/local/etc/haproxy/haproxy.cfg | awk '{print $1}'; echo __METRICS__; "
        "docker exec \"$container\" wget -qO- http://127.0.0.1:8404/metrics "
        f"| grep -E '{HAPROXY_METRIC_FILTER}'"
    )
    result = {}
    for instance_id in instance_ids:
        output = aws.run_ssm(instance_id, [command], timeout=60)
        config_sha, separator, metrics = output.partition("\n__METRICS__\n")
        if separator == "" or re.fullmatch(r"[0-9a-f]{64}", config_sha.strip()) is None:
            raise RuntimeError("HAProxy runtime config fingerprint is invalid")
        parse_prometheus(metrics)
        result[instance_id] = {"configSha256": config_sha.strip(), "metrics": metrics}
    return result


def collect_observability(aws: Any) -> dict[str, Any]:
    score_dir = aws.bundle.run_dir / "evidence" / "score"
    raw_path = aws.bundle.run_dir / "evidence" / "score-observability" / "after.json"
    raw = read_json(raw_path)
    if raw.get("runId") != aws.bundle.run_id or raw.get("sessionId") != aws.bundle.session_id:
        raise RuntimeError("score observability artifact belongs to another run")
    score = read_json(score_dir / "stage-summary.json")
    drain = read_json(aws.bundle.run_dir / "drain-accounting.json")
    resources = summarize_resources(raw["hostTelemetry"])
    haproxy = summarize_haproxy(
        raw["haproxyBefore"], raw["haproxyAfter"],
        aws.bundle.outputs["HaproxyConfigSha256"],
    )
    collector = summarize_collectors(raw["collectorBefore"], raw["collectorAfter"])
    if collector["successes"] != int(drain["counts"]["http202"]):
        raise RuntimeError("collector final ACK delta does not equal score HTTP 202")
    drain["counts"]["collectorFinalAck"] = collector["successes"]
    drain["counts"]["collectorDebugDelta"] = collector
    write_json(aws.bundle.run_dir / "drain-accounting.json", drain)

    score_start = int(score["aggregate"]["startEpoch"])
    score_end = max(
        int(datetime.fromisoformat(str(node["endedAt"]).replace("Z", "+00:00")).timestamp())
        for node in score["aggregate"]["nodes"]
    )
    failures, failure_evidence = collect_failures(
        aws, raw, drain, score_start, score_end, collector
    )
    raw["failureEvidence"] = failure_evidence
    write_json(raw_path, raw)
    cloudtrail = collect_cloudtrail(aws, score)
    result = {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "resources": resources,
        "haproxy": haproxy,
        "failures": failures,
        "cloudTrail": cloudtrail,
        "rawEvidence": {
            "path": raw_path.relative_to(aws.bundle.run_dir).as_posix(),
            "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        },
    }
    write_json(aws.bundle.run_dir / "observability-summary.json", result)
    write_cost_status(aws)
    return result


def summarize_resources(telemetry: dict[str, dict[str, str]]) -> dict[str, Any]:
    result = {}
    for role, expected in EXPECTED_HOSTS.items():
        hosts = telemetry.get(role)
        if not isinstance(hosts, dict) or len(hosts) != expected:
            raise RuntimeError(f"host telemetry count mismatch for {role}")
        per_host = {}
        for instance_id, text in hosts.items():
            parsed = parse_host_telemetry(text)
            if parsed["sampleCount"] < 50:
                raise RuntimeError(f"score host telemetry is incomplete for {instance_id}")
            per_host[instance_id] = {
                "sampleCount": parsed["sampleCount"],
                "cpuP95Percent": percentile(parsed["cpuPercent"], 0.95),
                "memoryP95Percent": percentile(parsed["memoryPercent"], 0.95),
                "filesystemPeakPercent": max(parsed["filesystemPercent"]),
            }
        role_result = {
            # A role passes only if every individual host p95 passes. Pooling
            # samples could otherwise dilute a short hot period on one host.
            "cpuP95Percent": max(item["cpuP95Percent"] for item in per_host.values()),
            "memoryP95Percent": max(item["memoryP95Percent"] for item in per_host.values()),
            "sampleCount": sum(item["sampleCount"] for item in per_host.values()),
            "hosts": per_host,
        }
        if role == "clickHouse":
            role_result["filesystemPeakPercent"] = max(
                item["filesystemPeakPercent"] for item in per_host.values()
            )
        result[role] = role_result
    return result


def parse_host_telemetry(text: str) -> dict[str, Any]:
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 13:
            raise RuntimeError("host telemetry row has an invalid field count")
        values = [int(value) for value in fields]
        if any(value < 0 for value in values):
            raise RuntimeError("host telemetry values must be nonnegative")
        rows.append(values)
    if len(rows) < 2:
        raise RuntimeError("host telemetry needs at least two samples")
    cpu = []
    for before, after in zip(rows, rows[1:]):
        before_total = sum(before[1:9])
        after_total = sum(after[1:9])
        total_delta = after_total - before_total
        idle_delta = (after[4] + after[5]) - (before[4] + before[5])
        if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
            raise RuntimeError("host CPU telemetry counters are not monotonic")
        cpu.append((total_delta - idle_delta) * 100.0 / total_delta)
    memory = [
        (row[9] - row[10]) * 100.0 / row[9]
        for row in rows if row[9] > 0 and 0 <= row[10] <= row[9]
    ]
    filesystem = [row[12] * 100.0 / row[11] for row in rows if row[11] > 0 and row[12] <= row[11]]
    if len(memory) != len(rows) or len(filesystem) != len(rows):
        raise RuntimeError("host memory or filesystem telemetry is invalid")
    return {
        "sampleCount": len(rows),
        "cpuPercent": cpu,
        "memoryPercent": memory,
        "filesystemPercent": filesystem,
    }


def summarize_collectors(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    if set(before) != set(after) or len(before) != EXPECTED_HOSTS["collector"]:
        raise RuntimeError("collector debug snapshot host sets differ")
    fields = ("successes", "failures", "retries", "partial_failures", "timeouts")
    totals = {field: 0 for field in fields}
    for instance_id in before:
        old = before[instance_id].get("kinesis", {}).get("put_records", {})
        new = after[instance_id].get("kinesis", {}).get("put_records", {})
        for field in fields:
            delta = int(new.get(field, -1)) - int(old.get(field, -1))
            if delta < 0:
                raise RuntimeError(f"collector counter reset during score: {instance_id}/{field}")
            totals[field] += delta
    return totals


def parse_prometheus(text: str) -> list[dict[str, Any]]:
    values = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PROMETHEUS_LINE.fullmatch(line)
        if not match:
            raise RuntimeError(f"invalid Prometheus sample: {line[:80]}")
        labels = {item.group(1): bytes(item.group(2), "utf-8").decode("unicode_escape") for item in LABEL.finditer(match.group("labels") or "")}
        value = float(match.group("value"))
        values.append({"name": match.group("name"), "labels": labels, "value": value})
    if not values:
        raise RuntimeError("Prometheus document is empty")
    return values


def summarize_haproxy(before: dict[str, Any], after: dict[str, Any], expected_sha: str) -> dict[str, Any]:
    if set(before) != set(after) or len(before) != 2:
        raise RuntimeError("HAProxy snapshot host sets differ")
    max_queue = 0.0
    http4xx = 0
    http5xx = 0
    active_counts = []
    for instance_id in before:
        if before[instance_id]["configSha256"] != expected_sha or after[instance_id]["configSha256"] != expected_sha:
            raise RuntimeError("HAProxy runtime config differs from synthesized output")
        old = parse_prometheus(before[instance_id]["metrics"])
        new = parse_prometheus(after[instance_id]["metrics"])
        active_counts.append(int(metric_value(new, "haproxy_backend_agg_server_status", proxy="collectors", state="UP")))
        max_queue = max(max_queue, metric_value(new, "haproxy_backend_max_queue", proxy="collectors"))
        for code in ("4xx", "5xx"):
            delta = metric_value(new, "haproxy_frontend_http_responses_total", proxy="ingest", code=code) - metric_value(old, "haproxy_frontend_http_responses_total", proxy="ingest", code=code)
            if delta < 0 or not delta.is_integer():
                raise RuntimeError("HAProxy response counter reset during score")
            if code == "4xx":
                http4xx += int(delta)
            else:
                http5xx += int(delta)
    if active_counts != [6, 6]:
        raise RuntimeError("each HAProxy must expose six active collector backends")
    return {
        "configSha256": expected_sha,
        "activeBackends": 6,
        "activeBackendsPerProxy": active_counts,
        "maxQueue": max_queue,
        "http4xx": http4xx,
        "http5xx": http5xx,
        "prometheusCollected": True,
    }


def metric_value(samples: list[dict[str, Any]], name: str, **labels: str) -> float:
    matches = [
        sample["value"] for sample in samples
        if sample["name"] == name and all(sample["labels"].get(key) == value for key, value in labels.items())
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one Prometheus sample for {name}/{labels}")
    value = float(matches[0])
    if not math.isfinite(value):
        raise RuntimeError(f"Prometheus sample must be finite for {name}/{labels}")
    return value


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise RuntimeError("percentile input is empty")
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return round(ordered[index], 6)


def collect_failures(
    aws: Any,
    raw: dict[str, Any],
    drain: dict[str, Any],
    start: int,
    end: int,
    collector: dict[str, int],
) -> tuple[dict[str, int], dict[str, Any]]:
    kinesis_metric = metric_sum_evidence(
        aws,
        "AWS/Kinesis",
        "WriteProvisionedThroughputExceeded",
        [{"Name": "StreamName", "Value": aws.bundle.outputs["StreamName"]}],
        minutes=60,
    )
    kinesis_throttle = int(round(float(kinesis_metric["sum"])))

    failure_bucket = aws.bundle.outputs["FailureBucketName"]
    failure_prefix = f"failures/{aws.bundle.run_id}/"
    failure_keys = sorted(
        str(item["Key"])
        for page in aws.client("s3").get_paginator("list_objects_v2").paginate(
            Bucket=failure_bucket,
            Prefix=failure_prefix,
        )
        for item in page.get("Contents", [])
        if isinstance(item, dict) and isinstance(item.get("Key"), str)
    )
    log_evidence = logs_count_evidence(
        aws, start, end + 600,
        "filter event = 'phase4_batch_retry' | stats count(*) as count",
    )
    clickhouse_errors = int(log_evidence["count"])
    before_tasks = {
        task["taskArn"] for role in raw["servicesBefore"].values() for task in role["tasks"]
    }
    after_tasks = {
        task["taskArn"] for role in raw["servicesAfter"].values() for task in role["tasks"]
    }
    unexpected_restarts = len(before_tasks.symmetric_difference(after_tasks))
    stopped_tasks = stopped_task_evidence(aws, start, end + 600)
    oom_kills = int(stopped_tasks["oomCount"])
    archive = drain.get("archive", {})
    archive_status = archive.get("workerResult", {}).get("status")
    archive_failures = 0 if archive_status == "passed" else 1
    kcl_terminal_failure = int(
        round(float(drain.get("failures", {}).get("terminalFailure", -1)))
    )
    summary = {
        "kinesisThrottle": kinesis_throttle,
        "collectorFinalFailure": collector["failures"] + collector["partial_failures"] + collector["timeouts"],
        "kclTerminalFailure": kcl_terminal_failure,
        "failureObjects": len(failure_keys),
        "clickHouseInsertErrors": clickhouse_errors,
        "archiveFailures": archive_failures,
        "unexpectedRestarts": unexpected_restarts,
        "oomKills": oom_kills,
    }
    evidence = {
        "schemaVersion": 1,
        "kinesisThrottleMetric": kinesis_metric,
        "failureObjects": {
            "bucket": failure_bucket,
            "prefix": failure_prefix,
            "keys": failure_keys,
        },
        "clickHouseInsertErrorQuery": log_evidence,
        "stoppedTaskQuery": stopped_tasks,
        "collectorDelta": dict(collector),
        "serviceTaskArnsBefore": sorted(before_tasks),
        "serviceTaskArnsAfter": sorted(after_tasks),
        "kclTerminalFailure": kcl_terminal_failure,
        "archiveWorkerStatus": archive_status,
    }
    return summary, evidence


def metric_sum_evidence(
    aws: Any,
    namespace: str,
    metric: str,
    dimensions: list[dict[str, str]],
    *,
    minutes: int,
) -> dict[str, Any]:
    end = datetime.now(UTC)
    start = end - timedelta(minutes=minutes)
    response = aws.client("cloudwatch").get_metric_statistics(
        Namespace=namespace,
        MetricName=metric,
        Dimensions=dimensions,
        StartTime=start,
        EndTime=end,
        Period=60,
        Statistics=["Sum"],
    )
    datapoints = [
        {
            "timestamp": point["Timestamp"].astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "sum": float(point.get("Sum", 0)),
        }
        for point in sorted(response.get("Datapoints", []), key=lambda item: item["Timestamp"])
        if isinstance(point, dict) and isinstance(point.get("Timestamp"), datetime)
    ]
    return {
        "namespace": namespace,
        "metricName": metric,
        "dimensions": dimensions,
        "startTime": start.isoformat().replace("+00:00", "Z"),
        "endTime": end.isoformat().replace("+00:00", "Z"),
        "periodSeconds": 60,
        "statistic": "Sum",
        "datapoints": datapoints,
        "sum": sum(float(point["sum"]) for point in datapoints),
    }


def logs_count_evidence(aws: Any, start: int, end: int, query: str) -> dict[str, Any]:
    client = aws.client("logs")
    response = client.start_query(
        logGroupName=f"/loopad/perf/phase7/{aws.bundle.run_id}/ConsumerLogs",
        startTime=start, endTime=end, queryString=query, limit=10,
    )
    query_id = response["queryId"]
    deadline = __import__("time").monotonic() + 300
    while __import__("time").monotonic() < deadline:
        result = client.get_query_results(queryId=query_id)
        if result.get("status") == "Complete":
            rows = result.get("results", [])
            if len(rows) != 1:
                raise RuntimeError("CloudWatch Logs count query returned an invalid result")
            fields = {item["field"]: item["value"] for item in rows[0]}
            count = int(float(fields.get("count", "0")))
            return {
                "logGroup": f"/loopad/perf/phase7/{aws.bundle.run_id}/ConsumerLogs",
                "startEpoch": start,
                "endEpoch": end,
                "query": query,
                "queryId": query_id,
                "status": "Complete",
                "results": rows,
                "count": count,
            }
        if result.get("status") in {"Failed", "Cancelled", "Timeout", "Unknown"}:
            break
        __import__("time").sleep(3)
    raise RuntimeError("CloudWatch Logs count query did not complete")


def logs_count(aws: Any, start: int, end: int, query: str) -> int:
    return int(logs_count_evidence(aws, start, end, query)["count"])


def stopped_task_evidence(aws: Any, start: int, end: int) -> dict[str, Any]:
    ecs = aws.client("ecs")
    count = 0
    tasks = []
    for role in ("Collector", "Haproxy", "Consumer", "ClickHouse"):
        cluster = aws.bundle.outputs[f"{role}ClusterName"]
        service = aws.bundle.outputs[f"{role}ServiceName"]
        arns = [
            arn for page in ecs.get_paginator("list_tasks").paginate(
                cluster=cluster, serviceName=service, desiredStatus="STOPPED"
            ) for arn in page.get("taskArns", [])
        ]
        for offset in range(0, len(arns), 100):
            for task in ecs.describe_tasks(cluster=cluster, tasks=arns[offset:offset + 100]).get("tasks", []):
                stopped = task.get("stoppedAt")
                if not isinstance(stopped, datetime) or not start <= int(stopped.timestamp()) <= end:
                    continue
                text = " ".join(str(value) for value in (
                    task.get("stoppedReason"), *(container.get("reason") for container in task.get("containers", []))
                ))
                is_oom = re.search(r"out.?of.?memory|oom", text, re.IGNORECASE) is not None
                tasks.append({
                    "taskArn": str(task.get("taskArn", "")),
                    "stoppedAt": stopped.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "stoppedReason": str(task.get("stoppedReason", "")),
                    "containerReasons": [
                        str(container.get("reason", ""))
                        for container in task.get("containers", [])
                        if isinstance(container, dict)
                    ],
                    "oom": is_oom,
                })
                if is_oom:
                    count += 1
    return {
        "startEpoch": start,
        "endEpoch": end,
        "tasks": sorted(tasks, key=lambda item: (item["stoppedAt"], item["taskArn"])),
        "oomCount": count,
    }


def stopped_oom_count(aws: Any, start: int, end: int) -> int:
    return int(stopped_task_evidence(aws, start, end)["oomCount"])


def collect_cloudtrail(aws: Any, score: dict[str, Any]) -> dict[str, Any]:
    run = read_json(aws.bundle.run_dir / "run.json")
    attempts = run.get("stageAttempts", [])
    for stage in ("deploy", "warmup", "score_archive"):
        if sum(1 for item in attempts if item.get("stage") == stage) != 1:
            raise RuntimeError(f"runner does not prove one {stage} attempt")
    command_expectations = load_ssm_command_expectations(aws.bundle.run_dir, aws.bundle.run_id)
    archive = score.get("archive", {})
    started_by = str(archive.get("startedBy", ""))
    if not started_by:
        raise RuntimeError("archive stage summary is missing exact startedBy")

    client = aws.client("cloudtrail")
    paid_started = datetime.fromisoformat(str(run["paidStartedAt"]).replace("Z", "+00:00"))
    event_start = paid_started - timedelta(minutes=5)
    event_end = datetime.now(UTC)
    deadline = time.monotonic() + CLOUDTRAIL_WAIT_SECONDS
    documents = None
    while documents is None:
        documents = classify_execution_events(
            deploy_events=lookup_events(client, "CreateChangeSet", event_start, event_end),
            send_events=lookup_events(client, "SendCommand", event_start, event_end),
            archive_events=lookup_events(client, "RunTask", event_start, event_end),
            run_id=aws.bundle.run_id,
            session_id=aws.bundle.session_id,
            command_expectations=command_expectations,
            started_by=started_by,
            task_definition=str(aws.bundle.outputs["ArchiveTaskDefinitionArn"]),
            cluster=str(aws.bundle.outputs["ArchiveClusterName"]),
        )
        if documents is not None:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("CloudTrail execution evidence did not become complete before the bounded wait")
        time.sleep(CLOUDTRAIL_POLL_SECONDS)

    raw_dir = aws.bundle.run_dir / "evidence" / "cloudtrail"
    raw_dir.mkdir(parents=True, exist_ok=False)
    paths = []
    digests = []
    for name, events in documents.items():
        path = raw_dir / f"{name}.json"
        write_json(path, {
            "schemaVersion": 1, "runId": aws.bundle.run_id,
            "sessionId": aws.bundle.session_id, "events": events,
        })
        paths.append(path.relative_to(aws.bundle.run_dir).as_posix())
        digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return {
        "collected": True,
        "deployAttempts": 1,
        "warmupAttempts": 1,
        "scoreAttempts": 1,
        "archiveAttempts": 1,
        "sourcePaths": paths,
        "sha256": digests,
    }


def load_ssm_command_expectations(run_dir: Path, run_id: str) -> dict[str, dict[str, dict[str, Any]]]:
    stamp = re.fullmatch(r"run_([0-9]{8}_[0-9]{6})_phase7_integration", run_id)
    if stamp is None:
        raise RuntimeError("invalid Phase 7 run ID while loading SSM evidence")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for stage, duration in (("warmup", 180), ("score", 300)):
        worker_run_id = f"run_{stamp.group(1)}_phase7_{stage}"
        expected_specs = (
            (
                "ssm-transfer-probe-started.json",
                "phase7-ssm-transfer-probe",
                f"loop-ad {worker_run_id} 20KiB SSM transfer probe",
            ),
            (
                "ssm-command-started.json",
                "phase7-oha-load-command",
                f"loop-ad {worker_run_id} oha 6250rps {duration}s",
            ),
        )
        by_id: dict[str, dict[str, Any]] = {}
        for file_name, kind, comment in expected_specs:
            paths = sorted((run_dir / "evidence" / stage).glob(f"node-*/{file_name}"))
            if len(paths) != 8:
                raise RuntimeError(f"expected eight immutable {kind} command IDs for {stage}")
            for path in paths:
                value = read_json(path)
                command_id = str(value.get("commandId", ""))
                if (
                    SSM_COMMAND_ID.fullmatch(command_id) is None
                    or value.get("kind") != kind
                    or value.get("runId") != worker_run_id
                    or value.get("stageLabel") != stage
                    or value.get("comment") != comment
                    or re.fullmatch(r"node-[0-9]{2}", str(value.get("nodeId", ""))) is None
                    or re.fullmatch(r"i-[0-9a-f]+", str(value.get("instanceId", ""))) is None
                    or command_id in by_id
                ):
                    raise RuntimeError(f"invalid or duplicate immutable SSM command evidence: {path}")
                by_id[command_id] = {
                    "kind": kind,
                    "comment": comment,
                    "instanceId": str(value["instanceId"]),
                }
        if len(by_id) != 16:
            raise RuntimeError(f"expected sixteen unique SSM commands for {stage}")
        result[stage] = by_id
    return result


def classify_execution_events(
    *,
    deploy_events: list[dict[str, Any]],
    send_events: list[dict[str, Any]],
    archive_events: list[dict[str, Any]],
    run_id: str,
    session_id: str,
    command_expectations: dict[str, dict[str, dict[str, Any]]],
    started_by: str,
    task_definition: str,
    cluster: str,
) -> dict[str, list[dict[str, Any]]] | None:
    deploy = [
        event for event in deploy_events
        if event.get("request", {}).get("stackName") == "LoopAdPerfPhase7IntegrationStack"
        and event.get("request", {}).get("tags", {}).get("RunId") == run_id
        and event.get("request", {}).get("tags", {}).get("SessionId") == session_id
    ]
    archive = [
        event for event in archive_events
        if event.get("request", {}).get("startedBy") == started_by
        and event.get("request", {}).get("taskDefinition") == task_definition
        and event.get("request", {}).get("cluster") == cluster
    ]
    if len(deploy) > 1 or len(archive) > 1:
        raise RuntimeError("CloudTrail proves more than one deploy or archive API attempt")

    matched_commands: dict[str, list[dict[str, Any]]] = {}
    run_stamp = re.fullmatch(r"run_([0-9]{8}_[0-9]{6})_phase7_integration", run_id)
    if run_stamp is None:
        raise RuntimeError("invalid Phase 7 run ID in CloudTrail classifier")
    for stage, expected_by_id in command_expectations.items():
        worker_run_id = f"run_{run_stamp.group(1)}_phase7_{stage}"
        same_run_events = [
            event for event in send_events
            if worker_run_id in str(event.get("request", {}).get("comment", ""))
        ]
        if len(same_run_events) > len(expected_by_id):
            raise RuntimeError(f"CloudTrail proves duplicate {stage} SendCommand attempts")
        for event in same_run_events:
            command_id = event.get("response", {}).get("commandId")
            expected = expected_by_id.get(str(command_id))
            request = event.get("request", {})
            if (
                expected is None
                or request.get("comment") != expected["comment"]
                or request.get("documentName") != "AWS-RunShellScript"
                or request.get("instanceIds") != [expected["instanceId"]]
            ):
                raise RuntimeError(f"CloudTrail contains an unowned or malformed {stage} SendCommand")
        if len(same_run_events) == len(expected_by_id) and {
            str(event.get("response", {}).get("commandId")) for event in same_run_events
        } != set(expected_by_id):
            raise RuntimeError(f"CloudTrail {stage} command IDs differ from immutable local evidence")
        matched_commands[stage] = sorted(
            same_run_events,
            key=lambda item: str(item.get("response", {}).get("commandId")),
        )

    if len(deploy) != 1 or len(archive) != 1 or any(
        len(matched_commands.get(stage, [])) != len(expected)
        for stage, expected in command_expectations.items()
    ):
        return None
    matched_events = [*deploy, *matched_commands["warmup"], *matched_commands["score"], *archive]
    require_approved_principals(matched_events)
    return {
        "deploy": deploy,
        "warmup": matched_commands["warmup"],
        "score": matched_commands["score"],
        "archive": archive,
    }


def lookup_events(
    client: Any,
    event_name: str,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> list[dict[str, Any]]:
    result = []
    lookup_request: dict[str, Any] = {
        "LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": event_name}],
    }
    if start_time is not None:
        lookup_request["StartTime"] = start_time
    if end_time is not None:
        lookup_request["EndTime"] = end_time
    for page in client.get_paginator("lookup_events").paginate(**lookup_request):
        for event in page.get("Events", []):
            raw = json.loads(event.get("CloudTrailEvent", "{}"))
            event_request = raw.get("requestParameters") or {}
            response = raw.get("responseElements") or {}
            result.append({
                "eventId": event.get("EventId"),
                "eventName": event.get("EventName"),
                "eventTime": event.get("EventTime").astimezone(UTC).isoformat().replace("+00:00", "Z") if isinstance(event.get("EventTime"), datetime) else None,
                "principalArn": raw.get("userIdentity", {}).get("arn"),
                "request": {
                    "stackName": event_request.get("stackName"),
                    "startedBy": event_request.get("startedBy"),
                    "taskDefinition": event_request.get("taskDefinition"),
                    "cluster": event_request.get("cluster"),
                    "comment": event_request.get("comment"),
                    "documentName": event_request.get("documentName"),
                    "instanceIds": event_request.get("instanceIds"),
                    "tags": tag_map(event_request.get("tags", []))
                    if isinstance(event_request.get("tags"), list) else {},
                },
                "response": {
                    "commandId": response.get("command", {}).get("commandId") if isinstance(response.get("command"), dict) else None,
                },
            })
    return result


def require_approved_principals(events: list[dict[str, Any]]) -> None:
    if not events or any(event.get("principalArn") != EXPECTED_OPERATOR_ARN for event in events):
        raise RuntimeError("CloudTrail execution principal differs from the approved operator")


def write_cost_status(aws: Any) -> dict[str, Any]:
    model = read_json(aws.bundle.run_dir / "inputs" / "cost-model.json")
    cost = {
        # Use the full approved 180-minute operational maximum as the
        # conservative accrued bound. The model's input accrued value is zero
        # at preflight and must not be reported as post-run cost evidence.
        "accruedUpperBoundUsd": model["operationalMaximumUsd"],
        "maximumIncludingCleanupUsd": model["maximumIncludingCleanupUsd"],
        "cleanupReserveUsd": model["cleanupReserveUsd"],
        "basis": "full approved 180-minute operational maximum at collection time",
    }
    result = {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "cost": cost,
        "sourceCostModelSha256": hashlib.sha256(
            (aws.bundle.run_dir / "inputs" / "cost-model.json").read_bytes()
        ).hexdigest(),
    }
    write_json(aws.bundle.run_dir / "cost-status.json", result)
    return result

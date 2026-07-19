from __future__ import annotations

from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


AWS_DIR = Path(__file__).resolve().parents[1] / "aws"
sys.path.insert(0, str(AWS_DIR))

from runtime_observability import (  # noqa: E402
    HAPROXY_METRIC_FILTER,
    capture_haproxy,
    classify_execution_events,
    collect_failures,
    lookup_events,
    metric_value,
    parse_host_telemetry,
    parse_prometheus,
    require_approved_principals,
    start_telemetry_command,
    stop_telemetry_command,
    summarize_collectors,
    summarize_haproxy,
    summarize_resources,
    telemetry_unit,
    write_cost_status,
)


class FakeCloudTrailPaginator:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.request: dict[str, object] | None = None

    def paginate(self, **request: object) -> list[dict[str, object]]:
        self.request = request
        return self.pages


class FakeCloudTrail:
    def __init__(self, paginator: FakeCloudTrailPaginator) -> None:
        self.paginator = paginator

    def get_paginator(self, operation: str) -> FakeCloudTrailPaginator:
        if operation != "lookup_events":
            raise AssertionError(operation)
        return self.paginator


def host_text(samples: int = 51) -> str:
    rows = []
    for index in range(samples):
        rows.append("\t".join(str(value) for value in (
            1_700_000_000 + index * 5,
            100 + index * 20, 0, 100 + index * 10,
            1_000 + index * 70, 0, 0, 0, 0,
            1_000_000, 400_000,
            2_000_000, 1_000_000,
        )))
    return "\n".join(rows) + "\n"


def prometheus(*, responses4: int, responses5: int, active: int = 6, queue: int = 0) -> str:
    return "\n".join((
        f'haproxy_backend_agg_server_status{{proxy="collectors",state="UP"}} {active}',
        f'haproxy_backend_max_queue{{proxy="collectors"}} {queue}',
        f'haproxy_frontend_http_responses_total{{proxy="ingest",code="4xx"}} {responses4}',
        f'haproxy_frontend_http_responses_total{{proxy="ingest",code="5xx"}} {responses5}',
        'haproxy_process_current_zlib_memory NaN',
    )) + "\n"


class RuntimeObservabilityTest(unittest.TestCase):
    def test_haproxy_capture_filters_required_metrics_before_ssm_output_limit(self) -> None:
        required = prometheus(responses4=1, responses5=2)
        full_document = "# unrelated HAProxy metric padding\n" * 1_000 + required
        self.assertGreater(full_document.index("haproxy_backend_agg_server_status"), 24_000)
        self.assertNotIn(
            "haproxy_backend_agg_server_status",
            full_document.encode()[:24_000].decode(),
        )
        selected = "\n".join(
            line for line in full_document.splitlines()
            if re.match(HAPROXY_METRIC_FILTER, line)
        ) + "\n"
        self.assertLess(len(selected.encode()), 24_000)

        class FakeAws:
            def __init__(self) -> None:
                self.commands: list[str] = []

            def run_ssm(self, instance_id, commands, timeout):
                self.commands.append(commands[0])
                return "a" * 64 + "\n__METRICS__\n" + selected

        aws = FakeAws()
        result = capture_haproxy(aws, ["i-one"])

        self.assertEqual(selected, result["i-one"]["metrics"])
        self.assertIn(f"grep -E '{HAPROXY_METRIC_FILTER}'", aws.commands[0])
        self.assertLess(aws.commands[0].index("wget -qO-"), aws.commands[0].index("| grep -E"))

    def test_failure_collection_preserves_recomputable_raw_query_sources(self) -> None:
        class Paginator:
            def paginate(self, **request):
                self.request = request
                return [{"Contents": [{"Key": "failures/run-id/item.json"}]}]

        paginator = Paginator()
        aws = SimpleNamespace(
            bundle=SimpleNamespace(
                run_id="run-id",
                outputs={
                    "StreamName": "stream",
                    "FailureBucketName": "bucket",
                },
            ),
            client=lambda name: SimpleNamespace(
                get_paginator=lambda operation: paginator
            ),
        )
        raw = {
            "servicesBefore": {"Collector": {"tasks": [{"taskArn": "task-a"}]}},
            "servicesAfter": {"Collector": {"tasks": [{"taskArn": "task-a"}]}},
        }
        drain = {
            "failures": {"terminalFailure": 0},
            "archive": {"workerResult": {"status": "passed"}},
        }
        collector = {
            "successes": 100,
            "failures": 0,
            "retries": 0,
            "partial_failures": 0,
            "timeouts": 0,
        }
        metric = {
            "namespace": "AWS/Kinesis",
            "metricName": "WriteProvisionedThroughputExceeded",
            "dimensions": [],
            "startTime": "2026-07-18T00:00:00Z",
            "endTime": "2026-07-18T01:00:00Z",
            "periodSeconds": 60,
            "statistic": "Sum",
            "datapoints": [{"timestamp": "2026-07-18T00:01:00Z", "sum": 0.0}],
            "sum": 0.0,
        }
        log = {
            "status": "Complete",
            "results": [[{"field": "count", "value": "0"}]],
            "count": 0,
        }
        stopped = {"tasks": [], "oomCount": 0}

        with (
            patch("runtime_observability.metric_sum_evidence", return_value=metric),
            patch("runtime_observability.logs_count_evidence", return_value=log),
            patch("runtime_observability.stopped_task_evidence", return_value=stopped),
        ):
            summary, evidence = collect_failures(
                aws, raw, drain, 1, 2, collector
            )

        self.assertEqual(1, summary["failureObjects"])
        self.assertEqual(metric, evidence["kinesisThrottleMetric"])
        self.assertEqual(log, evidence["clickHouseInsertErrorQuery"])
        self.assertEqual(stopped, evidence["stoppedTaskQuery"])
        self.assertEqual(["task-a"], evidence["serviceTaskArnsBefore"])

    def test_cloudtrail_lookup_is_time_bounded_and_preserves_run_tags(self) -> None:
        start = datetime(2026, 7, 18, 1, 0, tzinfo=UTC)
        end = datetime(2026, 7, 18, 2, 0, tzinfo=UTC)
        paginator = FakeCloudTrailPaginator([{"Events": [{
            "EventId": "event-1",
            "EventName": "CreateChangeSet",
            "EventTime": datetime(2026, 7, 18, 1, 1, tzinfo=UTC),
            "CloudTrailEvent": json.dumps({
                "userIdentity": {"arn": "arn:aws:iam::742711170910:root"},
                "requestParameters": {
                    "stackName": "LoopAdPerfPhase7IntegrationStack",
                    "comment": "exact comment",
                    "documentName": "AWS-RunShellScript",
                    "instanceIds": ["i-0123"],
                    "tags": [
                        {"key": "RunId", "value": "run_20260718_010000_phase7_integration"},
                        {"key": "SessionId", "value": "phase7-integration-20260718T010000Z"},
                    ],
                },
                "responseElements": None,
            }),
        }]}])

        events = lookup_events(FakeCloudTrail(paginator), "CreateChangeSet", start, end)

        self.assertEqual(start, paginator.request["StartTime"])
        self.assertEqual(end, paginator.request["EndTime"])
        self.assertEqual({
            "RunId": "run_20260718_010000_phase7_integration",
            "SessionId": "phase7-integration-20260718T010000Z",
        }, events[0]["request"]["tags"])
        self.assertEqual("2026-07-18T01:01:00Z", events[0]["eventTime"])
        self.assertEqual("exact comment", events[0]["request"]["comment"])
        self.assertEqual(["i-0123"], events[0]["request"]["instanceIds"])
        require_approved_principals(events)
        events[0]["principalArn"] = "arn:aws:iam::742711170910:role/unapproved"
        with self.assertRaisesRegex(RuntimeError, "approved operator"):
            require_approved_principals(events)

    def test_host_parser_derives_cpu_memory_and_filesystem_from_raw_counters(self) -> None:
        result = parse_host_telemetry(host_text())
        self.assertEqual(51, result["sampleCount"])
        self.assertEqual(50, len(result["cpuPercent"]))
        self.assertTrue(all(0 <= value <= 100 for value in result["cpuPercent"]))
        self.assertEqual({60.0}, set(result["memoryPercent"]))
        self.assertEqual({50.0}, set(result["filesystemPercent"]))

    def test_haproxy_summary_requires_six_backends_on_each_proxy_and_real_deltas(self) -> None:
        digest = "a" * 64
        before = {
            name: {"configSha256": digest, "metrics": prometheus(responses4=1, responses5=2)}
            for name in ("i-one", "i-two")
        }
        after = {
            name: {"configSha256": digest, "metrics": prometheus(responses4=1, responses5=2, queue=3)}
            for name in before
        }
        result = summarize_haproxy(before, after, digest)
        self.assertEqual(6, result["activeBackends"])
        self.assertEqual(3, result["maxQueue"])
        self.assertEqual(0, result["http4xx"])
        broken = {**after, "i-two": {"configSha256": digest, "metrics": prometheus(responses4=1, responses5=2, active=5)}}
        with self.assertRaisesRegex(RuntimeError, "six active"):
            summarize_haproxy(before, broken, digest)

    def test_prometheus_special_values_parse_but_selected_metrics_require_finite_values(self) -> None:
        samples = parse_prometheus("\n".join((
            "unselected_nan NaN",
            "unselected_positive_infinity +Inf",
            "unselected_negative_infinity -Inf",
        )) + "\n")
        by_name = {sample["name"]: sample["value"] for sample in samples}
        self.assertTrue(math.isnan(by_name["unselected_nan"]))
        self.assertEqual(float("inf"), by_name["unselected_positive_infinity"])
        self.assertEqual(float("-inf"), by_name["unselected_negative_infinity"])

        for token in ("NaN", "+Inf", "-Inf"):
            selected = parse_prometheus(
                f'haproxy_backend_max_queue{{proxy="collectors"}} {token}\n'
            )
            with self.subTest(token=token), self.assertRaisesRegex(
                RuntimeError, "must be finite"
            ):
                metric_value(
                    selected, "haproxy_backend_max_queue", proxy="collectors"
                )

        for token in ("nan", "Inf", "Infinity"):
            with self.subTest(token=token), self.assertRaisesRegex(
                RuntimeError, "invalid Prometheus sample"
            ):
                parse_prometheus(f"nonstandard_value {token}\n")

    def test_collector_final_ack_delta_is_real_and_counter_reset_fails(self) -> None:
        before = {
            f"i-{index}": {"kinesis": {"put_records": {
                "successes": 10, "failures": 0, "retries": 0,
                "partial_failures": 0, "timeouts": 0,
            }}}
            for index in range(6)
        }
        after = {
            key: {"kinesis": {"put_records": {
                "successes": 110, "failures": 0, "retries": 1,
                "partial_failures": 0, "timeouts": 0,
            }}}
            for key in before
        }
        result = summarize_collectors(before, after)
        self.assertEqual(600, result["successes"])
        self.assertEqual(6, result["retries"])
        after["i-0"]["kinesis"]["put_records"]["successes"] = 9
        with self.assertRaisesRegex(RuntimeError, "counter reset"):
            summarize_collectors(before, after)

    def test_resource_p95_uses_the_worst_host_instead_of_pooled_samples(self) -> None:
        normal = {
            "sampleCount": 51,
            "cpuPercent": [20.0] * 50,
            "memoryPercent": [30.0] * 51,
            "filesystemPercent": [40.0] * 51,
        }
        hot = {
            **normal,
            "cpuPercent": [20.0] * 45 + [95.0] * 5,
        }
        telemetry = {
            "collector": {f"i-{index}": "raw" for index in range(6)},
            "consumer": {f"i-c{index}": "raw" for index in range(2)},
            "clickHouse": {"i-ch": "raw"},
        }
        with patch("runtime_observability.parse_host_telemetry", side_effect=[hot] + [normal] * 8):
            result = summarize_resources(telemetry)
        self.assertEqual(95.0, result["collector"]["cpuP95Percent"])
        self.assertEqual(95.0, result["collector"]["hosts"]["i-0"]["cpuP95Percent"])

    def test_remote_telemetry_commands_are_run_scoped_and_contain_no_credentials(self) -> None:
        unit = telemetry_unit(
            "run_20260718_010203_phase7_integration", "clickHouse", "i-0123456789abcdef0"
        )
        command = start_telemetry_command(unit) + stop_telemetry_command(unit)
        self.assertIn(unit, command)
        self.assertNotRegex(command, r"AWS_(?:ACCESS|SECRET|SESSION)")

    def test_cost_status_uses_the_full_operational_bound_not_zero_preflight_accrual(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            inputs = run_dir / "inputs"
            inputs.mkdir()
            (inputs / "cost-model.json").write_text(json.dumps({
                "accruedUpperBoundUsd": "0.000000",
                "operationalMaximumUsd": "33.855718",
                "maximumIncludingCleanupUsd": "38.855718",
                "cleanupReserveUsd": "5.000000",
            }) + "\n", encoding="utf-8")
            aws = SimpleNamespace(bundle=SimpleNamespace(
                run_dir=run_dir,
                run_id="run_20260718_010000_phase7_integration",
                session_id="phase7-integration-20260718T010000Z",
            ))

            result = write_cost_status(aws)

            self.assertEqual("33.855718", result["cost"]["accruedUpperBoundUsd"])
            self.assertIn("180-minute", result["cost"]["basis"])

    def test_cloudtrail_classifier_requires_every_recorded_command_and_rejects_duplicates(self) -> None:
        principal = "arn:aws:iam::742711170910:root"
        run_id = "run_20260718_010000_phase7_integration"
        session_id = "phase7-integration-20260718T010000Z"
        task_definition = "arn:aws:ecs:ap-northeast-2:742711170910:task-definition/archive:1"
        cluster = "phase7-archive"
        started_by = "phase7-archive-20260718010000"
        expectations: dict[str, dict[str, dict[str, object]]] = {}
        send_events: list[dict[str, object]] = []
        for stage, suffix in (("warmup", "a"), ("score", "b")):
            by_id: dict[str, dict[str, object]] = {}
            for index in range(16):
                command_id = f"{index:08x}-0000-4000-8000-{suffix * 12}"
                comment = f"loop-ad run_20260718_010000_phase7_{stage} kind-{index // 8}"
                instance_id = f"i-{index + 1:016x}"
                by_id[command_id] = {"comment": comment, "instanceId": instance_id}
                send_events.append({
                    "principalArn": principal,
                    "request": {
                        "comment": comment,
                        "documentName": "AWS-RunShellScript",
                        "instanceIds": [instance_id],
                    },
                    "response": {"commandId": command_id},
                })
            expectations[stage] = by_id
        deploy = [{
            "principalArn": principal,
            "request": {
                "stackName": "LoopAdPerfPhase7IntegrationStack",
                "tags": {"RunId": run_id, "SessionId": session_id},
            },
        }]
        archive = [{
            "principalArn": principal,
            "request": {
                "startedBy": started_by,
                "taskDefinition": task_definition,
                "cluster": cluster,
            },
        }]

        result = classify_execution_events(
            deploy_events=deploy,
            send_events=send_events,
            archive_events=archive,
            run_id=run_id,
            session_id=session_id,
            command_expectations=expectations,
            started_by=started_by,
            task_definition=task_definition,
            cluster=cluster,
        )

        self.assertEqual(16, len(result["warmup"]))
        self.assertEqual(16, len(result["score"]))
        with self.assertRaisesRegex(RuntimeError, "duplicate warmup SendCommand"):
            classify_execution_events(
                deploy_events=deploy,
                send_events=[*send_events, send_events[0]],
                archive_events=archive,
                run_id=run_id,
                session_id=session_id,
                command_expectations=expectations,
                started_by=started_by,
                task_definition=task_definition,
                cluster=cluster,
            )
        mutated = json.loads(json.dumps(send_events))
        mutated[0]["request"]["comment"] = (
            "loop-ad run_20260718_010000_phase7_warmup unexpected-command"
        )
        with self.assertRaisesRegex(RuntimeError, "unowned or malformed warmup"):
            classify_execution_events(
                deploy_events=deploy,
                send_events=mutated,
                archive_events=archive,
                run_id=run_id,
                session_id=session_id,
                command_expectations=expectations,
                started_by=started_by,
                task_definition=task_definition,
                cluster=cluster,
            )


if __name__ == "__main__":
    unittest.main()

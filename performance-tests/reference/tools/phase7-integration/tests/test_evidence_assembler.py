from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest


AWS_DIR = Path(__file__).resolve().parents[1] / "aws"
sys.path.insert(0, str(AWS_DIR))

from evidence_assembler import (  # noqa: E402
    ARTIFACT_CONTRACTS,
    CLEANUP_SERVICE_CLASSES,
    EvidenceAssemblyError,
    REQUIRED_ARTIFACT_ALLOWLIST,
    assemble_evidence,
    canonical_sha256,
    compute_assembly_sha256,
    finalize_run_document,
    required_artifacts_are_valid,
)
from evaluator import evaluate  # noqa: E402


RUN_ID = "run_20260718_180000_phase7_integration"
SESSION_ID = "phase7-integration-20260718T180000Z"
OTHER_SESSION_ID = "phase7-integration-20260718T180001Z"
BASE_TIME = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)
PRE_CLEANUP_STAGES = (
    "deploy",
    "verify",
    "correctness",
    "seed",
    "warmup",
    "score_archive",
    "drain_validate",
    "collect",
)
ALL_RUNNER_STAGES = (*PRE_CLEANUP_STAGES, "cleanup", "inventory", "evaluate")
CLOUDTRAIL_PATHS = (
    "evidence/cloudtrail/deploy.json",
    "evidence/cloudtrail/warmup.json",
    "evidence/cloudtrail/score.json",
    "evidence/cloudtrail/archive.json",
)


def iso(minutes: int) -> str:
    return (BASE_TIME + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def worker_run_id(stage: str) -> str:
    return f"run_20260718_180000_phase7_{stage}"


def command_comment(stage: str, kind: str) -> str:
    worker = worker_run_id(stage)
    if kind == "load":
        duration = 180 if stage == "warmup" else 300
        return f"loop-ad {worker} oha 6250rps {duration}s"
    return f"loop-ad {worker} 20KiB SSM transfer probe"


def command_id(stage: str, node: int, kind: str) -> str:
    sequence = (0 if stage == "warmup" else 100) + node * 2 + (0 if kind == "load" else 1)
    return f"00000000-0000-4000-8000-{sequence:012x}"


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def stage_command_document(stage: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "argv": ["/usr/bin/true", stage],
        "cwd": "/tmp",
        "environment": {"AWS_REGION": "ap-northeast-2"},
        "timeoutSeconds": 1_200,
        "nonzeroDisposition": "acceptance-failure" if stage == "evaluate" else "hard-stop",
    }


def command_metadata() -> dict[str, dict[str, str]]:
    return {
        stage: {
            "path": f"inputs/{stage}-command.json",
            "sha256": canonical_sha256(stage_command_document(stage)),
        }
        for stage in ALL_RUNNER_STAGES
    }


def command_seal_document() -> dict[str, object]:
    metadata = command_metadata()
    return {
        "schemaVersion": 1,
        "runId": RUN_ID,
        "sessionId": SESSION_ID,
        "commands": metadata,
        "commandSetSha256": canonical_sha256(metadata),
    }


def stage_attempt(
    ordinal: int,
    stage: str,
    started_minute: int,
    finished_minute: int | None,
    *,
    exit_code: int | None = 0,
) -> dict[str, object]:
    attempt: dict[str, object] = {
        "ordinal": ordinal,
        "attempt": 1,
        "stage": stage,
        "commandSha256": canonical_sha256(stage_command_document(stage)),
        "startedAt": iso(started_minute),
        "timeoutSeconds": 1_200,
        "status": "in-progress" if finished_minute is None else "finished",
        "exitCode": exit_code,
        "failureDisposition": None,
    }
    if finished_minute is not None:
        attempt["finishedAt"] = iso(finished_minute)
    return attempt


def run_document() -> dict[str, object]:
    intervals = ((0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (50, 85), (85, 110), (110, 120))
    attempts = [
        stage_attempt(index, stage, *intervals[index - 1])
        for index, stage in enumerate(PRE_CLEANUP_STAGES, start=1)
    ]
    attempts.extend(
        (
            stage_attempt(9, "cleanup", 120, 130),
            stage_attempt(10, "inventory", 130, 135),
            stage_attempt(11, "evaluate", 135, None, exit_code=None),
        )
    )
    return {
        "schemaVersion": 2,
        "runId": RUN_ID,
        "sessionId": SESSION_ID,
        "phase": "7-2",
        "phase5": "skipped",
        "status": "running",
        "verdict": None,
        "initializedAt": iso(-5),
        "paidStartedAt": iso(0),
        "cleanupStartDeadline": iso(160),
        "hardDeadline": iso(180),
        "completedStages": [*PRE_CLEANUP_STAGES, "cleanup", "inventory"],
        "attemptedStages": [item["stage"] for item in attempts],
        "stageAttempts": attempts,
        "inProgressStage": {
            key: attempts[-1][key]
            for key in ("ordinal", "attempt", "stage", "commandSha256", "startedAt", "timeoutSeconds")
        },
        "cleanupOnly": False,
        "failedStage": None,
        "failureDisposition": None,
        "commandSetRequired": True,
        "commandSetSha256": canonical_sha256(command_metadata()),
    }


def recorded_command_document(stage: str, node: int, kind: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "kind": "phase7-oha-load-command" if kind == "load" else "phase7-ssm-transfer-probe",
        "runId": worker_run_id(stage),
        "stageLabel": stage,
        "nodeId": f"node-{node:02d}",
        "instanceId": f"i-{node:017x}",
        "commandId": command_id(stage, node, kind),
        "comment": command_comment(stage, kind),
        "recordedAt": iso(45 if stage == "warmup" else 55),
    }


def cloudtrail_documents() -> dict[str, dict[str, object]]:
    documents: dict[str, dict[str, object]] = {
        "deploy": {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "events": [{
                "eventId": "deploy-event",
                "eventName": "CreateChangeSet",
                "eventTime": iso(1),
                "principalArn": "arn:aws:iam::742711170910:root",
                "request": {
                    "stackName": "LoopAdPerfPhase7IntegrationStack",
                    "tags": {"RunId": RUN_ID, "SessionId": SESSION_ID},
                },
                "response": {},
            }],
        },
        "archive": {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "events": [{
                "eventId": "archive-event",
                "eventName": "RunTask",
                "eventTime": iso(55),
                "principalArn": "arn:aws:iam::742711170910:root",
                "request": {
                    "startedBy": "phase7-archive-20260718180000",
                    "taskDefinition": "arn:aws:ecs:ap-northeast-2:742711170910:task-definition/archive:1",
                    "cluster": "arn:aws:ecs:ap-northeast-2:742711170910:cluster/archive",
                },
                "response": {},
            }],
        },
    }
    for stage in ("warmup", "score"):
        events = []
        for node in range(1, 9):
            for kind in ("transfer", "load"):
                events.append({
                    "eventId": f"{stage}-{kind}-{node}",
                    "eventName": "SendCommand",
                    "eventTime": iso(45 if stage == "warmup" else 55),
                    "principalArn": "arn:aws:iam::742711170910:root",
                    "request": {
                        "comment": command_comment(stage, kind),
                        "documentName": "AWS-RunShellScript",
                        "instanceIds": [f"i-{node:017x}"],
                    },
                    "response": {"commandId": command_id(stage, node, kind)},
                })
        documents[stage] = {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "events": events,
        }
    return documents


def archive_document() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "runId": RUN_ID,
        "sessionId": SESSION_ID,
        "rows": 15_000_000,
        "objects": 3,
        "objectRows": [5_000_000, 5_000_000, 5_000_000],
        "preDropSourceMinusArchive": 0,
        "preDropArchiveMinusSource": 0,
        "committedSourceMinusArchive": 0,
        "committedArchiveMinusSource": 0,
        "postDropReferenceMinusArchive": 0,
        "postDropArchiveMinusReference": 0,
        "sourceRowsAfterDrop": 0,
        "liveRowsAfterDrop": 480,
        "committedReRead": True,
        "overlappedScoreWindow": True,
        "cycleSeconds": 1_200,
        "workerResult": {"status": "passed"},
    }


def raw_host_text(*, filesystem_percent: int = 50, samples: int = 60) -> str:
    rows = []
    for index in range(samples):
        rows.append("\t".join(str(value) for value in (
            1_700_000_000 + index * 5,
            100 + index * 60,
            0,
            0,
            100 + index * 40,
            0,
            0,
            0,
            0,
            1_000,
            400,
            1_000,
            filesystem_percent * 10,
        )))
    return "\n".join(rows) + "\n"


def collector_snapshot(successes: int) -> dict[str, object]:
    return {
        f"i-{index:017x}": {
            "kinesis": {
                "put_records": {
                    "successes": successes,
                    "failures": 0,
                    "retries": 0,
                    "partial_failures": 0,
                    "timeouts": 0,
                }
            }
        }
        for index in range(1, 7)
    }


def service_snapshots() -> dict[str, object]:
    return {
        role: {"tasks": [{"taskArn": f"arn:aws:ecs:task/{role.lower()}-{index}"}]}
        for index, role in enumerate(("Collector", "Haproxy", "Consumer", "ClickHouse"), start=1)
    }


def raw_observability_document() -> dict[str, object]:
    services = service_snapshots()
    service_arns = sorted(
        task["taskArn"]
        for role in services.values()
        for task in role["tasks"]
    )
    collector_delta = {
        "successes": 15_000_000,
        "failures": 0,
        "retries": 0,
        "partial_failures": 0,
        "timeouts": 0,
    }
    return {
        "schemaVersion": 1,
        "runId": RUN_ID,
        "sessionId": SESSION_ID,
        "hostTelemetry": {
            "collector": {
                f"i-{index:017x}": raw_host_text()
                for index in range(1, 7)
            },
            "consumer": {
                f"i-{index:017x}": raw_host_text()
                for index in range(7, 9)
            },
            "clickHouse": {
                "i-00000000000000009": raw_host_text(filesystem_percent=70)
            },
        },
        "collectorBefore": collector_snapshot(0),
        "collectorAfter": collector_snapshot(2_500_000),
        "servicesBefore": copy.deepcopy(services),
        "servicesAfter": copy.deepcopy(services),
        "failureEvidence": {
            "schemaVersion": 1,
            "kinesisThrottleMetric": {
                "namespace": "AWS/Kinesis",
                "metricName": "WriteProvisionedThroughputExceeded",
                "dimensions": [{"Name": "StreamName", "Value": RUN_ID}],
                "startTime": iso(50),
                "endTime": iso(110),
                "periodSeconds": 60,
                "statistic": "Sum",
                "datapoints": [{"timestamp": iso(60), "sum": 0.0}],
                "sum": 0.0,
            },
            "failureObjects": {
                "bucket": "phase7-failures",
                "prefix": f"failures/{RUN_ID}/",
                "keys": [],
            },
            "clickHouseInsertErrorQuery": {
                "logGroup": f"/loopad/perf/phase7/{RUN_ID}/ConsumerLogs",
                "startEpoch": 1,
                "endEpoch": 2,
                "query": "stats count(*) as count",
                "queryId": "query-1",
                "status": "Complete",
                "results": [[{"field": "count", "value": "0"}]],
                "count": 0,
            },
            "stoppedTaskQuery": {
                "startEpoch": 1,
                "endEpoch": 2,
                "tasks": [],
                "oomCount": 0,
            },
            "collectorDelta": collector_delta,
            "serviceTaskArnsBefore": service_arns,
            "serviceTaskArnsAfter": service_arns,
            "kclTerminalFailure": 0,
            "archiveWorkerStatus": "passed",
        },
    }


def cost_model_document() -> dict[str, object]:
    return {
        "passed": True,
        "operationalMaximumUsd": 30,
        "maximumIncludingCleanupUsd": 39,
        "cleanupReserveUsd": 5,
    }


def resource_summary(start: int, count: int, *, filesystem: float | None = None) -> dict[str, object]:
    hosts = {
        f"i-{index:017x}": {
            "sampleCount": 60,
            "cpuP95Percent": 60,
            "memoryP95Percent": 60,
            "filesystemPeakPercent": 70 if filesystem is not None else 50,
        }
        for index in range(start, start + count)
    }
    result: dict[str, object] = {
        "cpuP95Percent": 60,
        "memoryP95Percent": 60,
        "sampleCount": count * 60,
        "hosts": hosts,
    }
    if filesystem is not None:
        result["filesystemPeakPercent"] = filesystem
    return result


def fixture_documents() -> dict[str, dict[str, object]]:
    archive = archive_document()
    cloudtrail = cloudtrail_documents()
    raw_observability = raw_observability_document()
    cost_model = cost_model_document()
    return {
        "runState": run_document(),
        "commandSeal": command_seal_document(),
        "deploymentVerification": {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "verifiedAt": iso(20),
            "identity": {
                "account": "742711170910",
                "arn": "arn:aws:iam::742711170910:root",
            },
            "stackStatus": "CREATE_COMPLETE",
            "stackTags": {"RunId": RUN_ID, "SessionId": SESSION_ID, "ResourceScope": "run"},
            "stream": {"name": RUN_ID, "status": "ACTIVE", "openShardCount": 120},
            "protocolPath": {"scheme": "internal", "activeBackendsPerProxy": [6, 6]},
            "passed": True,
        },
        "correctnessSummary": {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "generatedAt": iso(30),
            "correctness": {
                "http": {"http202": 1_000, "non202": 0, "total": 1_000},
                "directKinesis": {"accepted": 2, "failed": 0, "shardIds": ["shardId-000"]},
                "counts": {"final": 1_000, "unique": 1_000, "physical": 1_000, "raw": 1},
                "lateEventDropped": 1,
                "inputRecords": 1_002,
                "passed": True,
            },
            "replacement": {
                "offered": 900,
                "accepted": 900,
                "stoppedTask": "task-a",
                "baselineTasks": ["task-a", "task-b"],
                "currentTasks": ["task-b", "task-c"],
                "counts": {"final": 900, "unique": 900, "physical": 900},
                "leasesBefore": {"count": 120, "sha256": "1" * 64},
                "leasesAfter": {"count": 120, "sha256": "2" * 64},
                "passed": True,
            },
            "passed": True,
        },
        "seedSummary": {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "seededAt": iso(40),
            "partition": "2026-07-10",
            "today": "2026-07-18",
            "rows": 15_000_000,
            "generatorContract": {
                "version": "phase6-seed-v1",
                "seed": 20260718,
                "partition": "2026-07-10",
                "rows": 15_000_000,
                "runId": RUN_ID,
            },
            "fingerprintSamples": [
                {"rows": 15_000_000, "uniqueEvents": 15_000_000, "checksum": "123"},
                {"rows": 15_000_000, "uniqueEvents": 15_000_000, "checksum": "123"},
            ],
            "stable": True,
            "durationSeconds": 300,
        },
        "warmupStageSummary": {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "stage": "warmup",
            "identityMode": "balanced-pool-sampled-with-replacement",
            "identityContract": {
                "predeclaredBeforeDeploy": True,
                "userApproved": True,
                "selectionWithReplacement": True,
                "warmupScorePoolsSeparated": True,
                "balancedShardCount": 120,
                "fixturePoolRows": 480,
            },
            "aggregate": {
                "actualRps": 50_000,
                "durationSeconds": 180,
                "attemptedRequests": 9_000_000,
                "completedRequests": 9_000_000,
                "transportErrors": 0,
                "http202": 9_000_000,
            },
            "accounting": {
                "http202": 9_000_000,
                "kinesisAccepted": 9_000_000,
                "kclProcessed": 9_000_000,
                "clickHouseInserted": 9_000_000,
            },
            "drain": {"progressed": True, "samples": [{"maximumMs": 0}]},
            "archive": None,
            "diagnosticContinuationAllowed": True,
        },
        "scoreStageSummary": {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "stage": "score",
            "identityMode": "balanced-pool-sampled-with-replacement",
            "identityContract": {
                "predeclaredBeforeDeploy": True,
                "userApproved": True,
                "selectionWithReplacement": True,
                "warmupScorePoolsSeparated": True,
                "balancedShardCount": 120,
                "fixturePoolRows": 480,
            },
            "archive": {
                "startedBy": "phase7-archive-20260718180000",
                "runTaskInvocationCount": 1,
                "taskCardinality": 1,
                "startedAt": iso(55),
                "stoppedAt": iso(75),
                "exitCode": 0,
            },
            "aggregate": {
                "actualRps": 50_000,
                "durationSeconds": 300,
                "attemptedRequests": 15_000_000,
                "completedRequests": 15_000_000,
                "transportErrors": 0,
                "http202": 15_000_000,
                "http429": 0,
                "http5xx": 0,
                "latencyCorrectedMs": {"p95": 250},
            },
        },
        "drainAccounting": {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "counts": {
                "http202": 15_000_000,
                "collectorFinalAck": 15_000_000,
                "kinesisAccepted": 15_000_000,
                "kclProcessed": 15_000_000,
                "clickHouseInserted": 15_000_000,
                "clickHouseLiveUnique": 480,
                "fixturePoolRows": 480,
            },
            "failures": {"terminalFailure": 0, "checkpointError": 0},
            "drain": {
                "seconds": 1_200,
                "iteratorAgeProgressed": True,
                "visibilityP50Ms": 10,
                "visibilityP95Ms": 50,
                "visibilityP99Ms": 100,
            },
            "archive": copy.deepcopy(archive),
        },
        "archiveValidation": archive,
        "observabilitySummary": {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "resources": {
                "collector": resource_summary(1, 6),
                "consumer": resource_summary(7, 2),
                "clickHouse": resource_summary(9, 1, filesystem=70),
            },
            "haproxy": {
                "configSha256": "a" * 64,
                "activeBackends": 6,
                "maxQueue": 0,
                "http4xx": 0,
                "http5xx": 0,
                "prometheusCollected": True,
            },
            "failures": {
                "kinesisThrottle": 0,
                "collectorFinalFailure": 0,
                "kclTerminalFailure": 0,
                "failureObjects": 0,
                "clickHouseInsertErrors": 0,
                "archiveFailures": 0,
                "unexpectedRestarts": 0,
                "oomKills": 0,
            },
            "cloudTrail": {
                "collected": True,
                "deployAttempts": 1,
                "warmupAttempts": 1,
                "scoreAttempts": 1,
                "archiveAttempts": 1,
                "sourcePaths": list(CLOUDTRAIL_PATHS),
                "sha256": [
                    hashlib.sha256(json_bytes(cloudtrail[Path(path).stem])).hexdigest()
                    for path in CLOUDTRAIL_PATHS
                ],
            },
            "rawEvidence": {
                "path": "evidence/score-observability/after.json",
                "sha256": hashlib.sha256(json_bytes(raw_observability)).hexdigest(),
            },
        },
        "costStatus": {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "cost": {
                "accruedUpperBoundUsd": 30,
                "maximumIncludingCleanupUsd": 39,
                "cleanupReserveUsd": 5,
                "basis": "full approved 180-minute operational maximum at collection time",
            },
            "sourceCostModelSha256": hashlib.sha256(json_bytes(cost_model)).hexdigest(),
        },
        "cleanupInventory": {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "counts": {name: 0 for name in CLEANUP_SERVICE_CLASSES},
            "resources": {name: [] for name in CLEANUP_SERVICE_CLASSES},
            "taggingApiResiduals": [],
            "taggingApiAuthoritative": False,
            "serviceInventoryZero": True,
            "taggingApiResidualsZero": True,
            "allZero": True,
        },
    }


def gate_document(run: dict[str, object], attempt: dict[str, object]) -> dict[str, object]:
    paid = datetime.fromisoformat(str(run["paidStartedAt"]).replace("Z", "+00:00"))
    started = datetime.fromisoformat(str(attempt["startedAt"]).replace("Z", "+00:00"))
    elapsed = max(0.0, (started - paid).total_seconds())
    return {
        "evaluatedAt": attempt["startedAt"],
        "stage": attempt["stage"],
        "elapsedPaidMinutes": round(elapsed / 60, 3),
        "stageTimeoutSeconds": attempt["timeoutSeconds"],
        "remainingPreCleanupReserveSeconds": 0,
        "cleanupWindowSecondsRemaining": round(160 * 60 - elapsed, 3),
        "hardDeadlineSecondsRemaining": round(180 * 60 - elapsed, 3),
        "checks": {
            "stageSequenceValid": True,
            "noUnconfirmedAttempt": True,
            "cleanupOnlyStateRespected": True,
            "stageWorstCaseFitsCleanupWindow": True,
            "hardDeadlineNotPassedForNonCleanup": True,
            "costGatePassedForNewWork": True,
            "oneShotNotPreviouslyAttempted": True,
        },
        "allowed": True,
        "cleanupRequired": attempt["stage"] in {"cleanup", "inventory", "evaluate"},
        "hardDeadlineBreached": elapsed >= 180 * 60,
    }


def attempt_control_document(attempt: dict[str, object]) -> dict[str, object]:
    stage = str(attempt["stage"])
    number = int(attempt["attempt"])
    return {
        "schemaVersion": 2,
        "stage": stage,
        "attempt": number,
        "startedAt": attempt["startedAt"],
        "finishedAt": attempt["finishedAt"],
        "commandSha256": attempt["commandSha256"],
        "timeoutSeconds": attempt["timeoutSeconds"],
        "timedOut": attempt["status"] == "timed-out",
        "exitCode": attempt["exitCode"],
        "failureDisposition": attempt["failureDisposition"],
        "stdoutPath": f"evidence/control/{stage}.attempt-{number}.stdout.log",
        "stderrPath": f"evidence/control/{stage}.attempt-{number}.stderr.log",
        "passed": attempt["exitCode"] == 0,
    }


def write_runner_control(run_dir: Path, run: dict[str, object]) -> None:
    inputs = run_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    for stage in ALL_RUNNER_STAGES:
        (inputs / f"{stage}-command.json").write_bytes(
            json_bytes(stage_command_document(stage))
        )
    (inputs / "command-seal.json").write_bytes(json_bytes(command_seal_document()))

    control = run_dir / "evidence" / "control"
    control.mkdir(parents=True, exist_ok=True)
    for path in [*control.glob("*-gate.json"), *control.glob("*.attempt-*.json")]:
        path.unlink()
    stage_numbers: dict[str, int] = {}
    for attempt in run["stageAttempts"]:
        stage = str(attempt["stage"])
        stage_numbers[stage] = stage_numbers.get(stage, 0) + 1
        if attempt.get("attempt") != stage_numbers[stage]:
            raise AssertionError(f"invalid fixture attempt number for {stage}")
        (control / f"{stage}-gate.json").write_bytes(
            json_bytes(gate_document(run, attempt))
        )
        if attempt.get("status") != "in-progress":
            (control / f"{stage}.attempt-{attempt['attempt']}.json").write_bytes(
                json_bytes(attempt_control_document(attempt))
            )
    snapshot = control / "evaluate-start-run.json"
    snapshot.write_bytes(json_bytes(run))
    (run_dir / "run.json").write_bytes(json_bytes(run))


def write_fixture(run_dir: Path) -> dict[str, dict[str, object]]:
    documents = fixture_documents()
    raw_path = run_dir / "evidence" / "score-observability" / "after.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(json_bytes(raw_observability_document()))
    cost_path = run_dir / "inputs" / "cost-model.json"
    cost_path.parent.mkdir(parents=True, exist_ok=True)
    cost_path.write_bytes(json_bytes(cost_model_document()))
    write_runner_control(run_dir, documents["runState"])
    for stage in ("warmup", "score"):
        for node in range(1, 9):
            node_dir = run_dir / "evidence" / stage / f"node-{node:02d}"
            node_dir.mkdir(parents=True, exist_ok=True)
            for kind, file_name in (
                ("load", "ssm-command-started.json"),
                ("transfer", "ssm-transfer-probe-started.json"),
            ):
                (node_dir / file_name).write_bytes(
                    json_bytes(recorded_command_document(stage, node, kind))
                )
    for name, document in cloudtrail_documents().items():
        path = run_dir / "evidence" / "cloudtrail" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json_bytes(document))
    for name, contract in ARTIFACT_CONTRACTS.items():
        if name in {"runState", "commandSeal"}:
            continue
        path = run_dir / contract.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(documents[name], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return documents


def write_artifact(
    run_dir: Path, documents: dict[str, dict[str, object]], name: str
) -> None:
    if name == "runState":
        write_runner_control(run_dir, documents[name])
        return
    path = run_dir / ARTIFACT_CONTRACTS[name].path
    path.write_bytes(json_bytes(documents[name]))


def rewrite_cloudtrail_and_digest(
    run_dir: Path,
    documents: dict[str, dict[str, object]],
    name: str,
    mutate,
) -> None:
    path = run_dir / "evidence" / "cloudtrail" / f"{name}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    raw = json_bytes(document)
    path.write_bytes(raw)
    cloudtrail = documents["observabilitySummary"]["cloudTrail"]
    index = list(cloudtrail["sourcePaths"]).index(path.relative_to(run_dir).as_posix())
    cloudtrail["sha256"][index] = hashlib.sha256(raw).hexdigest()
    write_artifact(run_dir, documents, "observabilitySummary")


def rewrite_raw_observability_and_digest(
    run_dir: Path,
    documents: dict[str, dict[str, object]],
    mutate,
) -> None:
    path = run_dir / "evidence" / "score-observability" / "after.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    raw = json_bytes(document)
    path.write_bytes(raw)
    documents["observabilitySummary"]["rawEvidence"]["sha256"] = hashlib.sha256(
        raw
    ).hexdigest()
    write_artifact(run_dir, documents, "observabilitySummary")


class EvidenceAssemblerTest(unittest.TestCase):
    def test_complete_fixture_assembles_evaluates_and_finalizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            documents = write_fixture(run_dir)

            evidence = assemble_evidence(run_dir)

            self.assertEqual(RUN_ID, evidence["runId"])
            self.assertEqual(SESSION_ID, evidence["sessionId"])
            self.assertEqual("skipped", evidence["phase5"])
            self.assertEqual(REQUIRED_ARTIFACT_ALLOWLIST, set(evidence["requiredArtifacts"]))
            for name, contract in ARTIFACT_CONTRACTS.items():
                metadata = evidence["requiredArtifacts"][name]
                self.assertEqual(contract.path, metadata["path"])
                self.assertEqual(contract.provenance, metadata["provenance"])
                self.assertRegex(metadata["sha256"], r"^[0-9a-f]{64}$")
                self.assertEqual(RUN_ID, metadata["runId"])
                self.assertEqual(SESSION_ID, metadata["sessionId"])
            self.assertEqual(0, evidence["performance"]["transportErrorRate"])
            self.assertEqual(15_000_000, evidence["counts"]["clickHouseInserted"])

            evaluation = evaluate(evidence, run_dir)
            self.assertEqual("passed", evaluation["verdict"])
            self.assertTrue(evaluation["checks"]["requiredArtifactsComplete"])
            self.assertTrue(evaluation["checks"]["phase5Skipped"])

            finalized = finalize_run_document(documents["runState"], evaluation)
            self.assertEqual("completed", finalized["status"])
            self.assertEqual("passed", finalized["verdict"])
            self.assertEqual("skipped", finalized["phase5"])
            self.assertRegex(finalized["finalEvaluation"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertIsNone(documents["runState"]["verdict"])

    def test_every_required_artifact_is_fail_closed_when_missing(self) -> None:
        for name, contract in ARTIFACT_CONTRACTS.items():
            with self.subTest(artifact=name), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                write_fixture(run_dir)
                (run_dir / contract.path).unlink()
                with self.assertRaises(EvidenceAssemblyError):
                    assemble_evidence(run_dir)

    def test_every_required_artifact_is_fail_closed_when_corrupt(self) -> None:
        for name, contract in ARTIFACT_CONTRACTS.items():
            with self.subTest(artifact=name), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                write_fixture(run_dir)
                (run_dir / contract.path).write_text("{", encoding="utf-8")
                with self.assertRaises(EvidenceAssemblyError):
                    assemble_evidence(run_dir)

    def test_every_required_artifact_is_bound_to_one_run_and_session(self) -> None:
        for name, contract in ARTIFACT_CONTRACTS.items():
            with self.subTest(artifact=name), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                documents = write_fixture(run_dir)
                documents[name]["sessionId"] = OTHER_SESSION_ID
                (run_dir / contract.path).write_text(
                    json.dumps(documents[name]) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(EvidenceAssemblyError):
                    assemble_evidence(run_dir)

    def test_digest_tampering_and_arbitrary_allowlist_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            evidence = assemble_evidence(run_dir)

            digest_mismatch = copy.deepcopy(evidence)
            digest_mismatch["requiredArtifacts"]["scoreStageSummary"]["sha256"] = "b" * 64
            digest_mismatch["requiredArtifactsSha256"] = canonical_sha256(
                digest_mismatch["requiredArtifacts"]
            )
            digest_mismatch["assemblySha256"] = compute_assembly_sha256(digest_mismatch)
            failed = evaluate(digest_mismatch, run_dir)
            self.assertEqual("failed", failed["verdict"])
            self.assertIn("requiredArtifactsComplete", failed["failedChecks"])

            arbitrary = copy.deepcopy(evidence)
            arbitrary["requiredArtifacts"] = {"x": True}
            arbitrary["requiredArtifactsSha256"] = canonical_sha256(
                arbitrary["requiredArtifacts"]
            )
            arbitrary["assemblySha256"] = compute_assembly_sha256(arbitrary)
            failed = evaluate(arbitrary, run_dir)
            self.assertEqual("failed", failed["verdict"])
            self.assertIn("requiredArtifactsComplete", failed["failedChecks"])

    def test_mismatched_archive_copy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            documents = write_fixture(run_dir)
            documents["archiveValidation"]["cycleSeconds"] = 1_201
            path = run_dir / ARTIFACT_CONTRACTS["archiveValidation"].path
            path.write_text(json.dumps(documents["archiveValidation"]) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceAssemblyError, "does not exactly match"):
                assemble_evidence(run_dir)

    def test_cloudtrail_source_digest_is_verified_from_the_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            (run_dir / "evidence/cloudtrail/deploy.json").write_bytes(b"tampered\n")
            with self.assertRaisesRegex(EvidenceAssemblyError, "source digest mismatch"):
                assemble_evidence(run_dir)

    def test_observability_raw_and_cost_model_sources_are_digest_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            (run_dir / "evidence/score-observability/after.json").write_text(
                json.dumps({"schemaVersion": 1, "runId": RUN_ID}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvidenceAssemblyError, "raw evidence digest mismatch"):
                assemble_evidence(run_dir)
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            (run_dir / "inputs/cost-model.json").write_text(
                json.dumps({**cost_model_document(), "operationalMaximumUsd": 31}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvidenceAssemblyError, "cost model digest mismatch"):
                assemble_evidence(run_dir)

    def test_observability_role_summary_must_be_the_worst_host_p95(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            documents = write_fixture(run_dir)
            collector = documents["observabilitySummary"]["resources"]["collector"]
            first = next(iter(collector["hosts"].values()))
            first["cpuP95Percent"] = 90
            write_artifact(run_dir, documents, "observabilitySummary")
            with self.assertRaisesRegex(EvidenceAssemblyError, "worst per-host p95"):
                assemble_evidence(run_dir)

    def test_raw_observability_is_recomputed_instead_of_trusting_the_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            documents = write_fixture(run_dir)
            rewrite_raw_observability_and_digest(
                run_dir,
                documents,
                lambda raw: raw["hostTelemetry"]["collector"].update(
                    {"i-00000000000000001": "not-telemetry\n"}
                ),
            )
            with self.assertRaisesRegex(EvidenceAssemblyError, "raw host telemetry"):
                assemble_evidence(run_dir)

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            documents = write_fixture(run_dir)

            def fabricate_failure(raw):
                metric = raw["failureEvidence"]["kinesisThrottleMetric"]
                metric["datapoints"][0]["sum"] = 1.0
                metric["sum"] = 1.0

            rewrite_raw_observability_and_digest(run_dir, documents, fabricate_failure)
            with self.assertRaisesRegex(EvidenceAssemblyError, "failure summary differs"):
                assemble_evidence(run_dir)

    def test_command_seal_gate_and_attempt_control_bytes_are_required(self) -> None:
        mutations = (
            (
                "sealed command",
                lambda run_dir: (run_dir / "inputs/deploy-command.json").write_bytes(
                    json_bytes({**stage_command_document("deploy"), "argv": ["/usr/bin/false"]})
                ),
                "command content digest mismatch",
            ),
            (
                "gate",
                lambda run_dir: (run_dir / "evidence/control/warmup-gate.json").unlink(),
                "missing or out-of-run warmup gate",
            ),
            (
                "attempt",
                lambda run_dir: (run_dir / "evidence/control/score_archive.attempt-1.json").unlink(),
                "missing or out-of-run score_archive attempt",
            ),
        )
        for description, mutate, message in mutations:
            with self.subTest(description), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                write_fixture(run_dir)
                mutate(run_dir)
                with self.assertRaisesRegex(EvidenceAssemblyError, message):
                    assemble_evidence(run_dir)

    def test_cloudtrail_semantics_require_all_16_recorded_commands_per_load_stage(self) -> None:
        mutations = (
            (
                "empty warmup",
                "warmup",
                lambda document: document.update(events=[]),
                "exactly 16 events",
            ),
            (
                "wrong principal",
                "deploy",
                lambda document: document["events"][0].update(principalArn="arn:aws:iam::742711170910:role/not-root"),
                "identity/principal",
            ),
            (
                "unrecorded comment",
                "score",
                lambda document: document["events"][0]["request"].update(comment="unrecorded same-run command"),
                "commands/comments differ",
            ),
            (
                "extra same-run command",
                "score",
                lambda document: document["events"].append(copy.deepcopy(document["events"][0])),
                "exactly 16 events",
            ),
            (
                "wrong archive ownership",
                "archive",
                lambda document: document["events"][0]["request"].update(startedBy="other-run"),
                "archive event",
            ),
        )
        for description, name, mutate, message in mutations:
            with self.subTest(description), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                documents = write_fixture(run_dir)
                rewrite_cloudtrail_and_digest(run_dir, documents, name, mutate)
                with self.assertRaisesRegex(EvidenceAssemblyError, message):
                    assemble_evidence(run_dir)

    def test_cloudtrail_commands_must_match_immutable_node_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_fixture(run_dir)
            path = run_dir / "evidence/warmup/node-01/ssm-command-started.json"
            command = json.loads(path.read_text(encoding="utf-8"))
            command["commandId"] = "ffffffff-ffff-4fff-8fff-ffffffffffff"
            path.write_bytes(json_bytes(command))

            with self.assertRaisesRegex(EvidenceAssemblyError, "commands/comments differ"):
                assemble_evidence(run_dir)

    def test_required_execution_artifact_failures_cannot_pass(self) -> None:
        def fail_deployment(documents):
            documents["deploymentVerification"]["passed"] = False

        def fail_correctness(documents):
            correctness = documents["correctnessSummary"]
            correctness["correctness"]["inputRecords"] = 1_001
            correctness["correctness"]["passed"] = False
            correctness["passed"] = False

        def fail_seed(documents):
            documents["seedSummary"]["stable"] = False

        def fail_warmup(documents):
            documents["warmupStageSummary"]["aggregate"]["durationSeconds"] = 179

        mutations = (
            ("deploymentVerification", fail_deployment),
            ("correctnessSummary", fail_correctness),
            ("seedSummary", fail_seed),
            ("warmupStageSummary", fail_warmup),
        )
        for artifact_name, mutate in mutations:
            with self.subTest(artifact_name), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                documents = write_fixture(run_dir)
                mutate(documents)
                write_artifact(run_dir, documents, artifact_name)

                evaluation = evaluate(assemble_evidence(run_dir), run_dir)

                self.assertEqual("failed", evaluation["verdict"])
                self.assertIn("requiredExecutionArtifactsPassed", evaluation["failedChecks"])

    def test_hard_stop_deadline_and_second_one_shot_attempt_cannot_pass(self) -> None:
        def hard_stop(run):
            run["failedStage"] = "score_archive"
            run["cleanupOnly"] = True
            run["failureDisposition"] = "hard-stop"

        def late_cleanup(run):
            cleanup = next(item for item in run["stageAttempts"] if item["stage"] == "cleanup")
            cleanup["startedAt"] = iso(161)
            cleanup["finishedAt"] = iso(162)

        def late_evaluation(run):
            run["stageAttempts"][-1]["startedAt"] = iso(181)
            run["inProgressStage"]["startedAt"] = iso(181)

        def second_deploy(run):
            attempts = run["stageAttempts"]
            duplicate = stage_attempt(9, "deploy", 115, 116)
            duplicate["attempt"] = 2
            attempts.insert(8, duplicate)
            for ordinal, attempt in enumerate(attempts, start=1):
                attempt["ordinal"] = ordinal
            run["attemptedStages"] = [item["stage"] for item in attempts]
            run["inProgressStage"]["ordinal"] = attempts[-1]["ordinal"]

        mutations = (
            ("hard stop", hard_stop, "oneShotRunnerStateValid"),
            ("cleanup after minute 160", late_cleanup, "deadlineContractMet"),
            ("evaluation after minute 180", late_evaluation, "deadlineContractMet"),
            ("second deploy", second_deploy, "oneShotRunnerStateValid"),
        )
        for description, mutate, failed_check in mutations:
            with self.subTest(description), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                documents = write_fixture(run_dir)
                mutate(documents["runState"])
                write_artifact(run_dir, documents, "runState")

                evaluation = evaluate(assemble_evidence(run_dir), run_dir)

                self.assertEqual("failed", evaluation["verdict"])
                self.assertIn(failed_check, evaluation["failedChecks"])

    def test_finalizer_rejects_inconsistent_or_cross_run_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            documents = write_fixture(run_dir)
            evaluation = evaluate(assemble_evidence(run_dir), run_dir)

            inconsistent = copy.deepcopy(evaluation)
            inconsistent["verdict"] = "failed"
            with self.assertRaises(EvidenceAssemblyError):
                finalize_run_document(documents["runState"], inconsistent)

            cross_run = copy.deepcopy(evaluation)
            cross_run["sessionId"] = OTHER_SESSION_ID
            with self.assertRaises(EvidenceAssemblyError):
                finalize_run_document(documents["runState"], cross_run)

    def test_cloudtrail_attempt_cardinality_is_an_acceptance_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            documents = write_fixture(run_dir)
            documents["observabilitySummary"]["cloudTrail"]["archiveAttempts"] = 2
            path = run_dir / ARTIFACT_CONTRACTS["observabilitySummary"].path
            path.write_text(
                json.dumps(documents["observabilitySummary"]) + "\n",
                encoding="utf-8",
            )

            evaluation = evaluate(assemble_evidence(run_dir), run_dir)

            self.assertEqual("failed", evaluation["verdict"])
            self.assertIn(
                "cloudTrailExecutionCardinalityExact", evaluation["failedChecks"]
            )

    def test_fabricated_rps_cannot_replace_comparable_processed_volume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            documents = write_fixture(run_dir)
            score = documents["scoreStageSummary"]
            score["aggregate"].update(
                {"attemptedRequests": 100, "completedRequests": 100, "http202": 100}
            )
            score_path = run_dir / ARTIFACT_CONTRACTS["scoreStageSummary"].path
            score_path.write_text(json.dumps(score) + "\n", encoding="utf-8")
            counts = documents["drainAccounting"]["counts"]
            for field in (
                "http202",
                "collectorFinalAck",
                "kinesisAccepted",
                "kclProcessed",
                "clickHouseInserted",
            ):
                counts[field] = 100
            drain_path = run_dir / ARTIFACT_CONTRACTS["drainAccounting"].path
            drain_path.write_text(
                json.dumps(documents["drainAccounting"]) + "\n", encoding="utf-8"
            )
            def lower_raw_collector_volume(raw):
                for index, value in enumerate(raw["collectorAfter"].values()):
                    value["kinesis"]["put_records"]["successes"] = 100 if index == 0 else 0
                raw["failureEvidence"]["collectorDelta"]["successes"] = 100

            rewrite_raw_observability_and_digest(
                run_dir, documents, lower_raw_collector_volume
            )

            evaluation = evaluate(assemble_evidence(run_dir), run_dir)

            self.assertEqual("failed", evaluation["verdict"])
            self.assertIn("processedVolumeAtLeast14850000", evaluation["failedChecks"])
            self.assertIn(
                "actualRpsConsistentWithCompletedVolume", evaluation["failedChecks"]
            )


if __name__ == "__main__":
    unittest.main()

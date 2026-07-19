#!/usr/bin/env python3
"""Run archive-only diagnostics on the unchanged full Phase 7 integration stack."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.exceptions import ClientError

from common import (
    EXPECTED_ACCOUNT,
    EXPECTED_OPERATOR_ARN,
    EXPECTED_REGION,
    PHASE7_COLLECTOR_COMMIT,
    expected_tags,
    file_sha256,
    image_source_closure_sha256,
    parse_utc,
    read_json,
    scoped_diagnostic_source_checks,
    tag_map,
    tags_match,
    utc_now,
    validate_identifiers,
    write_json,
)
from evidence_assembler import validate_cleanup_inventory_document
from full_stack_scoped_cost_model import (
    build_cost_model,
    canonical_sha256 as cost_canonical_sha256,
    validate_campaign_ledger,
    validate_cost_model,
)
from runner import ProcessOutcome, run_command
from runtime_stages import (
    AwsRuntime,
    FULL_SCALE_ROWS,
    RUNTIME_STACK,
    archive_evidence,
    load_bundle,
    one,
    verify_deployment,
)


IMAGE_STACK = "LoopAdPerfPhase7IntegrationImageStack"
ATTEMPT_TYPE = "aws-full-stack-scoped-diagnostic"
STAGE_PLAN = ("deploy", "verify", "seed", "archive", "collect", "cleanup", "inventory")
ZERO_ATTEMPT_STAGES = ("correctness", "replacement", "warmup", "score", "source-drop")
NON_CLEANUP_STAGES = {"deploy", "verify", "seed", "archive", "collect"}
STAGE_TIMEOUTS = {
    "deploy": 20 * 60,
    "verify": 10 * 60,
    "seed": 15 * 60,
    "archive": 35 * 60,
    "collect": 10 * 60,
    "cleanup": 20 * 60,
    "inventory": 5 * 60,
}
FORBIDDEN_CREDENTIAL_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def reject_credential_environment() -> None:
    if any(os.environ.get(name) for name in FORBIDDEN_CREDENTIAL_ENV):
        raise RuntimeError("scoped diagnostic refuses static AWS credential environment variables")


def assert_current_identity() -> dict[str, Any]:
    reject_credential_environment()
    session = boto3.Session(region_name=EXPECTED_REGION)
    sdk = session.client("sts", region_name=EXPECTED_REGION).get_caller_identity()
    completed = subprocess.run(
        [
            "aws",
            "sts",
            "get-caller-identity",
            "--region",
            EXPECTED_REGION,
            "--output",
            "json",
            "--no-cli-pager",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    cli = json.loads(completed.stdout)
    expected = {"account": EXPECTED_ACCOUNT, "arn": EXPECTED_OPERATOR_ARN}
    observed_sdk = {"account": sdk.get("Account"), "arn": sdk.get("Arn")}
    observed_cli = {"account": cli.get("Account"), "arn": cli.get("Arn")}
    if observed_sdk != expected or observed_cli != expected:
        raise RuntimeError("deploy requires matching exact CLI and locked boto3 identity")
    return {
        "verifiedAt": utc_now(),
        "region": EXPECTED_REGION,
        "sdk": observed_sdk,
        "cli": observed_cli,
        "staticCredentialEnvironmentPresent": False,
    }


def archive_task_identity(run_id: str, session_id: str) -> dict[str, str]:
    validate_identifiers(run_id, session_id)
    stamp = re.match(r"^run_([0-9]{8})_([0-9]{6})_", run_id)
    if stamp is None:
        raise ValueError("invalid Phase 7 run ID")
    digest = hashlib.sha256(f"phase7-archive-v1\0{run_id}".encode()).hexdigest()
    return {
        "clientToken": f"p7a-{digest[:60]}",
        "startedBy": f"phase7-archive-{stamp.group(1)}{stamp.group(2)}",
    }


def cdk_context(
    run_id: str,
    session_id: str,
    preflight: dict[str, Any],
    image_manifest: dict[str, Any],
) -> list[str]:
    images = {str(item["role"]): item for item in image_manifest["images"]}
    snapshot = preflight["snapshot"]
    values = {
        "environment": "perf-phase7-integration",
        "phase7RunId": run_id,
        "phase7SessionId": session_id,
        "phase7CollectorImageDigest": images["collector"]["digest"],
        "phase7ConsumerImageDigest": images["consumer"]["digest"],
        "phase7ArchiveImageDigest": images["archive"]["digest"],
        "phase7X86EcsAmiId": snapshot["amis"]["x86"]["imageId"],
        "phase7ArmEcsAmiId": snapshot["amis"]["arm"]["imageId"],
        "phase7ProtocolCertificateArn": snapshot["certificate"]["arn"],
        "phase7ProtocolDnsName": snapshot["certificate"]["domainName"],
    }
    return [item for key, value in values.items() for item in ("-c", f"{key}={value}")]


def validate_inputs(args: argparse.Namespace) -> dict[str, Any]:
    reject_credential_environment()
    validate_identifiers(args.run_id, args.session_id)
    expected_ledger = (
        args.infra_root
        / "performance-tests/phase7_2-stabilization/attempt-ledger.json"
    ).resolve()
    if args.attempt_ledger.resolve() != expected_ledger:
        raise RuntimeError("scoped diagnostic requires the exact campaign ledger path")
    if args.runtime_dir.exists():
        raise FileExistsError("a scoped diagnostic runtime directory is immutable and may not be reused")
    if not args.readiness_dir.is_dir() or not args.ca_certificate.is_file():
        raise RuntimeError("readiness directory and public CA bundle must exist before initialization")
    source_checks, source = scoped_diagnostic_source_checks(
        args.infra_root, args.scoped_diagnostic_source
    )
    if not all(check.passed for check in source_checks):
        raise RuntimeError("scoped diagnostic source seal failed revalidation")
    preflight = read_json(args.prepared_preflight)
    images = read_json(args.image_manifest)
    cost = read_json(args.cost_model)
    prices = read_json(args.prices)
    ledger = read_json(args.attempt_ledger)
    if any(
        item.get("runId") == args.run_id or item.get("sessionId") == args.session_id
        for item in ledger.get("attempts", [])
        if isinstance(item, dict)
    ):
        raise RuntimeError("scoped diagnostic identity was already used by the campaign")
    for name, document in (("preflight", preflight), ("image manifest", images)):
        if document.get("runId") != args.run_id or document.get("sessionId") != args.session_id:
            raise RuntimeError(f"{name} belongs to another identity")
    source_tree = source.get("implementationTreeSha256")
    if (
        preflight.get("passed") is not True
        or preflight.get("imageState") != "prepared"
        or preflight.get("attemptType") != ATTEMPT_TYPE
        or preflight.get("promotionEligible") is not False
        or preflight.get("sourceAuthorization", {}).get("implementationTreeSha256") != source_tree
    ):
        raise RuntimeError("prepared full-stack scoped preflight is not exact")
    cost_authorization = preflight.get("costAuthorization", {})
    if (
        not validate_cost_model(
            prices,
            ledger,
            cost,
            expected_run_id=args.run_id,
            expected_session_id=args.session_id,
        )
        or cost_authorization.get("campaignLedgerSha256")
        != cost.get("campaignLedgerSha256")
        or cost_authorization.get("priceDocumentSha256")
        != cost.get("priceDocumentSha256")
        or cost_authorization.get("phase8PromotionPolicySha256")
        != cost.get("phase8PromotionPolicySha256")
        or cost.get("priceDocumentSha256") != cost_canonical_sha256(prices)
    ):
        raise RuntimeError("scoped diagnostic cost authorization is not exact")
    if (
        images.get("attemptType") != ATTEMPT_TYPE
        or images.get("promotionEligible") is not False
        or images.get("runtimeDeployed") is not False
        or images.get("implementationTreeSha256") != source_tree
        or images.get("collectorCommit") != PHASE7_COLLECTOR_COMMIT
    ):
        raise RuntimeError("scoped image manifest is not exact")
    active_attempt = ledger.get("activeAttempt", {})
    paid_started_at = parse_utc(str(images.get("paidStartedAt")))
    prepared_at = parse_utc(str(images.get("preparedAt")))
    now = datetime.now(UTC)
    expected_marker = args.image_manifest.with_name(
        f"{args.image_manifest.stem}-paid-start.json"
    ).resolve()
    marker_path = Path(str(images.get("paidStartEvidencePath", ""))).resolve()
    if (
        active_attempt.get("runId") != args.run_id
        or active_attempt.get("sessionId") != args.session_id
        or active_attempt.get("state") != "images-prepared"
        or active_attempt.get("paidStartedAt") != images.get("paidStartedAt")
        or ledger.get("budget", {}).get("currentAttemptPaidStartAt")
        != images.get("paidStartedAt")
        or active_attempt.get("imageManifest", {}).get("sha256")
        != file_sha256(args.image_manifest)
        or marker_path != expected_marker
        or not marker_path.is_file()
        or images.get("paidStartEvidenceSha256") != file_sha256(marker_path)
        or not (paid_started_at <= prepared_at <= now)
    ):
        raise RuntimeError("durable image paid-start and prepared state is not exact")
    marker = read_json(marker_path)
    if (
        marker.get("runId") != args.run_id
        or marker.get("sessionId") != args.session_id
        or marker.get("paidStartedAt") != images.get("paidStartedAt")
        or active_attempt.get("paidStartEvidence", {}).get("sha256")
        != file_sha256(marker_path)
    ):
        raise RuntimeError("image paid-start marker does not match the active attempt")
    manifest_images = images.get("images", [])
    image_by_role = {
        str(item.get("role")): item
        for item in manifest_images
        if isinstance(item, dict)
    }
    if (
        not isinstance(manifest_images, list)
        or len(manifest_images) != 3
        or len(image_by_role) != 3
        or set(image_by_role) != {"collector", "consumer", "archive"}
    ):
        raise RuntimeError("full-stack scoped diagnostic requires three exact images")
    for role, item in image_by_role.items():
        expected_platform = "linux/amd64" if role == "collector" else "linux/arm64"
        if (
            item.get("architecture") != expected_platform
            or item.get("repository") != f"loop-ad/perf-phase7/{args.run_id}/{role}"
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("digest", ""))) is None
            or re.fullmatch(
                r"[0-9a-f]{64}", str(item.get("sourceClosureSha256", ""))
            ) is None
            or item.get("sourceClosureSha256")
            != image_source_closure_sha256(role, str(source_tree))
        ):
            raise RuntimeError(f"scoped image contract is invalid for {role}")
    image_authorization = preflight.get("imageAuthorization", {})
    if (
        image_authorization.get("imageManifestSha256")
        != cost_canonical_sha256(images)
        or image_authorization.get("digests")
        != {role: image_by_role[role]["digest"] for role in sorted(image_by_role)}
    ):
        raise RuntimeError("prepared preflight is not bound to the exact image manifest")
    if (
        cost.get("passed") is not True
        or cost.get("attemptType") != ATTEMPT_TYPE
        or cost.get("promotionEligible") is not False
        or int(cost.get("stageDeadlineMinutes", {}).get("cleanupStart", -1)) != 45
        or int(cost.get("stageDeadlineMinutes", {}).get("hard", -1)) != 120
    ):
        raise RuntimeError("scoped diagnostic cost model is not exact")
    price_age = (datetime.now(UTC) - parse_utc(str(prices.get("asOf")))).total_seconds()
    if price_age < 0 or price_age > 3600:
        raise RuntimeError("public price evidence expired before scoped deployment")
    return {
        "source": source,
        "preflight": preflight,
        "images": images,
        "cost": cost,
        "prices": prices,
        "ledger": ledger,
    }


def initialize(args: argparse.Namespace, inputs: dict[str, Any]) -> dict[str, Any]:
    prepared_at = parse_utc(str(inputs["images"]["paidStartedAt"]))
    cleanup_start_minutes = int(
        inputs["cost"]["stageDeadlineMinutes"]["cleanupStart"]
    )
    hard_minutes = int(inputs["cost"]["stageDeadlineMinutes"]["hard"])
    now = datetime.now(UTC)
    if now >= prepared_at + timedelta(minutes=cleanup_start_minutes):
        raise RuntimeError("scoped attempt cannot start inside its cleanup-only window")
    args.runtime_dir.mkdir(parents=True)
    for name, source in (
        ("scoped-diagnostic-source.json", args.scoped_diagnostic_source),
        ("preflight.json", args.prepared_preflight),
        ("image-manifest.json", args.image_manifest),
        ("cost-model.json", args.cost_model),
        ("prices.json", args.prices),
        ("attempt-ledger.json", args.attempt_ledger),
    ):
        write_json(args.runtime_dir / "inputs" / name, read_json(source))

    cdk = args.infra_root / "node_modules/.bin/cdk"
    if not cdk.is_file():
        raise FileNotFoundError("checked workspace CDK executable is missing")
    deploy = [
        str(cdk),
        "deploy",
        RUNTIME_STACK,
        "--exclusively",
        "--require-approval",
        "never",
        "--outputs-file",
        str(args.runtime_dir / "cdk-outputs.json"),
        *cdk_context(args.run_id, args.session_id, inputs["preflight"], inputs["images"]),
    ]
    command_set = {
        "schemaVersion": 1,
        "runId": args.run_id,
        "sessionId": args.session_id,
        "attemptType": ATTEMPT_TYPE,
        "promotionEligible": False,
        "stackDefinitions": [IMAGE_STACK, RUNTIME_STACK],
        "topologyBaseline": "Attempt 17",
        "stagePlan": list(STAGE_PLAN),
        "zeroAttemptStages": list(ZERO_ATTEMPT_STAGES),
        "stageMaximumAttempts": {stage: 1 for stage in STAGE_PLAN},
        "deployment": {"argv": deploy, "cwd": str(args.infra_root)},
        "archiveTask": {
            **archive_task_identity(args.run_id, args.session_id),
            "retainSourceAfterCommit": True,
        },
        "sourceTreeSha256": inputs["source"]["implementationTreeSha256"],
        "activeEpochPriorUpperBoundUsd": inputs["cost"]["activeEpochPriorUpperBoundUsd"],
        "campaignLedgerSha256": inputs["cost"]["campaignLedgerSha256"],
        "imageDigests": {
            item["role"]: item["digest"] for item in inputs["images"]["images"]
        },
    }
    command_set["sha256"] = canonical_sha256(command_set)
    write_json(args.runtime_dir / "inputs" / "command-set.json", command_set)
    document = {
        "schemaVersion": 1,
        "runId": args.run_id,
        "sessionId": args.session_id,
        "attemptType": ATTEMPT_TYPE,
        "promotionEligible": False,
        "phase": "7-2",
        "phase5": "skipped",
        "status": "initialized",
        "verdict": None,
        "initializedAt": utc_now(),
        "paidStartedAt": inputs["images"]["paidStartedAt"],
        "cleanupStartDeadline": (
            prepared_at + timedelta(minutes=cleanup_start_minutes)
        ).isoformat().replace("+00:00", "Z"),
        "hardDeadline": (
            prepared_at + timedelta(minutes=hard_minutes)
        ).isoformat().replace("+00:00", "Z"),
        "commandSetSha256": command_set["sha256"],
        "completedStages": [],
        "stageAttempts": [],
        "failedStage": None,
        "failure": None,
        "sourceDropAuthorized": False,
        "zeroAttemptStages": list(ZERO_ATTEMPT_STAGES),
    }
    write_json(args.runtime_dir / "run.json", document)
    (args.runtime_dir / "commands.md").write_text(
        "# Full-stack scoped archive diagnostic commands\n\n"
        f"- Command-set SHA-256: `{command_set['sha256']}`\n"
        f"- Stack definitions: `{IMAGE_STACK}`, `{RUNTIME_STACK}`\n"
        "- Plan: deploy, verify, 15M seed, retain-source archive, collect, cleanup, inventory.\n"
        "- Correctness, replacement, warmup, score and source DROP: zero attempts.\n",
        encoding="utf-8",
    )
    (args.runtime_dir / "infra.md").write_text(
        "# Full-stack scoped diagnostic infrastructure\n\n"
        "Attempt 17 integration stack topology is intentionally deployed unchanged. No dedicated "
        "diagnostic stack or reduced graph is used.\n",
        encoding="utf-8",
    )
    (args.runtime_dir / "failures.md").write_text(
        "# Full-stack scoped diagnostic failures\n\n",
        encoding="utf-8",
    )
    ledger = read_json(args.attempt_ledger)
    active = dict(ledger.get("activeAttempt", {}))
    if (
        active.get("runId") != args.run_id
        or active.get("sessionId") != args.session_id
        or active.get("state") != "images-prepared"
        or active.get("imageManifest", {}).get("sha256")
        != file_sha256(args.image_manifest)
    ):
        raise RuntimeError("active attempt changed before runtime command sealing")
    active["state"] = "runtime-sealed"
    active["runtimeSealedAt"] = utc_now()
    active["runtimeDirectory"] = str(args.runtime_dir.relative_to(args.infra_root))
    active["preparedPreflightSha256"] = file_sha256(args.prepared_preflight)
    active["commandSetSha256"] = command_set["sha256"]
    active["runtimeDeployAttempts"] = 0
    ledger["activeAttempt"] = active
    ledger["updatedAt"] = active["runtimeSealedAt"]
    write_json(args.attempt_ledger, ledger)
    return command_set


def record_runtime_deploy_start(args: argparse.Namespace) -> None:
    ledger = read_json(args.attempt_ledger)
    active = dict(ledger.get("activeAttempt", {}))
    if (
        active.get("runId") != args.run_id
        or active.get("sessionId") != args.session_id
        or active.get("state") != "runtime-sealed"
        or active.get("runtimeDeployAttempts") != 0
    ):
        raise RuntimeError("runtime deploy is not authorized by the durable attempt seal")
    active["state"] = "runtime-deploy-started"
    active["runtimeDeployAttempts"] = 1
    active["runtimeDeployStartedAt"] = utc_now()
    ledger["activeAttempt"] = active
    ledger["updatedAt"] = active["runtimeDeployStartedAt"]
    write_json(args.attempt_ledger, ledger)


def append_stage(
    run_dir: Path,
    stage: str,
    started: str,
    finished: str,
    passed: bool,
    result_path: str | None,
    error: BaseException | None,
) -> None:
    run = read_json(run_dir / "run.json")
    if any(item.get("stage") == stage for item in run.get("stageAttempts", [])):
        raise RuntimeError(f"stage may run only once: {stage}")
    attempt = {
        "stage": stage,
        "attempt": 1,
        "startedAt": started,
        "finishedAt": finished,
        "passed": passed,
        "resultPath": result_path,
        "errorType": type(error).__name__ if error else None,
        "error": str(error)[:1000] if error else None,
    }
    run.setdefault("stageAttempts", []).append(attempt)
    if passed:
        run.setdefault("completedStages", []).append(stage)
    else:
        run["failedStage"] = run.get("failedStage") or stage
        run["failure"] = {"errorType": attempt["errorType"], "error": attempt["error"]}
        run["status"] = "cleanup-required"
    write_json(run_dir / "evidence" / "control" / f"{stage}.json", attempt)
    write_json(run_dir / "run.json", run)


class StageDeadlineExceeded(TimeoutError):
    pass


def stage_time_budget(args: argparse.Namespace, stage: str, requested: int) -> int:
    run = read_json(args.runtime_dir / "run.json")
    deadline_field = (
        "cleanupStartDeadline" if stage in NON_CLEANUP_STAGES else "hardDeadline"
    )
    remaining = (
        parse_utc(str(run[deadline_field])) - datetime.now(UTC)
    ).total_seconds()
    if stage == "cleanup":
        remaining -= STAGE_TIMEOUTS["inventory"]
    if remaining <= 0:
        raise StageDeadlineExceeded(
            f"{stage} cannot start after its immutable {deadline_field}"
        )
    return max(1, min(requested, int(remaining)))


def call_with_timeout(function: Callable[[], dict[str, Any]], timeout: int) -> dict[str, Any]:
    if not hasattr(signal, "SIGALRM"):
        raise RuntimeError("scoped callable deadline enforcement requires SIGALRM")

    def deadline_handler(_signum: int, _frame: Any) -> None:
        raise StageDeadlineExceeded(
            "callable stage reached its immutable execution deadline"
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, deadline_handler)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, float(timeout))
    try:
        return function()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def execute_callable_stage(
    args: argparse.Namespace,
    stage: str,
    function: Callable[[], dict[str, Any]],
    output_name: str,
) -> dict[str, Any]:
    started = utc_now()
    try:
        timeout = stage_time_budget(args, stage, STAGE_TIMEOUTS[stage])
        result = call_with_timeout(function, timeout)
        write_json(args.runtime_dir / output_name, result)
        if result.get("passed") is False:
            raise RuntimeError(f"{stage} returned passed=false")
    except BaseException as error:
        append_stage(args.runtime_dir, stage, started, utc_now(), False, None, error)
        raise
    append_stage(args.runtime_dir, stage, started, utc_now(), True, output_name, None)
    return result


def execute_command_stage(
    args: argparse.Namespace,
    stage: str,
    argv: list[str],
    timeout: int,
    output_name: str | None = None,
) -> ProcessOutcome:
    started = utc_now()
    environment = os.environ.copy()
    environment.update({
        "CDK_DEFAULT_ACCOUNT": EXPECTED_ACCOUNT,
        "LOOP_AD_REGION": EXPECTED_REGION,
    })
    try:
        effective_timeout = stage_time_budget(args, stage, timeout)
        outcome = run_command(
            argv, str(args.infra_root), environment, effective_timeout
        )
        control = args.runtime_dir / "evidence" / "control"
        control.mkdir(parents=True, exist_ok=True)
        (control / f"{stage}.stdout.log").write_text(
            outcome.stdout, encoding="utf-8"
        )
        (control / f"{stage}.stderr.log").write_text(
            outcome.stderr, encoding="utf-8"
        )
        if outcome.returncode != 0:
            raise RuntimeError(
                f"{stage} exited {outcome.returncode}"
                f"{' after timeout' if outcome.timed_out else ''}"
            )
    except BaseException as error:
        append_stage(args.runtime_dir, stage, started, utc_now(), False, None, error)
        raise
    append_stage(
        args.runtime_dir, stage, started, utc_now(), True, output_name, None
    )
    return outcome


def seed(aws: AwsRuntime) -> dict[str, Any]:
    from runtime_stages import seed_partition

    return seed_partition(aws)


def list_exact_archive_tasks(aws: AwsRuntime, started_by: str, desired_status: str) -> list[str]:
    if desired_status not in {"RUNNING", "STOPPED"}:
        raise ValueError("archive task status must be RUNNING or STOPPED")
    ecs = aws.client("ecs")
    cluster = aws.bundle.outputs["ArchiveClusterName"]
    task_arns = sorted(
        arn
        for page in ecs.get_paginator("list_tasks").paginate(
            cluster=cluster,
            desiredStatus=desired_status,
        )
        for arn in page.get("taskArns", [])
    )
    exact: list[str] = []
    for offset in range(0, len(task_arns), 100):
        response = ecs.describe_tasks(
            cluster=cluster,
            tasks=task_arns[offset:offset + 100],
        )
        if response.get("failures"):
            raise RuntimeError("archive task inventory could not be described exactly")
        for task in response.get("tasks", []):
            if (
                task.get("startedBy") == started_by
                and task.get("lastStatus") == desired_status
            ):
                exact.append(str(task["taskArn"]))
    return sorted(exact)


def run_archive(aws: AwsRuntime) -> dict[str, Any]:
    aws.assert_identity()
    seed_summary = read_json(aws.bundle.run_dir / "seed-summary.json")
    identity = archive_task_identity(aws.bundle.run_id, aws.bundle.session_id)
    ecs = aws.client("ecs")
    before_running = list_exact_archive_tasks(aws, identity["startedBy"], "RUNNING")
    before_stopped = list_exact_archive_tasks(aws, identity["startedBy"], "STOPPED")
    if before_running or before_stopped:
        raise RuntimeError("archive task identity was already used")
    clickhouse_before = aws.service_snapshot("ClickHouse")
    stopped_service_before = sorted(
        arn
        for page in ecs.get_paginator("list_tasks").paginate(
            cluster=aws.bundle.outputs["ClickHouseClusterName"],
            serviceName=aws.bundle.outputs["ClickHouseServiceName"],
            desiredStatus="STOPPED",
        )
        for arn in page.get("taskArns", [])
    )
    response = ecs.run_task(
        cluster=aws.bundle.outputs["ArchiveClusterName"],
        taskDefinition=aws.bundle.outputs["ArchiveTaskDefinitionArn"],
        count=1,
        capacityProviderStrategy=[{
            "capacityProvider": aws.bundle.outputs["ArchiveCapacityProviderName"],
            "weight": 1,
            "base": 0,
        }],
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": aws.bundle.outputs["ArchiveSubnetIds"].split(","),
            "securityGroups": [aws.bundle.outputs["ArchiveSecurityGroupId"]],
            "assignPublicIp": "DISABLED",
        }},
        overrides={"containerOverrides": [{
            "name": "archive",
            "environment": [
                {"name": "ARCHIVE_PARTITION", "value": str(seed_summary["partition"])},
                {"name": "ARCHIVE_TODAY", "value": str(seed_summary["today"])},
                {"name": "ARCHIVE_RETAIN_SOURCE_AFTER_COMMIT", "value": "true"},
            ],
        }]},
        clientToken=identity["clientToken"],
        startedBy=identity["startedBy"],
        tags=[
            {"key": key, "value": value}
            for key, value in expected_tags(aws.bundle.run_id, aws.bundle.session_id).items()
        ],
        enableECSManagedTags=False,
    )
    failures = response.get("failures", [])
    tasks = response.get("tasks", [])
    if failures or len(tasks) != 1:
        raise RuntimeError(f"archive RunTask did not return exactly one task: {failures}")
    task_arn = str(tasks[0]["taskArn"])
    ecs.get_waiter("tasks_stopped").wait(
        cluster=aws.bundle.outputs["ArchiveClusterName"],
        tasks=[task_arn],
        WaiterConfig={"Delay": 15, "MaxAttempts": 140},
    )
    described = ecs.describe_tasks(
        cluster=aws.bundle.outputs["ArchiveClusterName"],
        tasks=[task_arn],
        include=["TAGS"],
    )
    if described.get("failures") or len(described.get("tasks", [])) != 1:
        raise RuntimeError("archive task description is not exact")
    task = described["tasks"][0]
    container = next(
        (item for item in task.get("containers", []) if item.get("name") == "archive"),
        None,
    )
    if (
        task.get("taskArn") != task_arn
        or task.get("startedBy") != identity["startedBy"]
        or task.get("taskDefinitionArn") != aws.bundle.outputs["ArchiveTaskDefinitionArn"]
        or not tags_match(tag_map(task.get("tags", [])), aws.bundle.run_id, aws.bundle.session_id)
        or container is None
        or container.get("exitCode") != 0
    ):
        raise RuntimeError("archive task identity, ownership or exit result failed")
    stopped = list_exact_archive_tasks(aws, identity["startedBy"], "STOPPED")
    if stopped != [task_arn]:
        raise RuntimeError("archive task stopped inventory is not exactly one")

    archive = archive_evidence(aws, retain_source_after_commit=True, clickhouse_timeout=900)
    aws.clickhouse("SYSTEM FLUSH LOGS", select=False, timeout=60)
    query_log = one(aws.clickhouse(
        "SELECT "
        "countIf(type IN ('ExceptionBeforeStart','ExceptionWhileProcessing') AND exception_code=241) AS code241Exceptions, "
        "countIf(query_kind='Alter' AND positionCaseInsensitive(query,'DROP PARTITION')>0) AS sourceDropQueries, "
        "maxIf(memory_usage, type='QueryFinish') AS peakQueryMemoryBytes "
        "FROM system.query_log WHERE event_time >= now() - INTERVAL 2 HOUR",
        timeout=120,
    ))
    partition = str(seed_summary["partition"])
    post_fingerprint = one(aws.clickhouse(
        "SELECT count() AS rows, uniqExact(event_id) AS uniqueEvents, "
        "toString(sum(cityHash64(project_id,event_id,toString(event_time),properties_json))) AS checksum "
        f"FROM loopad.events FINAL WHERE event_date=toDate('{partition}')",
        timeout=900,
    ))
    clickhouse_after = aws.wait_service("ClickHouse", timeout=300)
    stopped_service_after = sorted(
        arn
        for page in ecs.get_paginator("list_tasks").paginate(
            cluster=aws.bundle.outputs["ClickHouseClusterName"],
            serviceName=aws.bundle.outputs["ClickHouseServiceName"],
            desiredStatus="STOPPED",
        )
        for arn in page.get("taskArns", [])
    )
    checks = {
        "archiveTaskExitZero": container.get("exitCode") == 0,
        "retainSourceMode": archive.get("diagnosticSourceRetention") is True
        and archive.get("dropExecuted") is False,
        "rowsExact": archive.get("rows") == FULL_SCALE_ROWS
        and archive.get("sourceRowsAfterArchive") == FULL_SCALE_ROWS,
        "threePartsExact": archive.get("objects") == 3
        and archive.get("objectRows") == [5_000_000, 5_000_000, 5_000_000],
        "preDropEquivalent": archive.get("preDropSourceMinusArchive") == 0
        and archive.get("preDropArchiveMinusSource") == 0,
        "committedEquivalent": archive.get("committedSourceMinusArchive") == 0
        and archive.get("committedArchiveMinusSource") == 0,
        "commitRereadImmutable": archive.get("committedReRead") is True,
        "sourceFingerprintRetained": post_fingerprint == seed_summary["fingerprintSamples"][-1],
        "code241Zero": int(query_log["code241Exceptions"]) == 0,
        "sourceDropQueryZero": int(query_log["sourceDropQueries"]) == 0,
        "clickHouseTaskUnchanged": {
            item["taskArn"] for item in clickhouse_before["tasks"]
        } == {item["taskArn"] for item in clickhouse_after["tasks"]},
        "clickHouseNoNewStoppedServiceTask": stopped_service_after == stopped_service_before,
    }
    result = {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "attemptType": ATTEMPT_TYPE,
        "promotionEligible": False,
        "validatedAt": utc_now(),
        "task": {
            "taskArn": task_arn,
            "startedBy": task.get("startedBy"),
            "taskDefinitionArn": task.get("taskDefinitionArn"),
            "createdAt": iso(task.get("createdAt")),
            "startedAt": iso(task.get("startedAt")),
            "stoppedAt": iso(task.get("stoppedAt")),
            "stopCode": task.get("stopCode"),
            "stoppedReason": task.get("stoppedReason"),
            "exitCode": container.get("exitCode"),
            "reason": container.get("reason"),
            "tags": tag_map(task.get("tags", [])),
        },
        "archive": archive,
        "postArchiveFingerprint": post_fingerprint,
        "queryLog": {key: int(value) for key, value in query_log.items()},
        "clickHouseBefore": clickhouse_before,
        "clickHouseAfter": clickhouse_after,
        "stoppedServiceTasksBefore": stopped_service_before,
        "stoppedServiceTasksAfter": stopped_service_after,
        "checks": checks,
        "passed": all(checks.values()),
    }
    write_json(aws.bundle.run_dir / "archive-validation.json", result)
    if not result["passed"]:
        raise RuntimeError("full-stack scoped archive acceptance failed")
    return result


def iso(value: Any) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def json_safe(value: Any) -> Any:
    """Normalize boto3 response datetimes before immutable JSON persistence."""
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def collect_evidence(aws: AwsRuntime, paid_start: datetime) -> dict[str, Any]:
    aws.assert_identity()
    prefix = f"/loopad/perf/phase7/{aws.bundle.run_id}/"
    logs = aws.client("logs")
    groups = sorted(
        item["logGroupName"]
        for page in logs.get_paginator("describe_log_groups").paginate(
            logGroupNamePrefix=prefix
        )
        for item in page.get("logGroups", [])
        if str(item.get("logGroupName", "")).startswith(prefix)
    )
    log_evidence = []
    log_end_time_ms = int(datetime.now(UTC).timestamp() * 1000)
    for group in groups:
        streams = [
            item
            for page in logs.get_paginator("describe_log_streams").paginate(
                logGroupName=group,
                orderBy="LastEventTime",
                descending=True,
            )
            for item in page.get("logStreams", [])
        ]
        events = []
        for stream in streams:
            next_token = None
            while True:
                request: dict[str, Any] = {
                    "logGroupName": group,
                    "logStreamName": stream["logStreamName"],
                    "startFromHead": True,
                    "endTime": log_end_time_ms,
                }
                if next_token:
                    request["nextToken"] = next_token
                page = logs.get_log_events(**request)
                events.extend(page.get("events", []))
                observed_token = page.get("nextForwardToken")
                if not observed_token or observed_token == next_token:
                    break
                next_token = observed_token
        log_evidence.append({
            "logGroup": group,
            "streamCount": len(streams),
            "eventCount": len(events),
            "events": events,
        })
    trail = aws.client("cloudtrail")
    selected = []
    next_token = None
    while True:
        parameters: dict[str, Any] = {
            "StartTime": paid_start - timedelta(minutes=5),
            "EndTime": datetime.now(UTC) + timedelta(minutes=1),
            "MaxResults": 50,
        }
        if next_token:
            parameters["NextToken"] = next_token
        response = trail.lookup_events(**parameters)
        for event in response.get("Events", []):
            raw = str(event.get("CloudTrailEvent", ""))
            if aws.bundle.run_id in raw or aws.bundle.session_id in raw:
                selected.append({
                    "eventId": event.get("EventId"),
                    "eventName": event.get("EventName"),
                    "eventTime": iso(event.get("EventTime")),
                    "eventSource": event.get("EventSource"),
                    "readOnly": event.get("ReadOnly"),
                    "resources": event.get("Resources", []),
                })
        next_token = response.get("NextToken")
        if not next_token:
            break
    return {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "collectedAt": utc_now(),
        "cloudWatch": {"logGroupCount": len(log_evidence), "logGroups": log_evidence},
        "cloudTrail": {"eventCount": len(selected), "events": selected, "rawEventsPersisted": False},
        "passed": bool(groups),
    }


def collect_failure_evidence(
    aws: AwsRuntime, paid_start: datetime
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": aws.bundle.run_id,
        "sessionId": aws.bundle.session_id,
        "collectedAt": utc_now(),
        "beforeCleanup": True,
        "errors": {},
    }
    try:
        evidence["logsAndTrail"] = collect_evidence(aws, paid_start)
    except Exception as error:
        evidence["errors"]["logsAndTrail"] = str(error)[:1000]

    try:
        s3 = aws.client("s3")
        bucket = aws.bundle.outputs["ArchiveBucketName"]
        objects = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for item in page.get("Contents", []):
                record = {
                    "key": item.get("Key"),
                    "size": item.get("Size"),
                    "etag": item.get("ETag"),
                    "lastModified": iso(item.get("LastModified")),
                }
                key = str(item.get("Key", ""))
                size = int(item.get("Size", 0))
                if key.endswith(".json") and size <= 1_048_576:
                    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                    record["sha256"] = hashlib.sha256(body).hexdigest()
                    try:
                        record["document"] = json.loads(body)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        record["document"] = None
                objects.append(record)
        evidence["archiveBucket"] = {"bucket": bucket, "objects": objects}
    except Exception as error:
        evidence["errors"]["archiveBucket"] = str(error)[:1000]

    try:
        ecs = aws.client("ecs")
        cluster = aws.bundle.outputs["ArchiveClusterName"]
        arns = sorted({
            arn
            for desired in ("RUNNING", "STOPPED")
            for page in ecs.get_paginator("list_tasks").paginate(
                cluster=cluster, desiredStatus=desired
            )
            for arn in page.get("taskArns", [])
        })
        tasks = []
        for offset in range(0, len(arns), 100):
            response = ecs.describe_tasks(
                cluster=cluster,
                tasks=arns[offset:offset + 100],
                include=["TAGS"],
            )
            if response.get("failures"):
                raise RuntimeError("archive failure task inventory is incomplete")
            tasks.extend(response.get("tasks", []))
        evidence["archiveTasks"] = json_safe(tasks)
    except Exception as error:
        evidence["errors"]["archiveTasks"] = str(error)[:1000]

    try:
        aws.clickhouse("SYSTEM FLUSH LOGS", select=False, timeout=30)
        start_text = paid_start.isoformat().replace("+00:00", "Z")
        evidence["queryLog"] = one(aws.clickhouse(
            "SELECT "
            "countIf(type IN ('ExceptionBeforeStart','ExceptionWhileProcessing') "
            "AND exception_code=241) AS code241Exceptions, "
            "countIf(query_kind='Alter' AND "
            "positionCaseInsensitive(query,'DROP PARTITION')>0) AS sourceDropQueries "
            "FROM system.query_log WHERE event_time >= "
            f"parseDateTime64BestEffort('{start_text}')",
            timeout=60,
        ))
    except Exception as error:
        evidence["errors"]["queryLog"] = str(error)[:1000]
    evidence["complete"] = not evidence["errors"]
    return evidence


def source_drop_observation(run_dir: Path) -> bool | None:
    documents = [
        run_dir / "archive-validation.json",
        run_dir / "failure-evidence.json",
    ]
    observed_zero = False
    for path in documents:
        if not path.is_file():
            continue
        document = read_json(path)
        query_log = document.get("queryLog", {})
        try:
            source_drop_queries = int(query_log.get("sourceDropQueries", -1))
        except (TypeError, ValueError):
            source_drop_queries = -1
        if source_drop_queries > 0:
            return True
        if source_drop_queries == 0:
            observed_zero = True
        archive = document.get("archive", {})
        if archive.get("dropExecuted") is True:
            return True
        for item in document.get("archiveBucket", {}).get("objects", []):
            worker = item.get("document")
            if isinstance(worker, dict) and worker.get("dropExecuted") is True:
                return True
    return False if observed_zero else None


def attempt_diagnosis(
    args: argparse.Namespace, ordinal: int
) -> tuple[Path, dict[str, Any]] | None:
    path = (
        args.infra_root
        / "performance-tests/phase7_2-stabilization"
        / f"attempt-{ordinal}-archive-memory-diagnosis.json"
    )
    if not path.is_file():
        return None
    document = read_json(path)
    if (
        document.get("attemptOrdinal") != ordinal
        or document.get("runId") != args.run_id
        or document.get("sessionId") != args.session_id
    ):
        raise RuntimeError("terminal diagnosis does not match the immutable attempt")
    return path, document


def attempt_fix_verification(
    args: argparse.Namespace, ordinal: int
) -> tuple[Path, dict[str, Any]] | None:
    path = (
        args.infra_root
        / "performance-tests/phase7_2-stabilization"
        / f"attempt-{ordinal}-fix-verification.json"
    )
    if not path.is_file():
        return None
    document = read_json(path)
    if (
        document.get("attemptOrdinal") != ordinal
        or document.get("sourceRunId") != args.run_id
        or document.get("sourceSessionId") != args.session_id
        or document.get("status") != "passed"
        or not re.fullmatch(r"[0-9a-f]{40}", str(document.get("fixCommit", "")))
    ):
        raise RuntimeError("attempt fix verification is not an exact passed record")
    return path, document


def cleanup_evidence_is_zero(args: argparse.Namespace, path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return validate_cleanup_inventory_document(
            read_json(path), args.run_id, args.session_id
        )
    except Exception:
        return False


def ensure_authoritative_cleanup_zero(args: argparse.Namespace) -> Path | None:
    standard = args.runtime_dir / "cleanup-verification.json"
    if cleanup_evidence_is_zero(args, standard):
        final_path: Path | None = standard
    else:
        final_path = None
        cleanup_script = (
            args.infra_root / "performance-tests/phase7-integration/aws/cleanup.py"
        )
        environment = os.environ.copy()
        for name in FORBIDDEN_CREDENTIAL_ENV:
            environment.pop(name, None)
        for attempt in (1, 2):
            output = (
                args.runtime_dir
                / f"cleanup-recovery-attempt-{attempt}-verification.json"
            )
            stdout = (
                args.runtime_dir
                / "evidence/control"
                / f"cleanup-recovery-attempt-{attempt}.stdout.log"
            )
            stderr = (
                args.runtime_dir
                / "evidence/control"
                / f"cleanup-recovery-attempt-{attempt}.stderr.log"
            )
            if any(path.exists() for path in (output, stdout, stderr)):
                raise RuntimeError(
                    "refusing to overwrite immutable recovery cleanup evidence"
                )
            stdout.parent.mkdir(parents=True, exist_ok=True)
            recovery_error: BaseException | None = None
            try:
                outcome = run_command(
                    [
                        sys.executable,
                        str(cleanup_script),
                        "--run-id",
                        args.run_id,
                        "--session-id",
                        args.session_id,
                        "--execute",
                        "--timeout-seconds",
                        "1080",
                        "--output",
                        str(output),
                    ],
                    str(args.infra_root),
                    environment,
                    1200,
                )
                returncode = outcome.returncode
                timed_out = outcome.timed_out
                stdout.write_text(outcome.stdout, encoding="utf-8")
                stderr.write_text(outcome.stderr, encoding="utf-8")
            except BaseException as error:
                recovery_error = error
                returncode = None
                timed_out = isinstance(error, TimeoutError)
                stdout.write_text("", encoding="utf-8")
                stderr.write_text(
                    f"{type(error).__name__}: {str(error)[:2000]}\n",
                    encoding="utf-8",
                )
            run = read_json(args.runtime_dir / "run.json")
            run.setdefault("cleanupRecoveryAttempts", []).append({
                "attempt": attempt,
                "finishedAt": utc_now(),
                "returncode": returncode,
                "timedOut": timed_out,
                "errorType": (
                    type(recovery_error).__name__ if recovery_error else None
                ),
                "error": str(recovery_error)[:1000] if recovery_error else None,
                "verificationPath": str(output.relative_to(args.runtime_dir)),
                "authoritativeInventoryZero": cleanup_evidence_is_zero(args, output),
            })
            if datetime.now(UTC) > parse_utc(str(run["hardDeadline"])):
                run["hardDeadlineBreachedDuringCleanup"] = True
            write_json(args.runtime_dir / "run.json", run)
            if cleanup_evidence_is_zero(args, output):
                final_path = output
                break
    run = read_json(args.runtime_dir / "run.json")
    run["finalCleanupVerificationPath"] = (
        str(final_path.relative_to(args.runtime_dir)) if final_path else None
    )
    write_json(args.runtime_dir / "run.json", run)
    return final_path


def write_terminal_report(args: argparse.Namespace, inputs: dict[str, Any]) -> dict[str, Any]:
    run = read_json(args.runtime_dir / "run.json")
    ledger = read_json(args.attempt_ledger)
    active = dict(ledger.get("activeAttempt", {}))
    diagnosis_record = attempt_diagnosis(args, int(active.get("ordinal", 0)))
    source_drop = source_drop_observation(args.runtime_dir)
    if (
        source_drop is None
        and diagnosis_record is not None
        and diagnosis_record[1].get("sourceDropExecuted") is False
    ):
        source_drop = False
    final_cleanup = run.get("finalCleanupVerificationPath")
    cleanup_zero = cleanup_evidence_is_zero(
        args,
        args.runtime_dir / str(final_cleanup) if final_cleanup else Path("/nonexistent"),
    )
    required = {"deploy", "verify", "seed", "archive", "collect", "cleanup", "inventory"}
    completed = set(run.get("completedStages", []))
    before_cleanup_passed = required.difference({"cleanup", "inventory"}).issubset(completed)
    if source_drop is not False and not run.get("failedStage"):
        run["failedStage"] = "source-drop-safety"
        run["failure"] = {
            "errorType": "RuntimeError",
            "error": "source DROP non-execution was not proven exactly",
        }
    verdict = (
        "passed"
        if before_cleanup_passed
        and cleanup_zero
        and source_drop is False
        and not run.get("failedStage")
        else "failed"
    )
    run["status"] = "finalized"
    run["verdict"] = verdict
    run["finalizedAt"] = utc_now()
    run["cleanupInventoryZero"] = cleanup_zero
    write_json(args.runtime_dir / "run.json", run)
    report = {
        "schemaVersion": 1,
        "runId": args.run_id,
        "sessionId": args.session_id,
        "attemptType": ATTEMPT_TYPE,
        "promotionEligible": False,
        "phase5": "skipped",
        "verdict": verdict,
        "firstFailingGate": run.get("failedStage"),
        "stageAttempts": run.get("stageAttempts", []),
        "zeroAttemptStages": list(ZERO_ATTEMPT_STAGES),
        "sourceDropExecuted": source_drop,
        "sourceDropSafetyProven": source_drop is False,
        "cleanupInventoryZero": cleanup_zero,
        "minimalSmoke": {
            "stage": "verify",
            "eventLoad": False,
            "passed": "verify" in completed,
        },
        "chargedOperationalUpperBoundUsd": inputs["cost"]["chargedOperationalUpperBoundUsd"],
        "activeEpochMaximumIncludingCleanupUsd": inputs["cost"]["maximumIncludingCleanupUsd"],
        "projectedCampaignMaximumIncludingCleanupUsd": inputs["cost"]["projectedCampaignMaximumIncludingCleanupUsd"],
        "phase8PaidAwsExperimentOperationalUpperBoundUsd": inputs["cost"]["phase8PaidAwsExperimentOperationalUpperBoundUsd"],
    }
    write_json(args.runtime_dir / "execution-summary.json", report)
    (args.runtime_dir / "report.md").write_text(
        "# Phase 7 full-stack scoped archive diagnostic\n\n"
        f"- Verdict: `{verdict}`\n"
        f"- Attempt type: `{ATTEMPT_TYPE}`; promotion eligible: `false`\n"
        f"- First failing gate: `{run.get('failedStage')}`\n"
        f"- Cleanup authoritative zero: `{str(cleanup_zero).lower()}`\n"
        "- Minimal smoke: deployment/service/TLS health plus ClickHouse SELECT 1/schema; no event load.\n"
        "- Correctness/replacement/warmup/score/source DROP attempts: `0`\n",
        encoding="utf-8",
    )
    if (
        active.get("runId") == args.run_id
        and active.get("sessionId") == args.session_id
    ):
        active["state"] = (
            "terminal-cleaned-awaiting-ledger-entry"
            if cleanup_zero
            else "cleanup-required"
        )
        active["terminalAt"] = run["finalizedAt"]
        active["terminalVerdict"] = verdict
        active["firstFailingGate"] = run.get("failedStage")
        active["cleanupInventoryZero"] = cleanup_zero
        active["executionSummary"] = {
            "path": str(
                (args.runtime_dir / "execution-summary.json").relative_to(
                    args.infra_root
                )
            ),
            "sha256": file_sha256(args.runtime_dir / "execution-summary.json"),
        }
        ledger["activeAttempt"] = active
        ledger["updatedAt"] = run["finalizedAt"]
        write_json(args.attempt_ledger, ledger)
    return report


def money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def relative_evidence(args: argparse.Namespace, path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    return {
        "path": str(path.resolve().relative_to(args.infra_root.resolve())),
        "sha256": file_sha256(path),
    }


def write_campaign_resume(
    args: argparse.Namespace,
    *,
    entry: dict[str, Any],
    active_accrued: Decimal,
    cleanup_zero: bool,
) -> None:
    evidence_paths = entry.get("evidencePaths", {})
    evidence_relative = (
        evidence_paths.get("awsAttempt")
        or evidence_paths.get("deploymentReadiness")
        or "performance-tests/phase7_2-stabilization"
    )
    if entry["verdict"] == "passed":
        unresolved = (
            "The scoped Attempt 17 full-stack archive path passed. The attempt remains "
            "promotion-ineligible, but the user-authorized composite policy now requires a "
            "Phase 8 handoff combining immutable Attempt 17 performance evidence with this "
            "fresh minimal-smoke/archive evidence."
        )
        next_command = (
            "python3 performance-tests/phase7-integration/aws/"
            "build_phase8_composite_handoff.py --infra-root . --attempt-ledger "
            "performance-tests/phase7_2-stabilization/attempt-ledger.json "
            "--promotion-policy performance-tests/phase7_2-stabilization/"
            "phase8-composite-promotion-policy-20260719.json --output "
            "performance-tests/phase7_2-stabilization/phase8-handoff.json"
        )
    else:
        unresolved = (
            f"The first failing gate is {entry['firstFailingGate']!r}. Diagnose the immutable "
            "failure evidence, apply the smallest between-attempt fix, then regenerate the "
            "focused source seal before any new paid work."
        )
        failure_evidence = entry.get("failure", {}).get("evidencePath")
        if not failure_evidence:
            failure_evidence = (
                entry.get("terminalEvidenceHashes", {})
                .get("failure", {})
                .get("path")
            )
        next_command = (
            f"jq . {failure_evidence}"
            if failure_evidence
            else f"find {evidence_relative} -maxdepth 2 -type f | sort"
        )
    remaining_hard = Decimal("60") - active_accrued
    remaining_operational = Decimal("55") - active_accrued
    planned_charge = Decimal(str(entry["cost"].get(
        "nextRetryOperationalUpperBoundUsd",
        entry["cost"].get("plannedOperationalUpperBoundUsd", "60.000001"),
    )))
    projected_retry = active_accrued + planned_charge + Decimal("5")
    retry_authorized = (
        entry["verdict"] != "passed"
        and active_accrued < Decimal("55")
        and projected_retry <= Decimal("60")
    )
    source_drop = entry["failure"].get("sourceDropExecuted")
    source_drop_text = (
        "true" if source_drop is True
        else "false" if source_drop is False
        else "unknown/not-proven"
    )
    if entry["verdict"] == "passed":
        authorization_line = (
            "- New AWS work currently authorized: `false`; no new 50k, warmup, score or paid "
            "Phase 8 experiment is required. Generate the composite handoff and continue with "
            "unpaid Phase 8 finalization.\n"
        )
    elif retry_authorized:
        authorization_line = (
            "- New AWS work currently authorized: `false` until a fresh scoped source, price, "
            "cost, identity, global-zero, absent and prepared preflight all pass. A bounded "
            "archive-path retry may proceed after those gates without another user prompt; no "
            "50k/warmup/score stage is authorized.\n"
        )
    else:
        authorization_line = (
            "- New AWS work currently authorized: `false`; the active epoch cannot fit another "
            "scoped archive attempt plus cleanup reserve. Delayed billing must not be treated as "
            "zero.\n"
        )
    content = (
        "# Phase 7-2 stabilization resume\n\n"
        f"- Last completed action: scoped Attempt {entry['ordinal']} "
        f"`{entry['runId']}` ended `{entry['verdict']}` and was appended to the immutable "
        f"ledger at `{entry['paidWindow']['endedAt']}`.\n"
        f"- Current AWS inventory: authoritative service inventory zero and exact RunId/SessionId "
        f"Tagging API residuals zero: `{str(cleanup_zero).lower()}`.\n"
        f"- Active cost epoch accrued upper bound: `${money(active_accrued)}`. Previous epochs are "
        "excluded from admission. Hard cap: `$60.000000`; new-paid-work stop: `$55.000000`; "
        "cleanup reserve: `$5.000000`.\n"
        f"- Remaining hard-cap budget: `${money(remaining_hard)}`; remaining operational budget "
        f"before reserve: `${money(remaining_operational)}`.\n"
        f"- First failing gate: `{entry['firstFailingGate']}`. Source DROP executed: "
        f"`{source_drop_text}`. Phase 5 remains `skipped`.\n"
        f"- Unresolved hypothesis: {unresolved}\n"
        f"- Exact next safe command: `{next_command}`.\n"
        "- New 50k/warmup/score attempt: `not authorized and not required`.\n"
        f"{authorization_line}"
        f"- Ledger head SHA-256: `{entry['entrySha256']}`. Runtime evidence: "
        f"`{evidence_relative}`.\n"
    )
    resume = (
        args.infra_root
        / "performance-tests/phase7_2-stabilization/resume.md"
    )
    temporary = resume.with_suffix(".md.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(resume)


def terminal_campaign_ledger(
    ledger: dict[str, Any],
    active: dict[str, Any],
    entry: dict[str, Any],
    *,
    active_accrued: Decimal,
    next_scoped_charge: Decimal,
    updated_at: str,
) -> dict[str, Any]:
    unhashed_entry = dict(entry)
    claimed_entry_sha256 = unhashed_entry.pop("entrySha256", None)
    if (
        entry.get("ordinal") != len(ledger.get("attempts", [])) + 1
        or entry.get("ordinal") != active.get("ordinal")
        or entry.get("runId") != active.get("runId")
        or entry.get("sessionId") != active.get("sessionId")
        or entry.get("previousEntrySha256") != ledger.get("ledgerHeadSha256")
        or claimed_entry_sha256 != cost_canonical_sha256(unhashed_entry)
    ):
        raise RuntimeError("terminal ledger entry hash or active identity is invalid")
    next_ledger = dict(ledger)
    next_attempts = list(ledger["attempts"])
    next_attempts.append(entry)
    next_ledger["attempts"] = next_attempts
    next_ledger["ledgerHeadSha256"] = entry["entrySha256"]
    next_ledger["activeAttempt"] = None
    budget = dict(ledger["budget"])
    budget["activeEpochAccruedUpperBoundUsd"] = money(active_accrued)
    budget["campaignChargedUpperBoundUsd"] = money(active_accrued)
    budget["lifetimeChargedUpperBoundUsd"] = money(
        Decimal(str(budget.get("lifetimeChargedUpperBoundUsd", "0")))
        + Decimal(str(entry["cost"]["chargedUpperBoundUsd"]))
    )
    budget["remainingBeforeHardCapUsd"] = money(Decimal("60") - active_accrued)
    budget["remainingOperationalBeforeReserveUsd"] = money(
        Decimal("55") - active_accrued
    )
    next_scoped_maximum = active_accrued + next_scoped_charge + Decimal("5")
    retry_needed = entry.get("verdict") != "passed"
    budget["nextScopedAttemptOperationalUpperBoundUsd"] = (
        money(next_scoped_charge) if retry_needed else None
    )
    budget["nextScopedAttemptMaximumIncludingCleanupUsd"] = (
        money(next_scoped_maximum) if retry_needed else None
    )
    budget["newPaidWorkAuthorized"] = bool(
        retry_needed
        and active_accrued < Decimal("55")
        and next_scoped_maximum <= Decimal("60")
    )
    budget["phase8PaidExperimentOperationalUpperBoundUsd"] = "0.000000"
    budget["nextFullAttemptOperationalUpperBoundUsd"] = None
    budget["nextFullAttemptMaximumIncludingCleanupUsd"] = None
    budget["nextAttemptOperationalUpperBoundUsd"] = None
    budget["nextAttemptMaximumIncludingCleanupUsd"] = None
    budget["nextTargetedAndFullAttemptMaximumIncludingCleanupUsd"] = None
    for field in (
        "currentAttemptOrdinal",
        "currentAttemptPaidStartAt",
        "currentAttemptReservedOperationalUpperBoundUsd",
        "currentAttemptMaximumIncludingCleanupUsd",
    ):
        budget[field] = None
    next_ledger["budget"] = budget
    epochs = [dict(epoch) for epoch in ledger["budgetEpochs"]]
    matched_epochs = 0
    for epoch in epochs:
        if (
            epoch.get("epochId") == active["activeEpochId"]
            and epoch.get("status") == "active"
        ):
            matched_epochs += 1
            epoch["accruedUpperBoundUsd"] = money(active_accrued)
            ordinals = list(epoch.get("attemptOrdinals", []))
            if active["ordinal"] in ordinals:
                raise RuntimeError("active attempt ordinal was already charged")
            ordinals.append(active["ordinal"])
            epoch["attemptOrdinals"] = ordinals
    if matched_epochs != 1:
        raise RuntimeError("terminal scoped attempt active budget epoch is not exact")
    next_ledger["budgetEpochs"] = epochs
    next_ledger["updatedAt"] = updated_at
    if entry.get("verdict") == "passed":
        next_ledger["promotionCandidate"] = {
            "mode": "composite-user-authorized",
            "status": "ready-for-phase8-handoff",
            "performanceAttemptOrdinal": 17,
            "archiveAttemptOrdinal": entry["ordinal"],
            "archiveAttemptEntrySha256": entry["entrySha256"],
            "phase8PaidAwsExperiment": False,
            "historicalAttemptVerdictsRewritten": False,
        }
    elif budget["newPaidWorkAuthorized"] is False:
        next_ledger["status"] = "budget-exhausted"
    validate_campaign_ledger(
        next_ledger,
        allow_terminal_status=next_ledger.get("status") == "budget-exhausted",
    )
    return next_ledger


def finalize_cleaned_attempt_ledger(
    args: argparse.Namespace,
    inputs: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    if report.get("cleanupInventoryZero") is not True:
        raise RuntimeError("a scoped attempt cannot be appended before authoritative cleanup zero")
    ledger = read_json(args.attempt_ledger)
    active = dict(ledger.get("activeAttempt", {}))
    attempts = ledger.get("attempts", [])
    if (
        active.get("runId") != args.run_id
        or active.get("sessionId") != args.session_id
        or active.get("state") != "terminal-cleaned-awaiting-ledger-entry"
        or active.get("ordinal") != len(attempts) + 1
        or active.get("terminalVerdict") != report.get("verdict")
    ):
        raise RuntimeError("terminal scoped attempt does not match the durable campaign state")

    run = read_json(args.runtime_dir / "run.json")
    final_cleanup_relative = run.get("finalCleanupVerificationPath")
    if not isinstance(final_cleanup_relative, str) or not final_cleanup_relative:
        raise RuntimeError("terminal scoped attempt has no authoritative cleanup evidence")
    cleanup_path = args.runtime_dir / final_cleanup_relative
    cleanup = read_json(cleanup_path)
    if not validate_cleanup_inventory_document(cleanup, args.run_id, args.session_id):
        raise RuntimeError("terminal scoped attempt cleanup inventory is not zero")

    stage_attempts = run.get("stageAttempts", [])
    if not isinstance(stage_attempts, list):
        raise RuntimeError("terminal scoped attempt stage attempts are invalid")
    stage_counts = {
        stage: sum(
            1
            for item in stage_attempts
            if isinstance(item, dict) and item.get("stage") == stage
        )
        for stage in STAGE_PLAN
    }
    if any(value > 1 for value in stage_counts.values()):
        raise RuntimeError("terminal scoped attempt exceeded an immutable stage count")
    if (
        int(active.get("imagePreparationAttempts", 0)) != 1
        or int(active.get("imageStackDeployAttempts", 0)) != 1
        or active.get("stageMaximumAttempts", {}).get("imagePreparation") != 1
        or active.get("stageMaximumAttempts", {}).get("imageStackDeploy") != 1
        or int(active.get("runtimeDeployAttempts", 0)) != stage_counts["deploy"]
    ):
        raise RuntimeError("durable image/runtime attempt counts disagree with evidence")

    source = inputs["source"]
    images = inputs["images"]
    cost = inputs["cost"]
    planned_charge = Decimal(str(cost["chargedOperationalUpperBoundUsd"]))
    prior = Decimal(str(active["activeEpochPriorUpperBoundUsd"]))
    if money(prior + planned_charge) != str(cost["operationalMaximumUsd"]):
        raise RuntimeError("terminal scoped attempt charge disagrees with admission cost model")
    paid_start = parse_utc(str(active["paidStartedAt"]))
    paid_end = parse_utc(str(run["finalizedAt"]))
    if paid_end < paid_start:
        raise RuntimeError("terminal scoped attempt paid window is invalid")
    hard_deadline = parse_utc(str(run["hardDeadline"]))
    cleanup_overrun = (
        Decimal(str(cost["cleanupReserveUsd"]))
        if run.get("hardDeadlineBreachedDuringCleanup") is True
        or paid_end > hard_deadline
        else Decimal("0")
    )
    total_charge = planned_charge + cleanup_overrun
    active_accrued = prior + total_charge
    projected_scoped_retry = active_accrued + planned_charge + Decimal("5")

    failing_gate = report.get("firstFailingGate")
    failure = run.get("failure") if isinstance(run.get("failure"), dict) else {}
    raw_error = str(failure.get("error") or "")
    if isinstance(failing_gate, str):
        stderr_path = (
            args.runtime_dir / "evidence/control" / f"{failing_gate}.stderr.log"
        )
        if stderr_path.is_file():
            raw_stderr = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            if raw_stderr:
                raw_error = raw_stderr[-4000:]
    if report.get("verdict") == "passed":
        diagnosis = None
    else:
        diagnosis = (
            f"Immutable scoped attempt stopped at {failing_gate}: "
            f"{str(failure.get('error') or raw_error)[:1000]}"
        )
    diagnosis_record = attempt_diagnosis(args, int(active["ordinal"]))
    if diagnosis_record is not None and report.get("verdict") != "passed":
        diagnosis_document = diagnosis_record[1]
        if diagnosis_document.get("firstFailingGate") != failing_gate:
            raise RuntimeError("terminal diagnosis first gate does not match the runner")
        raw_error = str(diagnosis_document.get("rawAwsError") or raw_error)
        diagnosis = str(diagnosis_document.get("diagnosis") or diagnosis)

    fix_record = attempt_fix_verification(args, int(active["ordinal"]))

    evidence_candidates = {
        "run": args.runtime_dir / "run.json",
        "commandSet": args.runtime_dir / "inputs/command-set.json",
        "executionSummary": args.runtime_dir / "execution-summary.json",
        "report": args.runtime_dir / "report.md",
        "failure": args.runtime_dir / "failure.json",
        "deploymentVerification": args.runtime_dir / "deployment-verification.json",
        "seed": args.runtime_dir / "seed-summary.json",
        "archive": args.runtime_dir / "archive-validation.json",
        "metrics": args.runtime_dir / "metrics-summary.json",
        "cleanup": cleanup_path,
    }
    if diagnosis_record is not None:
        evidence_candidates["diagnosis"] = diagnosis_record[0]
    if fix_record is not None:
        evidence_candidates["fixVerification"] = fix_record[0]
    terminal_hashes = {
        name: evidence
        for name, path in evidence_candidates.items()
        if (evidence := relative_evidence(args, path)) is not None
    }
    image_by_role = {
        str(item["role"]): item
        for item in images.get("images", [])
        if isinstance(item, dict)
    }
    entry = {
        "ordinal": active["ordinal"],
        "runId": args.run_id,
        "sessionId": args.session_id,
        "attemptType": ATTEMPT_TYPE,
        "promotionEligible": False,
        "phase5": "skipped",
        "verdict": report["verdict"],
        "runnerFinalVerdict": report["verdict"],
        "firstFailingGate": failing_gate,
        "previousEntrySha256": ledger.get("ledgerHeadSha256"),
        "gitCommit": source.get("gitCommit"),
        "implementationGitTree": source.get("gitTree"),
        "implementationSourceClosureSha256": source.get(
            "implementationTreeSha256"
        ),
        "evidencePaths": {
            "awsAttempt": str(args.runtime_dir.relative_to(args.infra_root)),
            "deploymentReadiness": str(
                args.readiness_dir.relative_to(args.infra_root)
            ),
            "scopedSource": str(
                args.scoped_diagnostic_source.relative_to(args.infra_root)
            ),
            "localHandoff": None,
        },
        "imageSourceHashes": {
            role: item.get("sourceClosureSha256")
            for role, item in sorted(image_by_role.items())
        },
        "imageDigests": {
            role: item.get("digest")
            for role, item in sorted(image_by_role.items())
        },
        "sealedCommandSetSha256": run.get("commandSetSha256"),
        "stageAttemptCounts": {
            "imagePreparation": int(active.get("imagePreparationAttempts", 0)),
            "imageStackDeploy": int(active.get("imageStackDeployAttempts", 0)),
            "runtimeDeploy": stage_counts["deploy"],
            "verify": stage_counts["verify"],
            "seed15M": stage_counts["seed"],
            "archive": stage_counts["archive"],
            "collect": stage_counts["collect"],
            "cleanup": stage_counts["cleanup"],
            "inventory": stage_counts["inventory"],
            **{stage: 0 for stage in ZERO_ATTEMPT_STAGES},
        },
        "immutableInputHashes": {
            "scopedSource": file_sha256(args.scoped_diagnostic_source),
            "prices": file_sha256(args.prices),
            "costModel": file_sha256(args.cost_model),
            "absentPreflight": active.get("immutableInputs", {}).get(
                "absentPreflightSha256"
            ),
            "preparedPreflight": file_sha256(args.prepared_preflight),
            "imageManifest": file_sha256(args.image_manifest),
            "commandSetFile": file_sha256(
                args.runtime_dir / "inputs/command-set.json"
            ),
        },
        "failure": {
            "firstFailingGate": failing_gate,
            "rawAwsError": raw_error or None,
            "rawRunnerError": failure.get("error"),
            "diagnosis": diagnosis,
            "sourceDropExecuted": report.get("sourceDropExecuted"),
        },
        "fix": {
            "applied": fix_record is not None,
            "description": (
                fix_record[1].get("summary") if fix_record is not None else None
            ),
            "fixCommit": (
                fix_record[1].get("fixCommit") if fix_record is not None else None
            ),
            "requiredBetweenAttempts": (
                report.get("verdict") != "passed" and fix_record is None
            ),
        },
        "paidWindow": {
            "startedAt": active["paidStartedAt"],
            "endedAt": run["finalizedAt"],
            "elapsedSeconds": format((paid_end - paid_start).total_seconds(), ".3f"),
            "cleanupDeadline": run["cleanupStartDeadline"],
            "hardDeadline": run["hardDeadline"],
            "deadlineBreached": bool(run.get("hardDeadlineBreachedDuringCleanup")),
        },
        "cost": {
            "activeEpochId": active["activeEpochId"],
            "activeEpochPriorUpperBoundUsd": money(prior),
            "chargedUpperBoundUsd": money(total_charge),
            "activeEpochAccruedUpperBoundUsdAfterAttempt": money(active_accrued),
            "plannedOperationalUpperBoundUsd": money(planned_charge),
            "nextRetryOperationalUpperBoundUsd": money(planned_charge),
            "cleanupOverrunUpperBoundUsd": money(cleanup_overrun),
            "projectedScopedRetryMaximumIncludingCleanupUsd": money(
                projected_scoped_retry
            ),
            "phase8PaidAwsExperimentOperationalUpperBoundUsd": "0.000000",
            "measuredCostUsd": None,
            "measuredCostEstimated": True,
            "delayedBillingCoveredByUpperBound": True,
        },
        "cleanup": {
            "attempts": {
                "runnerCleanup": stage_counts["cleanup"],
                "runnerInventory": stage_counts["inventory"],
                "recoveryCleanup": len(run.get("cleanupRecoveryAttempts", [])),
            },
            "finishedAt": run["finalizedAt"],
            "evidence": terminal_hashes["cleanup"],
            "finalAuthoritativeInventory": {
                "serviceClassCount": len(cleanup["counts"]),
                "serviceInventoryZero": cleanup["serviceInventoryZero"],
                "taggingApiAuthoritative": cleanup["taggingApiAuthoritative"],
                "taggingApiResidualsZero": cleanup["taggingApiResidualsZero"],
                "taggingApiResiduals": cleanup["taggingApiResiduals"],
                "allZero": cleanup["allZero"],
            },
        },
        "terminalEvidenceHashes": terminal_hashes,
    }
    entry["entrySha256"] = cost_canonical_sha256(entry)

    next_scoped_charge = planned_charge
    if report.get("verdict") != "passed" and fix_record is not None:
        # Reprice the already-fixed next retry against the just-closed active
        # epoch using the same fresh price document. The provisional ledger is
        # not persisted; it only supplies an idle, hash-linked cost basis.
        provisional = terminal_campaign_ledger(
            ledger,
            active,
            entry,
            active_accrued=active_accrued,
            next_scoped_charge=Decimal("0"),
            updated_at=run["finalizedAt"],
        )
        next_model = build_cost_model(
            inputs["prices"],
            provisional,
            inputs["cost"]["phase8PromotionPolicy"],
        )
        next_scoped_charge = Decimal(
            str(next_model["chargedOperationalUpperBoundUsd"])
        )
        projected_scoped_retry = (
            active_accrued + next_scoped_charge + Decimal("5")
        )
        entry["cost"]["nextRetryOperationalUpperBoundUsd"] = money(
            next_scoped_charge
        )
        entry["cost"]["projectedScopedRetryMaximumIncludingCleanupUsd"] = money(
            projected_scoped_retry
        )
        entry.pop("entrySha256", None)
        entry["entrySha256"] = cost_canonical_sha256(entry)

    next_ledger = terminal_campaign_ledger(
        ledger,
        active,
        entry,
        active_accrued=active_accrued,
        next_scoped_charge=next_scoped_charge,
        updated_at=run["finalizedAt"],
    )
    entry_evidence = args.runtime_dir / "campaign-ledger-entry.json"
    if entry_evidence.exists():
        if read_json(entry_evidence) != entry:
            raise RuntimeError("immutable terminal ledger-entry evidence changed")
    else:
        write_json(entry_evidence, entry)
    write_campaign_resume(
        args,
        entry=entry,
        active_accrued=active_accrued,
        cleanup_zero=True,
    )
    write_json(args.attempt_ledger, next_ledger)
    return entry


def finalize_cleaned_early_failure_ledger(
    args: argparse.Namespace,
    *,
    failure_stage: str,
    error: BaseException,
    cleanup_path: Path,
    failure_path: Path,
    evidence_dir: Path,
) -> dict[str, Any]:
    cleanup = read_json(cleanup_path)
    if not validate_cleanup_inventory_document(cleanup, args.run_id, args.session_id):
        raise RuntimeError("early failure cannot be terminalized before cleanup zero")
    ledger = read_json(args.attempt_ledger)
    active = dict(ledger.get("activeAttempt", {}))
    if (
        active.get("runId") != args.run_id
        or active.get("sessionId") != args.session_id
        or active.get("ordinal") != len(ledger.get("attempts", [])) + 1
        or active.get("state") not in {
            "image-preparation-failed-cleaned",
            "initialization-failed-cleaned-awaiting-ledger-entry",
        }
        or int(active.get("runtimeDeployAttempts", 0)) != 0
        or int(active.get("imagePreparationAttempts", 0)) != 1
        or int(active.get("imageStackDeployAttempts", 0)) != 1
        or active.get("stageMaximumAttempts", {}).get("imagePreparation") != 1
        or active.get("stageMaximumAttempts", {}).get("imageStackDeploy") != 1
    ):
        raise RuntimeError("early failure does not match the durable active attempt")
    validate_campaign_ledger(
        ledger,
        allow_active=True,
        expected_run_id=args.run_id,
        expected_session_id=args.session_id,
    )
    source_checks, source = scoped_diagnostic_source_checks(
        args.infra_root, args.scoped_diagnostic_source
    )
    if not all(check.passed for check in source_checks):
        raise RuntimeError("early failure source seal changed before terminalization")
    cost = read_json(args.cost_model)
    immutable = active.get("immutableInputs", {})
    if (
        immutable.get("sourceSha256") != file_sha256(args.scoped_diagnostic_source)
        or immutable.get("costModelSha256") != file_sha256(args.cost_model)
        or active.get("chargedOperationalUpperBoundUsd")
        != cost.get("chargedOperationalUpperBoundUsd")
    ):
        raise RuntimeError("early failure immutable cost/source binding changed")

    failure_document = read_json(failure_path)
    paid_start = parse_utc(str(active["paidStartedAt"]))
    terminal_at = parse_utc(str(failure_document["failedAt"]))
    if terminal_at < paid_start:
        raise RuntimeError("early failure paid window is invalid")
    cleanup_start_minutes = int(
        cost["stageDeadlineMinutes"]["cleanupStart"]
    )
    hard_minutes = int(cost["stageDeadlineMinutes"]["hard"])
    hard_deadline = paid_start + timedelta(minutes=hard_minutes)
    planned_charge = Decimal(str(cost["chargedOperationalUpperBoundUsd"]))
    prior = Decimal(str(active["activeEpochPriorUpperBoundUsd"]))
    if money(prior + planned_charge) != str(cost["operationalMaximumUsd"]):
        raise RuntimeError("early failure charge disagrees with admission cost model")
    cleanup_overrun = (
        Decimal(str(cost["cleanupReserveUsd"]))
        if terminal_at > hard_deadline
        or cleanup.get("cleanupDeadlineBreached") is True
        else Decimal("0")
    )
    total_charge = planned_charge + cleanup_overrun
    active_accrued = prior + total_charge
    projected_scoped_retry = active_accrued + planned_charge + Decimal("5")

    image_manifest_path = getattr(args, "image_manifest", None)
    if image_manifest_path is None:
        image_manifest_path = getattr(args, "output", None)
    images: dict[str, Any] = {}
    if isinstance(image_manifest_path, Path) and image_manifest_path.is_file():
        candidate = read_json(image_manifest_path)
        if (
            candidate.get("runId") == args.run_id
            and candidate.get("sessionId") == args.session_id
        ):
            images = candidate
    image_by_role = {
        str(item["role"]): item
        for item in images.get("images", [])
        if isinstance(item, dict) and item.get("role")
    }
    if image_by_role and (
        set(image_by_role) != {"collector", "consumer", "archive"}
        or images.get("collectorCommit") != PHASE7_COLLECTOR_COMMIT
        or any(
            item.get("sourceClosureSha256")
            != image_source_closure_sha256(
                role, str(source["implementationTreeSha256"])
            )
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}", str(item.get("digest", ""))
            ) is None
            for role, item in image_by_role.items()
        )
    ):
        raise RuntimeError("early failure image manifest is not exact")
    runtime_dir = getattr(args, "runtime_dir", None)
    runtime_path = runtime_dir if isinstance(runtime_dir, Path) else None
    terminal_candidates = {
        "failure": failure_path,
        "cleanup": cleanup_path,
    }
    if images and isinstance(image_manifest_path, Path):
        terminal_candidates["imageManifest"] = image_manifest_path
    if runtime_path is not None:
        terminal_candidates.update({
            "run": runtime_path / "run.json",
            "commandSet": runtime_path / "inputs/command-set.json",
        })
    terminal_hashes = {
        name: evidence
        for name, path in terminal_candidates.items()
        if (evidence := relative_evidence(args, path)) is not None
    }
    evidence_dir = evidence_dir.resolve()
    root = args.infra_root.resolve()
    evidence_dir.relative_to(root)
    entry = {
        "ordinal": active["ordinal"],
        "runId": args.run_id,
        "sessionId": args.session_id,
        "attemptType": ATTEMPT_TYPE,
        "promotionEligible": False,
        "phase5": "skipped",
        "verdict": "failed",
        "runnerFinalVerdict": "failed",
        "firstFailingGate": failure_stage,
        "previousEntrySha256": ledger.get("ledgerHeadSha256"),
        "gitCommit": source.get("gitCommit"),
        "implementationGitTree": source.get("gitTree"),
        "implementationSourceClosureSha256": source.get(
            "implementationTreeSha256"
        ),
        "evidencePaths": {
            "awsAttempt": (
                str(runtime_path.resolve().relative_to(root))
                if runtime_path is not None and runtime_path.exists()
                else None
            ),
            "deploymentReadiness": str(evidence_dir.relative_to(root)),
            "scopedSource": str(
                args.scoped_diagnostic_source.resolve().relative_to(root)
            ),
            "localHandoff": None,
        },
        "imageSourceHashes": {
            role: image_source_closure_sha256(
                role, str(source["implementationTreeSha256"])
            )
            for role in ("archive", "collector", "consumer")
        },
        "imageDigests": {
            role: image_by_role.get(role, {}).get("digest")
            for role in ("archive", "collector", "consumer")
        },
        "sealedCommandSetSha256": active.get("commandSetSha256"),
        "stageAttemptCounts": {
            "imagePreparation": int(active.get("imagePreparationAttempts", 0)),
            "imageStackDeploy": int(active.get("imageStackDeployAttempts", 0)),
            "runtimeDeploy": 0,
            "verify": 0,
            "seed15M": 0,
            "archive": 0,
            "collect": 0,
            "cleanup": 1,
            "inventory": 1,
            **{stage: 0 for stage in ZERO_ATTEMPT_STAGES},
        },
        "immutableInputHashes": {
            "scopedSource": immutable.get("sourceSha256"),
            "prices": immutable.get("pricesSha256"),
            "costModel": immutable.get("costModelSha256"),
            "absentPreflight": immutable.get("absentPreflightSha256"),
            "preparedPreflight": active.get("preparedPreflightSha256"),
            "imageManifest": (
                file_sha256(image_manifest_path)
                if images and isinstance(image_manifest_path, Path)
                else None
            ),
            "commandSetFile": (
                file_sha256(runtime_path / "inputs/command-set.json")
                if runtime_path is not None
                and (runtime_path / "inputs/command-set.json").is_file()
                else None
            ),
        },
        "failure": {
            "firstFailingGate": failure_stage,
            "rawAwsError": str(error)[:4000],
            "rawRunnerError": str(error)[:1000],
            "diagnosis": (
                f"Immutable scoped attempt failed before runtime deploy at "
                f"{failure_stage}: {str(error)[:1000]}"
            ),
            "sourceDropExecuted": False,
            "evidencePath": str(failure_path.resolve().relative_to(root)),
        },
        "fix": {
            "applied": False,
            "description": None,
            "fixCommit": None,
            "requiredBetweenAttempts": True,
        },
        "paidWindow": {
            "startedAt": active["paidStartedAt"],
            "endedAt": failure_document["failedAt"],
            "elapsedSeconds": format(
                (terminal_at - paid_start).total_seconds(), ".3f"
            ),
            "cleanupDeadline": (
                paid_start + timedelta(minutes=cleanup_start_minutes)
            ).isoformat().replace("+00:00", "Z"),
            "hardDeadline": hard_deadline.isoformat().replace("+00:00", "Z"),
            "deadlineBreached": cleanup_overrun > 0,
        },
        "cost": {
            "activeEpochId": active["activeEpochId"],
            "activeEpochPriorUpperBoundUsd": money(prior),
            "chargedUpperBoundUsd": money(total_charge),
            "activeEpochAccruedUpperBoundUsdAfterAttempt": money(active_accrued),
            "plannedOperationalUpperBoundUsd": money(planned_charge),
            "cleanupOverrunUpperBoundUsd": money(cleanup_overrun),
            "projectedScopedRetryMaximumIncludingCleanupUsd": money(
                projected_scoped_retry
            ),
            "phase8PaidAwsExperimentOperationalUpperBoundUsd": "0.000000",
            "measuredCostUsd": None,
            "measuredCostEstimated": True,
            "delayedBillingCoveredByUpperBound": True,
        },
        "cleanup": {
            "attempts": {
                "automaticCleanup": 1,
                "authoritativeInventory": 1,
            },
            "finishedAt": failure_document["failedAt"],
            "evidence": terminal_hashes["cleanup"],
            "finalAuthoritativeInventory": {
                "serviceClassCount": len(cleanup["counts"]),
                "serviceInventoryZero": cleanup["serviceInventoryZero"],
                "taggingApiAuthoritative": cleanup["taggingApiAuthoritative"],
                "taggingApiResidualsZero": cleanup["taggingApiResidualsZero"],
                "taggingApiResiduals": cleanup["taggingApiResiduals"],
                "allZero": cleanup["allZero"],
            },
        },
        "terminalEvidenceHashes": terminal_hashes,
    }
    entry["entrySha256"] = cost_canonical_sha256(entry)
    next_ledger = terminal_campaign_ledger(
        ledger,
        active,
        entry,
        active_accrued=active_accrued,
        next_scoped_charge=planned_charge,
        updated_at=failure_document["failedAt"],
    )
    entry_evidence = evidence_dir / "campaign-ledger-entry.json"
    if entry_evidence.exists():
        if read_json(entry_evidence) != entry:
            raise RuntimeError("immutable early terminal ledger-entry evidence changed")
    else:
        write_json(entry_evidence, entry)
    write_campaign_resume(
        args,
        entry=entry,
        active_accrued=active_accrued,
        cleanup_zero=True,
    )
    write_json(args.attempt_ledger, next_ledger)
    return entry


def execute(args: argparse.Namespace) -> dict[str, Any]:
    inputs = validate_inputs(args)
    command_set = initialize(args, inputs)
    failure: BaseException | None = None
    aws: AwsRuntime | None = None
    current_gate = "identity-before-deploy"
    paid_start = parse_utc(read_json(args.runtime_dir / "run.json")["paidStartedAt"])
    try:
        write_json(
            args.runtime_dir / "evidence/control/identity-before-deploy.json",
            assert_current_identity(),
        )
        current_gate = "runtime-deploy-admission"
        record_runtime_deploy_start(args)
        current_gate = "deploy"
        execute_command_stage(
            args,
            "deploy",
            command_set["deployment"]["argv"],
            STAGE_TIMEOUTS["deploy"],
            "cdk-outputs.json",
        )
        bundle = load_bundle(args.runtime_dir)
        aws = AwsRuntime(bundle)
        current_gate = "verify"
        execute_callable_stage(
            args,
            "verify",
            lambda: verify_deployment(aws, args.ca_certificate),
            "deployment-verification.json",
        )
        current_gate = "seed"
        execute_callable_stage(args, "seed", lambda: seed(aws), "seed-summary.json")
        current_gate = "archive"
        execute_callable_stage(args, "archive", lambda: run_archive(aws), "archive-stage.json")
        current_gate = "collect"
        execute_callable_stage(
            args,
            "collect",
            lambda: collect_evidence(aws, paid_start),
            "metrics-summary.json",
        )
    except BaseException as error:
        failure = error
        run = read_json(args.runtime_dir / "run.json")
        if not run.get("failedStage"):
            run["failedStage"] = current_gate
            run["failure"] = {
                "errorType": type(error).__name__,
                "error": str(error)[:1000],
            }
            run["status"] = "cleanup-required"
            write_json(args.runtime_dir / "run.json", run)
        if aws is not None:
            try:
                evidence = call_with_timeout(
                    lambda: collect_failure_evidence(aws, paid_start), 60
                )
                write_json(args.runtime_dir / "failure-evidence.json", evidence)
            except BaseException as evidence_error:
                write_json(args.runtime_dir / "failure-evidence-collection-error.json", {
                    "schemaVersion": 1,
                    "failedAt": utc_now(),
                    "errorType": type(evidence_error).__name__,
                    "error": str(evidence_error)[:1000],
                })
        failures = args.runtime_dir / "failures.md"
        with failures.open("a", encoding="utf-8") as stream:
            stream.write(
                f"- `{utc_now()}` `{type(error).__name__}`: {str(error)[:1000]}\n"
            )
        write_json(args.runtime_dir / "failure.json", {
            "schemaVersion": 1,
            "failedAt": utc_now(),
            "errorType": type(error).__name__,
            "error": str(error)[:1000],
            "traceback": "".join(traceback.format_exception(error))[-8000:],
        })
    finally:
        cleanup_script = args.infra_root / "performance-tests/phase7-integration/aws/cleanup.py"
        cleanup_attempt_output = (
            args.runtime_dir / "cleanup-attempt-1-verification.json"
        )
        cleanup_output = args.runtime_dir / "cleanup-verification.json"
        try:
            execute_command_stage(
                args,
                "cleanup",
                [
                    sys.executable,
                    str(cleanup_script),
                    "--run-id",
                    args.run_id,
                    "--session-id",
                    args.session_id,
                    "--execute",
                    "--timeout-seconds",
                    "1080",
                    "--output",
                    str(cleanup_attempt_output),
                ],
                STAGE_TIMEOUTS["cleanup"],
                "cleanup-attempt-1-verification.json",
            )
        except BaseException as cleanup_error:
            failure = failure or cleanup_error
        try:
            execute_command_stage(
                args,
                "inventory",
                [
                    sys.executable,
                    str(cleanup_script),
                    "--run-id",
                    args.run_id,
                    "--session-id",
                    args.session_id,
                    "--output",
                    str(cleanup_output),
                ],
                STAGE_TIMEOUTS["inventory"],
                "cleanup-verification.json",
            )
        except BaseException as inventory_error:
            failure = failure or inventory_error
    try:
        ensure_authoritative_cleanup_zero(args)
    except BaseException as recovery_error:
        failure = failure or recovery_error
        run = read_json(args.runtime_dir / "run.json")
        run["cleanupRecoveryError"] = {
            "failedAt": utc_now(),
            "errorType": type(recovery_error).__name__,
            "error": str(recovery_error)[:1000],
        }
        write_json(args.runtime_dir / "run.json", run)
    report = write_terminal_report(args, inputs)
    if report.get("cleanupInventoryZero") is True:
        finalize_cleaned_attempt_ledger(args, inputs, report)
    if failure is not None and report["verdict"] == "passed":
        raise RuntimeError("scoped diagnostic cannot pass after a recorded failure")
    return report


def emergency_cleanup_after_initialization_failure(
    args: argparse.Namespace, error: BaseException
) -> bool | None:
    try:
        image_manifest = read_json(args.image_manifest)
        if (
            image_manifest.get("runId") != args.run_id
            or image_manifest.get("sessionId") != args.session_id
            or not image_manifest.get("paidStartedAt")
        ):
            return None
    except Exception:
        return None
    cleanup_output = args.readiness_dir / "preinitialize-cleanup-verification.json"
    failure_output = args.readiness_dir / "preinitialize-failure.json"
    stdout_path = args.readiness_dir / "preinitialize-cleanup.stdout.log"
    stderr_path = args.readiness_dir / "preinitialize-cleanup.stderr.log"
    if any(
        path.exists()
        for path in (cleanup_output, failure_output, stdout_path, stderr_path)
    ):
        raise RuntimeError(
            "refusing to overwrite immutable preinitialize cleanup evidence"
        ) from error
    cleanup_script = (
        args.infra_root / "performance-tests/phase7-integration/aws/cleanup.py"
    )
    environment = os.environ.copy()
    for name in FORBIDDEN_CREDENTIAL_ENV:
        environment.pop(name, None)
    outcome = run_command(
        [
            sys.executable,
            str(cleanup_script),
            "--run-id",
            args.run_id,
            "--session-id",
            args.session_id,
            "--execute",
            "--timeout-seconds",
            "1080",
            "--output",
            str(cleanup_output),
        ],
        str(args.infra_root),
        environment,
        1200,
    )
    stdout_path.write_text(outcome.stdout, encoding="utf-8")
    stderr_path.write_text(outcome.stderr, encoding="utf-8")
    cleanup_zero = False
    if cleanup_output.is_file():
        try:
            cleanup_zero = validate_cleanup_inventory_document(
                read_json(cleanup_output), args.run_id, args.session_id
            )
        except Exception:
            cleanup_zero = False
    write_json(failure_output, {
        "schemaVersion": 1,
        "runId": args.run_id,
        "sessionId": args.session_id,
        "failedAt": utc_now(),
        "failedBeforeInitialization": True,
        "errorType": type(error).__name__,
        "error": str(error)[:2000],
        "automaticCleanup": {
            "returncode": outcome.returncode,
            "timedOut": outcome.timed_out,
            "authoritativeInventoryZero": cleanup_zero,
            "verificationPath": str(cleanup_output),
        },
    })
    ledger_updated = False
    try:
        ledger = read_json(args.attempt_ledger)
        active = dict(ledger.get("activeAttempt", {}))
        if (
            active.get("runId") == args.run_id
            and active.get("sessionId") == args.session_id
        ):
            active["state"] = (
                "initialization-failed-cleaned-awaiting-ledger-entry"
                if cleanup_zero
                else "cleanup-required"
            )
            active["initializationFailure"] = {
                "failedAt": utc_now(),
                "errorType": type(error).__name__,
                "error": str(error)[:2000],
                "cleanupInventoryZero": cleanup_zero,
                "evidencePath": str(failure_output.relative_to(args.infra_root)),
            }
            ledger["activeAttempt"] = active
            ledger["updatedAt"] = utc_now()
            write_json(args.attempt_ledger, ledger)
            ledger_updated = True
    except Exception as ledger_error:
        ledger_error_path = (
            args.readiness_dir / "preinitialize-ledger-update-error.json"
        )
        if not ledger_error_path.exists():
            write_json(ledger_error_path, {
                "schemaVersion": 1,
                "runId": args.run_id,
                "sessionId": args.session_id,
                "failedAt": utc_now(),
                "errorType": type(ledger_error).__name__,
                "error": str(ledger_error)[:2000],
            })
    if cleanup_zero and ledger_updated:
        try:
            finalize_cleaned_early_failure_ledger(
                args,
                failure_stage="runtime-initialization",
                error=error,
                cleanup_path=cleanup_output,
                failure_path=failure_output,
                evidence_dir=args.readiness_dir,
            )
        except BaseException as terminal_error:
            terminal_error_path = (
                args.readiness_dir
                / "preinitialize-terminalization-error.json"
            )
            if not terminal_error_path.exists():
                write_json(terminal_error_path, {
                    "schemaVersion": 1,
                    "runId": args.run_id,
                    "sessionId": args.session_id,
                    "failedAt": utc_now(),
                    "errorType": type(terminal_error).__name__,
                    "error": str(terminal_error)[:2000],
                    "activeAttemptRetained": True,
                })
            raise RuntimeError(
                "initialization cleanup reached zero but terminal ledger append failed"
            ) from terminal_error
    return cleanup_zero


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--scoped-diagnostic-source", required=True, type=Path)
    parser.add_argument("--prepared-preflight", required=True, type=Path)
    parser.add_argument("--image-manifest", required=True, type=Path)
    parser.add_argument("--cost-model", required=True, type=Path)
    parser.add_argument("--prices", required=True, type=Path)
    parser.add_argument("--attempt-ledger", required=True, type=Path)
    parser.add_argument("--readiness-dir", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--ca-certificate", required=True, type=Path)
    args = parser.parse_args()
    args.infra_root = args.infra_root.resolve()
    args.scoped_diagnostic_source = args.scoped_diagnostic_source.resolve()
    args.prepared_preflight = args.prepared_preflight.resolve()
    args.image_manifest = args.image_manifest.resolve()
    args.cost_model = args.cost_model.resolve()
    args.prices = args.prices.resolve()
    args.attempt_ledger = args.attempt_ledger.resolve()
    args.readiness_dir = args.readiness_dir.resolve()
    args.runtime_dir = args.runtime_dir.resolve()
    args.ca_certificate = args.ca_certificate.resolve()
    return args


def main() -> int:
    args = parse_args()
    try:
        report = execute(args)
    except KeyboardInterrupt as error:
        emergency_cleanup_after_initialization_failure(args, error)
        return 130
    except BaseException as error:
        emergency_cleanup_after_initialization_failure(args, error)
        raise
    return 0 if report.get("verdict") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

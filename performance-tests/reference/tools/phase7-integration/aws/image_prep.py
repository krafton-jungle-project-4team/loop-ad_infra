#!/usr/bin/env python3
"""Create the run-owned image stack and push three immutable Phase 7 images."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

from common import (
    EXPECTED_ACCOUNT,
    EXPECTED_OPERATOR_ARN,
    EXPECTED_REGION,
    IMAGE_STACK_NAME,
    PHASE7_COLLECTOR_COMMIT,
    file_sha256,
    handoff_checks,
    image_source_closure_sha256,
    read_json,
    reject_strict_paid_work_under_composite_policy,
    scoped_diagnostic_source_checks,
    tag_map,
    tags_match,
    utc_now,
    validate_identifiers,
    write_json,
)
from full_stack_scoped_cost_model import canonical_sha256, validate_cost_model
from evidence_assembler import validate_cleanup_inventory_document
from preflight import AwsSnapshot, evaluate_preflight


COLLECTOR_COMMIT = PHASE7_COLLECTOR_COMMIT
DUMMY_DIGEST = "sha256:" + "0" * 64
SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "total_max_attempts": 5},
    user_agent_appid="loopad-phase7-image-prep/1",
)


def start_paid_watchdog(args: argparse.Namespace, deadline: datetime) -> None:
    if not hasattr(signal, "SIGALRM"):
        raise RuntimeError("image preparation deadline enforcement requires SIGALRM")

    def handler(_signum: int, _frame: Any) -> None:
        raise TimeoutError("image preparation reached its cleanup-start deadline")

    args._paid_watchdog_previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, handler)
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise TimeoutError("image preparation paid deadline is already exhausted")
    signal.setitimer(signal.ITIMER_REAL, remaining)


def stop_paid_watchdog(args: argparse.Namespace) -> None:
    if not hasattr(args, "_paid_watchdog_previous_handler"):
        return
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, args._paid_watchdog_previous_handler)
    delattr(args, "_paid_watchdog_previous_handler")


def run(command: list[str], cwd: Path, *, stdin: str | None = None, capture: bool = False,
        env: dict[str, str] | None = None, deadline: datetime | None = None) -> str:
    timeout = None
    if deadline is not None:
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError("image preparation reached its cleanup-start deadline")
        timeout = max(1, int(remaining))
    completed = subprocess.run(
        command,
        cwd=cwd,
        input=stdin,
        text=True,
        check=True,
        capture_output=capture,
        env=env,
        timeout=timeout,
    )
    return completed.stdout.strip() if capture else ""


def assert_collector_source_capability(
    collector_repository: Path,
    infra_root: Path,
) -> None:
    if not collector_repository.is_dir():
        raise FileNotFoundError(
            "collector repository must be an existing local Git working tree"
        )
    run(
        [
            "git",
            "-C",
            str(collector_repository),
            "cat-file",
            "-e",
            f"{COLLECTOR_COMMIT}^{{commit}}",
        ],
        infra_root,
    )


def docker_plugin_directories() -> list[Path]:
    candidates = [
        Path.home() / ".docker/cli-plugins",
        Path("/usr/local/lib/docker/cli-plugins"),
        Path("/usr/local/libexec/docker/cli-plugins"),
        Path("/usr/lib/docker/cli-plugins"),
        Path("/usr/libexec/docker/cli-plugins"),
        Path("/Applications/Docker.app/Contents/Resources/cli-plugins"),
    ]
    return [candidate for candidate in candidates if (candidate / "docker-buildx").exists()]


def isolated_docker_environment(docker_config: Path) -> dict[str, str]:
    plugin_directories = docker_plugin_directories()
    if not plugin_directories:
        raise RuntimeError("docker buildx plugin was not found in a supported CLI plugin directory")
    docker_config.mkdir(parents=True, mode=0o700, exist_ok=True)
    write_json(docker_config / "config.json", {
        "cliPluginsExtraDirs": [str(path) for path in plugin_directories],
    })
    environment = os.environ.copy()
    environment["DOCKER_CONFIG"] = str(docker_config)
    return environment


def assert_docker_build_capability(infra_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="loopad-phase7-docker-preflight-", dir="/tmp") as parent:
        environment = isolated_docker_environment(Path(parent) / "docker-config")
        run(["docker", "buildx", "version"], infra_root, capture=True, env=environment)
        run(["docker", "buildx", "inspect", "--bootstrap"], infra_root, capture=True, env=environment)


def cdk_context(args: argparse.Namespace, digests: dict[str, str] | None = None) -> list[str]:
    values = digests or {"collector": DUMMY_DIGEST, "consumer": DUMMY_DIGEST, "archive": DUMMY_DIGEST}
    pairs = {
        "environment": "perf-phase7-integration",
        "phase7RunId": args.run_id,
        "phase7SessionId": args.session_id,
        "phase7CollectorImageDigest": values["collector"],
        "phase7ConsumerImageDigest": values["consumer"],
        "phase7ArchiveImageDigest": values["archive"],
        "phase7X86EcsAmiId": args.x86_ami,
        "phase7ArmEcsAmiId": args.arm_ami,
        "phase7ProtocolCertificateArn": args.certificate_arn,
        "phase7ProtocolDnsName": args.protocol_dns_name,
    }
    return [item for key, value in pairs.items() for item in ("-c", f"{key}={value}")]


def docker_login(
    ecr: Any,
    infra_root: Path,
    environment: dict[str, str],
    deadline: datetime,
) -> None:
    response = ecr.get_authorization_token()["authorizationData"]
    if len(response) != 1:
        raise RuntimeError("expected one ECR authorization endpoint")
    authorization = response[0]
    username, password = base64.b64decode(authorization["authorizationToken"]).decode("utf-8").split(":", 1)
    run(["docker", "login", "--username", username, "--password-stdin", authorization["proxyEndpoint"]], infra_root, stdin=password + "\n", env=environment, deadline=deadline)


def verify_platform(
    image: str,
    platform: str,
    infra_root: Path,
    environment: dict[str, str],
    deadline: datetime,
) -> None:
    run(["docker", "pull", "--platform", platform, image], infra_root, env=environment, deadline=deadline)
    observed = run(["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image], infra_root, capture=True, env=environment, deadline=deadline)
    if observed != platform:
        raise RuntimeError(f"image platform mismatch for {image}: {observed} != {platform}")


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    validate_identifiers(args.run_id, args.session_id)
    if any(os.environ.get(key) for key in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"
    )):
        raise RuntimeError("image preparation refuses AWS credential environment variables; use fresh aws login")
    if args.handoff:
        reject_strict_paid_work_under_composite_policy(args.infra_root)
        handoff_gate, handoff = handoff_checks(args.infra_root, args.handoff)
        source_kind = "phase7-1-handoff"
        source_path = str(args.handoff.resolve())
    else:
        handoff_gate, handoff = scoped_diagnostic_source_checks(
            args.infra_root, args.scoped_diagnostic_source
        )
        source_kind = "full-stack-scoped-diagnostic-source"
        source_path = str(args.scoped_diagnostic_source.resolve())
    failures = [check.name for check in handoff_gate if not check.passed]
    if failures:
        raise RuntimeError(f"handoff gate failed: {', '.join(failures)}")
    absent_preflight_sha256 = None
    cleanup_start_minutes = 95
    if source_kind == "full-stack-scoped-diagnostic-source":
        required = (
            args.absent_preflight,
            args.prices,
            args.cost_model,
            args.attempt_ledger,
        )
        if any(path is None for path in required):
            raise RuntimeError(
                "scoped image preparation requires absent preflight, prices, cost and ledger"
            )
        expected_ledger = (
            args.infra_root
            / "performance-tests/phase7_2-stabilization/attempt-ledger.json"
        ).resolve()
        if args.attempt_ledger.resolve() != expected_ledger:
            raise RuntimeError("scoped image preparation requires the exact campaign ledger")
        absent = read_json(args.absent_preflight)
        price_document = read_json(args.prices)
        cost_model = read_json(args.cost_model)
        cleanup_start_minutes = int(
            cost_model["stageDeadlineMinutes"]["cleanupStart"]
        )
        ledger = read_json(args.attempt_ledger)
        active_attempt = ledger.get("activeAttempt", {})
        if (
            active_attempt.get("runId") != args.run_id
            or active_attempt.get("sessionId") != args.session_id
            or active_attempt.get("attemptType")
            != "aws-full-stack-scoped-diagnostic"
            or active_attempt.get("state") != "sealed-unpaid"
            or active_attempt.get("paidStartedAt") is not None
        ):
            raise RuntimeError(
                "scoped attempt must be durably sealed before image preparation"
            )
        expected_cost_authorization = {
            "campaignLedgerSha256": cost_model.get("campaignLedgerSha256"),
            "priceDocumentSha256": cost_model.get("priceDocumentSha256"),
            "phase8PromotionPolicySha256": cost_model.get(
                "phase8PromotionPolicySha256"
            ),
        }
        if (
            absent.get("passed") is not True
            or absent.get("runId") != args.run_id
            or absent.get("sessionId") != args.session_id
            or absent.get("imageState") != "absent"
            or absent.get("attemptType") != "aws-full-stack-scoped-diagnostic"
            or absent.get("promotionEligible") is not False
            or absent.get("sourceAuthorization", {}).get(
                "implementationTreeSha256"
            ) != handoff.get("implementationTreeSha256")
            or absent.get("costAuthorization") != expected_cost_authorization
            or not validate_cost_model(
                price_document,
                ledger,
                cost_model,
                expected_run_id=args.run_id,
                expected_session_id=args.session_id,
            )
        ):
            raise RuntimeError("scoped absent preflight authorization is not exact")
        live_snapshot = AwsSnapshot().collect(
            args.run_id,
            args.session_id,
            args.x86_ami,
            args.arm_ami,
            args.certificate_arn,
        )
        revalidated = evaluate_preflight(
            live_snapshot,
            handoff,
            handoff_gate,
            price_document,
            cost_model,
            args.run_id,
            args.session_id,
            "absent",
            args.x86_ami,
            args.arm_ami,
            args.protocol_dns_name,
            source_kind=source_kind,
            source_path=source_path,
            campaign_ledger=ledger,
        )
        if revalidated.get("passed") is not True:
            raise RuntimeError("live scoped pre-paid preflight revalidation failed")
        revalidated_path = args.output.with_name(
            f"{args.output.stem}-live-absent-preflight.json"
        )
        if revalidated_path.exists():
            raise FileExistsError("live absent preflight evidence is immutable")
        write_json(revalidated_path, revalidated)
        absent_preflight_sha256 = file_sha256(args.absent_preflight)
    assert_docker_build_capability(args.infra_root)
    assert_collector_source_capability(args.collector_repository, args.infra_root)

    session = boto3.Session(region_name=EXPECTED_REGION)
    identity = session.client("sts", config=SDK_CONFIG).get_caller_identity()
    if identity.get("Account") != EXPECTED_ACCOUNT or identity.get("Arn") != EXPECTED_OPERATOR_ARN:
        raise RuntimeError("exact user-approved root identity is required")
    cloudformation = session.client("cloudformation", config=SDK_CONFIG)
    ecr = session.client("ecr", config=SDK_CONFIG)

    environment = os.environ.copy()
    environment.update({"CDK_DEFAULT_ACCOUNT": EXPECTED_ACCOUNT, "LOOP_AD_REGION": EXPECTED_REGION})
    cdk = args.infra_root / "node_modules" / ".bin" / "cdk"
    if not cdk.is_file():
        raise FileNotFoundError("checked workspace CDK executable is missing")
    paid_started_at = utc_now()
    cleanup_start_deadline = datetime.fromisoformat(
        paid_started_at.replace("Z", "+00:00")
    ) + timedelta(minutes=cleanup_start_minutes)
    paid_marker = args.output.with_name(f"{args.output.stem}-paid-start.json")
    if paid_marker.exists():
        raise FileExistsError("image preparation paid-start marker is immutable")
    write_json(paid_marker, {
        "schemaVersion": 1,
        "runId": args.run_id,
        "sessionId": args.session_id,
        "paidStartedAt": paid_started_at,
        "stage": "image-preparation",
        "cleanupRequiredOnFailure": True,
    })
    start_paid_watchdog(args, cleanup_start_deadline)
    if source_kind == "full-stack-scoped-diagnostic-source":
        ledger = read_json(args.attempt_ledger)
        active_attempt = dict(ledger["activeAttempt"])
        if (
            active_attempt.get("runId") != args.run_id
            or active_attempt.get("sessionId") != args.session_id
            or active_attempt.get("state") != "sealed-unpaid"
        ):
            raise RuntimeError("scoped attempt seal changed before the paid boundary")
        active_attempt["state"] = "image-preparation-paid"
        active_attempt["paidStartedAt"] = paid_started_at
        active_attempt["imagePreparationAttempts"] = 1
        active_attempt["imageStackDeployAttempts"] = 1
        active_attempt["paidStartEvidence"] = {
            "path": str(paid_marker.resolve().relative_to(args.infra_root)),
            "sha256": file_sha256(paid_marker),
        }
        ledger["activeAttempt"] = active_attempt
        budget = dict(ledger["budget"])
        budget["currentAttemptPaidStartAt"] = paid_started_at
        ledger["budget"] = budget
        ledger["updatedAt"] = paid_started_at
        write_json(args.attempt_ledger, ledger)
        paid_ledger_sha256 = canonical_sha256(ledger)
    else:
        paid_ledger_sha256 = None
    run([
        str(cdk), *cdk_context(args), "deploy", IMAGE_STACK_NAME, "--exclusively",
        "--require-approval", "never", "--concurrency", "1",
    ], args.infra_root, env=environment, deadline=cleanup_start_deadline)
    stack = cloudformation.describe_stacks(StackName=IMAGE_STACK_NAME)["Stacks"][0]
    tags = tag_map(stack.get("Tags", []))
    if stack["StackStatus"] != "CREATE_COMPLETE" or not tags_match(tags, args.run_id, args.session_id):
        raise RuntimeError("image stack is not CREATE_COMPLETE with exact ownership tags")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
    repositories = {
        "collector": (outputs["CollectorRepositoryName"], outputs["CollectorRepositoryUri"], "linux/amd64"),
        "consumer": (outputs["ConsumerRepositoryName"], outputs["ConsumerRepositoryUri"], "linux/arm64"),
        "archive": (outputs["ArchiveRepositoryName"], outputs["ArchiveRepositoryUri"], "linux/arm64"),
    }
    tag = f"tree-{handoff['implementationTreeSha256'][:24]}"

    collector_parent = Path(tempfile.mkdtemp(prefix="loopad-phase7-collector-", dir="/tmp"))
    collector_context = collector_parent / "source"
    docker_config = collector_parent / "docker-config"
    docker_environment = isolated_docker_environment(docker_config)
    try:
        docker_login(ecr, args.infra_root, docker_environment, cleanup_start_deadline)
        run(["git", "-C", str(args.collector_repository), "worktree", "add", "--detach", str(collector_context), COLLECTOR_COMMIT], args.infra_root, deadline=cleanup_start_deadline)
        build_specs = {
            "collector": (collector_context / "Dockerfile", collector_context),
            "consumer": (args.infra_root / "performance-tests/phase4-clickhouse/consumer/Dockerfile", args.infra_root),
            "archive": (args.infra_root / "performance-tests/phase7-integration/archive/Dockerfile", args.infra_root),
        }
        images: list[dict[str, Any]] = []
        for role in ("collector", "consumer", "archive"):
            repository_name, repository_uri, platform = repositories[role]
            dockerfile, context = build_specs[role]
            tagged = f"{repository_uri}:{tag}"
            run([
                "docker", "buildx", "build", "--platform", platform,
                "--provenance=false", "--sbom=false", "--pull", "--push",
                "--file", str(dockerfile), "--tag", tagged, str(context),
            ], args.infra_root, env=docker_environment, deadline=cleanup_start_deadline)
            detail = ecr.describe_images(repositoryName=repository_name, imageIds=[{"imageTag": tag}])["imageDetails"]
            if len(detail) != 1 or not str(detail[0].get("imageDigest", "")).startswith("sha256:"):
                raise RuntimeError(f"missing immutable digest for {role}")
            digest = detail[0]["imageDigest"]
            exact = f"{repository_uri}@{digest}"
            verify_platform(
                exact,
                platform,
                args.infra_root,
                docker_environment,
                cleanup_start_deadline,
            )
            images.append({
                "role": role,
                "repository": repository_name,
                "repositoryUri": repository_uri,
                "tag": tag,
                "digest": digest,
                "architecture": platform,
                "exactImage": exact,
                "sourceClosureSha256": image_source_closure_sha256(
                    role, handoff["implementationTreeSha256"]
                ),
            })
    finally:
        if collector_context.exists():
            subprocess.run(["git", "-C", str(args.collector_repository), "worktree", "remove", "--force", str(collector_context)], check=False, timeout=60)
        shutil.rmtree(collector_parent, ignore_errors=True)

    return {
        "schemaVersion": 1,
        "workload": (
            "phase7-end-to-end-integration"
            if source_kind == "phase7-1-handoff"
            else "phase7-full-stack-scoped-archive-diagnostic"
        ),
        "attemptType": (
            "aws-integration-strict"
            if source_kind == "phase7-1-handoff"
            else "aws-full-stack-scoped-diagnostic"
        ),
        "promotionEligible": source_kind == "phase7-1-handoff",
        "preparedAt": utc_now(),
        "paidStartedAt": paid_started_at,
        "paidStartEvidencePath": str(paid_marker.resolve()),
        "paidStartEvidenceSha256": file_sha256(paid_marker),
        "campaignLedgerPaidStateSha256": paid_ledger_sha256,
        "account": EXPECTED_ACCOUNT,
        "region": EXPECTED_REGION,
        "identityArn": identity["Arn"],
        "runId": args.run_id,
        "sessionId": args.session_id,
        "imageStackName": IMAGE_STACK_NAME,
        "imageStackStatus": stack["StackStatus"],
        "collectorCommit": COLLECTOR_COMMIT,
        "implementationTreeSha256": handoff["implementationTreeSha256"],
        "sourceAuthorization": {
            "kind": source_kind,
            "path": source_path,
            "implementationTreeSha256": handoff["implementationTreeSha256"],
            "absentPreflightSha256": absent_preflight_sha256,
        },
        "images": images,
        "runtimeDeployed": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-root", required=True, type=Path)
    parser.add_argument(
        "--collector-repository",
        required=True,
        type=Path,
        help="existing local loop-ad_event_collector Git working tree (not an ECR name)",
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--handoff", type=Path)
    source_group.add_argument("--scoped-diagnostic-source", type=Path)
    parser.add_argument("--absent-preflight", type=Path)
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--cost-model", type=Path)
    parser.add_argument("--attempt-ledger", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--x86-ami", required=True)
    parser.add_argument("--arm-ami", required=True)
    parser.add_argument("--certificate-arn", required=True)
    parser.add_argument("--protocol-dns-name", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def record_images_prepared(args: argparse.Namespace, result: dict[str, Any]) -> None:
    if args.scoped_diagnostic_source is None:
        return
    ledger = read_json(args.attempt_ledger)
    active = dict(ledger.get("activeAttempt", {}))
    if (
        active.get("runId") != args.run_id
        or active.get("sessionId") != args.session_id
        or active.get("state") != "image-preparation-paid"
        or active.get("paidStartedAt") != result.get("paidStartedAt")
    ):
        raise RuntimeError("paid scoped attempt state changed during image preparation")
    active["state"] = "images-prepared"
    active["imagesPreparedAt"] = result["preparedAt"]
    active["imageManifest"] = {
        "path": str(args.output.relative_to(args.infra_root)),
        "sha256": file_sha256(args.output),
        "digests": {
            item["role"]: item["digest"] for item in result["images"]
        },
    }
    ledger["activeAttempt"] = active
    ledger["updatedAt"] = result["preparedAt"]
    write_json(args.attempt_ledger, ledger)


def main() -> int:
    args = parse_args()
    args.infra_root = args.infra_root.resolve()
    args.collector_repository = args.collector_repository.resolve()
    for name in ("absent_preflight", "prices", "cost_model", "attempt_ledger"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    args.output = args.output.resolve()
    if args.output.exists():
        raise FileExistsError("image preparation output is immutable")
    try:
        result = prepare(args)
        write_json(args.output, result)
        record_images_prepared(args, result)
        stop_paid_watchdog(args)
    except BaseException as error:
        stop_paid_watchdog(args)
        paid_marker = args.output.with_name(f"{args.output.stem}-paid-start.json")
        if paid_marker.is_file():
            cleanup_output = args.output.with_name(
                f"{args.output.stem}-failure-cleanup-verification.json"
            )
            failure_output = args.output.with_name(
                f"{args.output.stem}-failure.json"
            )
            if cleanup_output.exists() or failure_output.exists():
                raise RuntimeError(
                    "refusing to overwrite immutable image preparation failure evidence"
                ) from error
            cleanup_script = (
                args.infra_root
                / "performance-tests/phase7-integration/aws/cleanup.py"
            )
            cleanup_exception = None
            try:
                completed = subprocess.run(
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
                    cwd=args.infra_root,
                    text=True,
                    capture_output=True,
                    timeout=1200,
                    check=False,
                )
            except BaseException as cleanup_error:
                cleanup_exception = cleanup_error
                completed = subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr=str(cleanup_error)
                )
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
                "errorType": type(error).__name__,
                "error": str(error)[:2000],
                "automaticCleanup": {
                    "returncode": completed.returncode,
                    "authoritativeInventoryZero": cleanup_zero,
                    "cleanupErrorType": (
                        type(cleanup_exception).__name__
                        if cleanup_exception is not None
                        else None
                    ),
                    "verificationPath": str(cleanup_output),
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-4000:],
                },
            })
            if args.scoped_diagnostic_source is not None and args.attempt_ledger:
                ledger = read_json(args.attempt_ledger)
                active = dict(ledger.get("activeAttempt", {}))
                if (
                    active.get("runId") == args.run_id
                    and active.get("sessionId") == args.session_id
                ):
                    active["state"] = (
                        "image-preparation-failed-cleaned"
                        if cleanup_zero
                        else "cleanup-required"
                    )
                    active["imagePreparationFailure"] = {
                        "failedAt": utc_now(),
                        "errorType": type(error).__name__,
                        "error": str(error)[:2000],
                        "evidencePath": str(failure_output.relative_to(args.infra_root)),
                        "cleanupVerificationPath": str(
                            cleanup_output.relative_to(args.infra_root)
                        ),
                        "authoritativeInventoryZero": cleanup_zero,
                    }
                    ledger["activeAttempt"] = active
                    ledger["updatedAt"] = utc_now()
                    write_json(args.attempt_ledger, ledger)
            if (
                cleanup_zero
                and args.scoped_diagnostic_source is not None
                and args.attempt_ledger
            ):
                try:
                    from full_stack_scoped_archive import (
                        finalize_cleaned_early_failure_ledger,
                    )

                    finalize_cleaned_early_failure_ledger(
                        args,
                        failure_stage="image-preparation",
                        error=error,
                        cleanup_path=cleanup_output,
                        failure_path=failure_output,
                        evidence_dir=args.output.parent,
                    )
                except BaseException as terminal_error:
                    terminal_error_path = args.output.with_name(
                        f"{args.output.stem}-terminalization-error.json"
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
                        "image cleanup reached zero but terminal ledger append failed"
                    ) from terminal_error
            if not cleanup_zero:
                raise RuntimeError(
                    "image preparation failed and automatic cleanup did not prove zero"
                ) from error
        raise
    print(json.dumps({"preparedAt": result["preparedAt"], "runId": result["runId"], "images": result["images"], "runtimeDeployed": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

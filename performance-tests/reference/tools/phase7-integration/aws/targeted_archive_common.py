#!/usr/bin/env python3
"""Fail-closed helpers for one immutable targeted Phase 7 archive attempt."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import boto3
from botocore.config import Config


EXPECTED_ACCOUNT = "742711170910"
EXPECTED_REGION = "ap-northeast-2"
EXPECTED_OPERATOR_ARN = f"arn:aws:iam::{EXPECTED_ACCOUNT}:root"
RUN_ID_PATTERN = re.compile(r"^run_\d{8}_\d{6}_phase7_archive_diagnostic$")
SESSION_ID_PATTERN = re.compile(r"^phase7-archive-diagnostic-\d{8}T\d{6}Z$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
OWNERSHIP_TAGS = {
    "Project": "loop-ad",
    "Phase": "7",
    "ResourceScope": "run",
    "ManagedBy": "codex",
    "AttemptType": "aws-targeted-diagnostic",
    "PromotionEligible": "false",
}
SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "total_max_attempts": 5},
    user_agent_appid="loopad-phase7-targeted-archive/1",
)
SOURCE_CLOSURE = (
    "assets/clickhouse/phase4-schema.sql",
    "performance-tests/phase6-archive/archive.py",
    "performance-tests/phase6-archive/requirements.txt",
    "performance-tests/phase6-archive/seed_partition.py",
    "performance-tests/phase7-integration/archive/Dockerfile",
    "performance-tests/phase7-integration/archive/entrypoint.py",
    "performance-tests/phase7-integration/archive/schema_sidecar.py",
    "performance-tests/phase7-integration/archive/targeted_seed.py",
)
TARGETED_IMPLEMENTATION = tuple(dict.fromkeys((*SOURCE_CLOSURE,
    "src/perf-phase7-archive-diagnostic-stack.ts",
    "src/perf-phase7-integration-stack.ts",
    "performance-tests/phase7-integration/aws/archive_diagnostic_app.ts",
    "performance-tests/phase7-integration/aws/lookup-targeted-archive-prices.mjs",
    "performance-tests/phase7-integration/aws/targeted_archive_common.py",
    "performance-tests/phase7-integration/aws/targeted_archive_cost_model.py",
    "performance-tests/phase7-integration/aws/targeted_archive_cleanup.py",
    "performance-tests/phase7-integration/aws/targeted_archive_image_prep.py",
    "performance-tests/phase7-integration/aws/targeted_archive_preflight.py",
    "performance-tests/phase7-integration/aws/targeted_archive_runtime.py",
    "performance-tests/phase7-integration/tests/test_targeted_archive_tooling.py",
    "test/perf-phase7-archive-diagnostic.test.ts",
    "test/perf-phase7-integration.test.ts",
)))


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_identifiers(run_id: str, session_id: str) -> None:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run ID must be a fresh targeted archive diagnostic identity")
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("session ID must be a fresh targeted archive diagnostic identity")
    run_stamp = run_id.removeprefix("run_").removesuffix("_phase7_archive_diagnostic")
    session_stamp = session_id.removeprefix("phase7-archive-diagnostic-").removesuffix("Z")
    if run_stamp.replace("_", "T") != session_stamp:
        raise ValueError("run ID and session ID timestamps must be identical")


def stack_names(session_id: str) -> tuple[str, str]:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("invalid targeted session ID")
    timestamp = "".join(character for character in session_id if character.isdigit())[-14:]
    return (
        f"LoopAd-P7-ArchiveDiag-Image-{timestamp}",
        f"LoopAd-P7-ArchiveDiag-Runtime-{timestamp}",
    )


def repository_name(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("invalid targeted run ID")
    return f"loop-ad/perf-phase7-targeted/{run_id}/archive"


def expected_tags(run_id: str, session_id: str) -> dict[str, str]:
    validate_identifiers(run_id, session_id)
    return {**OWNERSHIP_TAGS, "RunId": run_id, "SessionId": session_id}


def tag_map(tags: Iterable[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in tags:
        key = item.get("Key", item.get("key"))
        value = item.get("Value", item.get("value"))
        if key is not None and value is not None:
            result[str(key)] = str(value)
    return result


def tags_match(tags: dict[str, str], run_id: str, session_id: str) -> bool:
    return all(tags.get(key) == value for key, value in expected_tags(run_id, session_id).items())


def assert_no_static_credentials() -> None:
    present = [
        key
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
        if os.environ.get(key)
    ]
    if present:
        raise RuntimeError("targeted AWS tooling refuses credential environment variables; use aws login")


def locked_session() -> boto3.Session:
    assert_no_static_credentials()
    session = boto3.Session(region_name=EXPECTED_REGION)
    identity = session.client("sts", config=SDK_CONFIG).get_caller_identity()
    if identity.get("Account") != EXPECTED_ACCOUNT or identity.get("Arn") != EXPECTED_OPERATOR_ARN:
        raise RuntimeError("exact user-approved AWS root identity is required")
    return session


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=json_default) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"unsupported JSON evidence value: {type(value).__name__}")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def source_closure(infra_root: Path) -> dict[str, Any]:
    entries: list[dict[str, str]] = []
    combined = hashlib.sha256()
    for relative in SOURCE_CLOSURE:
        path = infra_root / relative
        if not path.is_file():
            raise FileNotFoundError(f"targeted image source is missing: {relative}")
        digest = file_sha256(path)
        entries.append({"path": relative, "sha256": digest})
        combined.update(relative.encode())
        combined.update(b"\0")
        combined.update(digest.encode())
        combined.update(b"\n")
    return {"files": entries, "sha256": combined.hexdigest()}


def git_identity(infra_root: Path) -> dict[str, str]:
    commit = run(["git", "rev-parse", "HEAD"], infra_root, capture=True)
    tree = run(["git", "rev-parse", "HEAD^{tree}"], infra_root, capture=True)
    changed = run(
        ["git", "status", "--short", "--", *TARGETED_IMPLEMENTATION],
        infra_root,
        capture=True,
    )
    if changed:
        raise RuntimeError(f"targeted implementation is not committed:\n{changed}")
    return {"commit": commit, "tree": tree}


def run(
    command: list[str],
    cwd: Path,
    *,
    capture: bool = False,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    allowed_codes: tuple[int, ...] = (0,),
) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=stdin,
        text=True,
        capture_output=capture,
        check=False,
    )
    if completed.returncode not in allowed_codes:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"command failed with exit {completed.returncode}: {' '.join(command)}\n{detail[-4000:]}"
        )
    return completed.stdout.strip() if capture else ""


def cdk_context(
    run_id: str,
    session_id: str,
    digest: str,
    arm_ami: str,
) -> list[str]:
    if not DIGEST_PATTERN.fullmatch(digest):
        raise ValueError("archive image must be pinned by exact sha256 digest")
    if not re.fullmatch(r"ami-[0-9a-f]{8,17}", arm_ami):
        raise ValueError("ARM ECS AMI must be an exact AMI ID")
    pairs = {
        "phase7ArchiveDiagnosticRunId": run_id,
        "phase7ArchiveDiagnosticSessionId": session_id,
        "phase7ArchiveDiagnosticImageDigest": digest,
        "phase7ArchiveDiagnosticArmEcsAmiId": arm_ami,
    }
    return [item for key, value in pairs.items() for item in ("-c", f"{key}={value}")]


def cdk_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "CDK_DEFAULT_ACCOUNT": EXPECTED_ACCOUNT,
        "LOOP_AD_REGION": EXPECTED_REGION,
    })
    return environment


def app_command(infra_root: Path) -> str:
    app = infra_root / "performance-tests/phase7-integration/aws/archive_diagnostic_app.ts"
    return f"{infra_root / 'node_modules/.bin/ts-node'} --prefer-ts-exts {app}"

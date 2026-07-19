#!/usr/bin/env python3
"""Generate the only Phase 7-2 stage command documents accepted by the runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from common import read_json, write_json
from runner import STAGES, STAGE_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "performance-tests/phase7-integration/aws/runtime_stages.py"
CLEANUP = ROOT / "performance-tests/phase7-integration/aws/cleanup.py"
FINAL_EVALUATE = ROOT / "performance-tests/phase7-integration/aws/final_evaluate.py"


def build_commands(run_dir: Path, ca_certificate: Path) -> dict[str, dict[str, Any]]:
    run_dir = run_dir.resolve()
    ca_certificate = ca_certificate.resolve()
    if not ca_certificate.is_file():
        raise FileNotFoundError("public CA certificate bundle is missing")
    run = read_json(run_dir / "run.json")
    preflight = read_json(run_dir / "inputs" / "preflight.json")
    images = read_json(run_dir / "inputs" / "image-manifest.json")
    run_id = str(run["runId"])
    session_id = str(run["sessionId"])
    if any(document.get("runId") != run_id or document.get("sessionId") != session_id for document in (preflight, images)):
        raise RuntimeError("command inputs belong to another run")
    snapshot = preflight["snapshot"]
    image_by_role = {item["role"]: item for item in images["images"]}
    uv = executable("uv")
    cdk = ROOT / "node_modules" / ".bin" / "cdk"
    if not cdk.is_file():
        raise FileNotFoundError("checked workspace CDK executable is missing")
    environment = {
        "CDK_DEFAULT_ACCOUNT": "742711170910",
        "LOOP_AD_REGION": "ap-northeast-2",
        "UV_CACHE_DIR": "/tmp/loopad-phase7-uv-cache",
    }
    runtime_base = [
        uv, "run", "--project", str(ROOT / "performance-tests/phase4-clickhouse/producer-env"),
        "--locked", "python", str(RUNTIME),
    ]
    cleanup_base = [
        uv, "run", "--project", str(ROOT / "performance-tests/phase4-clickhouse/producer-env"),
        "--locked", "python", str(CLEANUP),
        "--run-id", run_id, "--session-id", session_id,
    ]
    contexts = {
        "environment": "perf-phase7-integration",
        "phase7RunId": run_id,
        "phase7SessionId": session_id,
        "phase7CollectorImageDigest": image_by_role["collector"]["digest"],
        "phase7ConsumerImageDigest": image_by_role["consumer"]["digest"],
        "phase7ArchiveImageDigest": image_by_role["archive"]["digest"],
        "phase7X86EcsAmiId": snapshot["amis"]["x86"]["imageId"],
        "phase7ArmEcsAmiId": snapshot["amis"]["arm"]["imageId"],
        "phase7ProtocolCertificateArn": snapshot["certificate"]["arn"],
        "phase7ProtocolDnsName": snapshot["certificate"]["domainName"],
    }
    deploy_argv = [str(cdk), "deploy", "LoopAdPerfPhase7IntegrationStack", "--exclusively",
                   "--require-approval", "never", "--outputs-file", str(run_dir / "cdk-outputs.json")]
    for key, value in contexts.items():
        deploy_argv.extend(["-c", f"{key}={value}"])

    def command(argv: list[str], stage: str, disposition: str = "hard-stop") -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "argv": argv,
            "cwd": str(ROOT),
            "environment": environment,
            "timeoutSeconds": STAGE_TIMEOUT_SECONDS[stage],
            "nonzeroDisposition": disposition,
        }

    commands = {
        "deploy": command(deploy_argv, "deploy"),
        "verify": command([
            *runtime_base, "verify", "--run-dir", str(run_dir),
            "--ca-certificate", str(ca_certificate),
        ], "verify"),
        "correctness": command([*runtime_base, "correctness", "--run-dir", str(run_dir)], "correctness"),
        "seed": command([*runtime_base, "seed", "--run-dir", str(run_dir)], "seed"),
        "warmup": command([*runtime_base, "warmup", "--run-dir", str(run_dir), "--ca-certificate", str(ca_certificate)], "warmup"),
        "score_archive": command([*runtime_base, "score_archive", "--run-dir", str(run_dir), "--ca-certificate", str(ca_certificate)], "score_archive"),
        "drain_validate": command([*runtime_base, "drain_validate", "--run-dir", str(run_dir)], "drain_validate"),
        "collect": command([*runtime_base, "collect", "--run-dir", str(run_dir)], "collect"),
        # Leave two minutes inside the runner's 20-minute watchdog for the
        # final inventory write and process-group termination evidence.
        "cleanup": command([*cleanup_base, "--execute", "--timeout-seconds", "1080", "--output", str(run_dir / "cleanup-inventory.json")], "cleanup"),
        "inventory": command([*cleanup_base, "--output", str(run_dir / "cleanup-inventory.json")], "inventory"),
        "evaluate": command([
            uv, "run", "--project", str(ROOT / "performance-tests/phase4-clickhouse/producer-env"),
            "--locked", "python", str(FINAL_EVALUATE), "--run-dir", str(run_dir),
        ], "evaluate", "acceptance-failure"),
    }
    if set(commands) != set(STAGES):
        raise RuntimeError("generated command set differs from the runner stage contract")
    return commands


def executable(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise FileNotFoundError(f"required executable is missing: {name}")
    return str(Path(value).resolve())


def seal_commands(run_dir: Path, commands: dict[str, dict[str, Any]]) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    run = read_json(run_dir / "run.json")
    if (
        run.get("status") != "initialized"
        or run.get("attemptedStages")
        or run.get("commandSetSha256") is not None
        or run.get("commandSetRequired") is not True
    ):
        raise RuntimeError("command set may be sealed exactly once before the first stage")
    inputs = run_dir / "inputs"
    metadata: dict[str, dict[str, str]] = {}
    for stage in STAGES:
        document = commands[stage]
        path = inputs / f"{stage}-command.json"
        write_json(path, document)
        digest = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        metadata[stage] = {"path": f"inputs/{stage}-command.json", "sha256": digest}
    command_set_sha = hashlib.sha256(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    seal = {
        "schemaVersion": 1,
        "runId": run["runId"],
        "sessionId": run["sessionId"],
        "commands": metadata,
        "commandSetSha256": command_set_sha,
    }
    write_json(inputs / "command-seal.json", seal)
    run["commandSetSha256"] = command_set_sha
    write_json(run_dir / "run.json", run)
    return seal


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--ca-certificate", required=True, type=Path)
    args = parser.parse_args()
    commands = build_commands(args.run_dir, args.ca_certificate)
    seal = seal_commands(args.run_dir, commands)
    inputs = args.run_dir.resolve() / "inputs"
    print(json.dumps({
        "stages": list(commands), "outputDirectory": str(inputs),
        "commandSetSha256": seal["commandSetSha256"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

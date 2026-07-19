#!/usr/bin/env python3
"""Capture final local unit, systemd, flock, cost, and CDK evidence without AWS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from cost_model import calculate, load_prices
from preflight import (
    CONTAINER_MEMORY_BYTES,
    QUERY_MEMORY_BYTES,
    SERVER_MEMORY_BYTES,
    LocalPreflight,
    evaluate_local_preflight,
)


def write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_gate(
    name: str,
    command: List[str],
    *,
    cwd: Path,
    evidence: Path,
    env: Mapping[str, str],
    timeout: int = 300,
) -> Dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    (evidence / f"{name}.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (evidence / f"{name}.stderr.log").write_text(completed.stderr, encoding="utf-8")
    return {"name": name, "command": command, "exitCode": completed.returncode, "passed": completed.returncode == 0}


def actions(statement: Mapping[str, Any]) -> Iterable[str]:
    value = statement.get("Action", [])
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def static_review(template_path: Path) -> Dict[str, Any]:
    template = json.loads(template_path.read_text(encoding="utf-8"))
    resources = template["Resources"]
    buckets = [value for value in resources.values() if value["Type"] == "AWS::S3::Bucket"]
    policies = [value for value in resources.values() if value["Type"] == "AWS::IAM::Policy"]
    launch_templates = [value for value in resources.values() if value["Type"] == "AWS::EC2::LaunchTemplate"]
    security_ingress = [value for value in resources.values() if value["Type"] == "AWS::EC2::SecurityGroupIngress"]
    allowed_actions = [
        action
        for policy in policies
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if statement.get("Effect") == "Allow"
        for action in actions(statement)
    ]
    block = buckets[0]["Properties"]["PublicAccessBlockConfiguration"]
    ebs = launch_templates[0]["Properties"]["LaunchTemplateData"]["BlockDeviceMappings"][0]["Ebs"]
    launch_data = launch_templates[0]["Properties"]["LaunchTemplateData"]
    user_data = json.dumps(launch_data["UserData"])
    checks = {
        "oneArchiveBucket": len(buckets) == 1,
        "publicAccessBlocked": all(block.values()),
        "securityGroupIngressCountZero": len(security_ingress) == 0,
        "archiveAllowActionsHaveNoDeleteObject": not any(action.startswith("s3:DeleteObject") for action in allowed_actions),
        "archiveAllowsListGetPut": all(action in allowed_actions for action in ["s3:ListBucket", "s3:GetObject", "s3:PutObject"]),
        "ebsDeleteOnTermination": ebs["DeleteOnTermination"] is True,
        "ebsEncrypted": ebs["Encrypted"] is True,
        "ebsGp3": ebs["VolumeType"] == "gp3",
        "imdsV2Required": launch_data["MetadataOptions"]["HttpTokens"] == "required",
        "containerImdsHopLimitTwo": launch_data["MetadataOptions"]["HttpPutResponseHopLimit"] == 2,
        "bootstrapParentsTraversable": "install -d -o root -g root -m 0711 /opt/loopad /etc/loopad" in user_data,
        "serviceUserExecutionPreflight": "runuser -u loopad-archive -- /opt/loopad/phase6/venv/bin/python --version" in user_data,
        "serviceUserConfigPreflight": "runuser -u loopad-archive -- test -r /etc/loopad/phase6/archive.json" in user_data,
        "queryMemoryExact": "clickhouse_memory_bytes" in user_data and str(QUERY_MEMORY_BYTES) in user_data,
        "serverMemoryConfigMounted": "clickhouse-config/memory.xml:/etc/clickhouse-server/config.d/phase6-memory.xml:ro" in user_data,
        "containerMemoryVerified": str(CONTAINER_MEMORY_BYTES) in user_data,
        "serverMemoryVerified": str(SERVER_MEMORY_BYTES) in user_data,
        "startupTimeoutFailsClosed": all(
            value in user_data
            for value in ["clickhouse_ready=0", "clickhouse_ready=1", "$clickhouse_ready", "-eq 1"]
        ),
        "noStaticAwsCredentials": all(value not in user_data for value in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]),
        "noLambda": not any(value["Type"] == "AWS::Lambda::Function" for value in resources.values()),
        "noDynamoDb": not any(value["Type"] == "AWS::DynamoDB::Table" for value in resources.values()),
        "noStepFunctions": not any(value["Type"] == "AWS::StepFunctions::StateMachine" for value in resources.values()),
        "noEventBridge": not any(value["Type"] == "AWS::Events::Rule" for value in resources.values()),
    }
    return {"checks": checks, "passed": all(checks.values()), "allowedActions": sorted(set(allowed_actions))}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--free-disk-kib", type=int, required=True)
    parser.add_argument("--filesystem-used-percent", type=float, required=True)
    parser.add_argument("--existing-session-volumes", type=int, default=0)
    args = parser.parse_args()

    phase6 = Path(__file__).resolve().parent
    root = phase6.parents[1]
    run_dir = args.run_dir.resolve()
    unit_evidence = run_dir / "evidence" / "unit"
    systemd_evidence = run_dir / "evidence" / "systemd"
    cdk_evidence = run_dir / "evidence" / "cdk"
    preflight_evidence = run_dir / "evidence" / "preflight"
    for path in [unit_evidence, systemd_evidence, cdk_evidence, preflight_evidence]:
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = "/tmp/phase6-pycache"
    env["CDK_DISABLE_VERSION_CHECK"] = "1"
    env["CDK_VALIDATION"] = "false"
    env["LOCAL_SESSION_ID"] = args.session_id
    env["LOCAL_RUN_DIR"] = str(run_dir)

    gates = [
        run_gate(
            "python-unit",
            ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=phase6,
            evidence=unit_evidence,
            env=env,
        ),
        run_gate("typescript-build", ["npm", "run", "build"], cwd=root, evidence=unit_evidence, env=env),
        run_gate(
            "cdk-unit",
            ["npx", "jest", "--runInBand", "test/perf-phase6-archive.test.ts"],
            cwd=root,
            evidence=unit_evidence,
            env=env,
        ),
        run_gate(
            "systemd-analyze",
            [
                "docker", "run", "--rm",
                "-v", f"{phase6 / 'systemd'}:/units:ro",
                "-v", f"{phase6 / 'tests/fixtures/docker.service'}:/etc/systemd/system/docker.service:ro",
                "ubuntu:24.04", "bash", "-lc",
                "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq systemd >/dev/null && systemd-analyze verify /etc/systemd/system/docker.service /units/loopad-phase6-archive.service /units/loopad-phase6-archive.timer /units/loopad-phase6-archive-run.timer",
            ],
            cwd=root,
            evidence=systemd_evidence,
            env=env,
        ),
        run_gate(
            "flock-overlap",
            [
                "docker", "run", "--rm",
                "-v", f"{phase6 / 'tests/flock_overlap.sh'}:/test/flock_overlap.sh:ro",
                "ubuntu:24.04", "bash", "/test/flock_overlap.sh",
            ],
            cwd=root,
            evidence=systemd_evidence,
            env=env,
        ),
        run_gate(
            "bootstrap-runtime",
            [
                "docker", "run", "--rm",
                "-v", f"{phase6 / 'tests/bootstrap_runtime.sh'}:/test/bootstrap_runtime.sh:ro",
                "ubuntu:24.04", "bash", "-lc",
                "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq passwd python3 python3-venv util-linux >/dev/null && bash /test/bootstrap_runtime.sh",
            ],
            cwd=root,
            evidence=systemd_evidence,
            env=env,
        ),
        run_gate(
            "compose-config",
            [
                "docker", "compose", "-p", args.project,
                "-f", str(phase6 / "docker-compose.yml"), "config",
            ],
            cwd=root,
            evidence=unit_evidence,
            env=env,
        ),
    ]

    cdk_out = cdk_evidence / "cdk.out-final"
    synth = run_gate(
        "synth-no-lookup",
        [
            "npx", "cdk", "synth", "--lookups=false", "--validation=false",
            "--app", "npx ts-node performance-tests/phase6-archive/synth.ts",
            "--output", str(cdk_out),
        ],
        cwd=root,
        evidence=cdk_evidence,
        env=env,
    )
    gates.append(synth)
    template_path = cdk_out / "LoopAdPerfPhase6ArchiveStack.template.json"
    review = static_review(template_path) if synth["passed"] else {"passed": False, "checks": {}}
    cdk_summary = {
        "schemaVersion": "1.0",
        "synth": synth,
        "templatePath": str(template_path),
        "templateSha256": sha256_file(template_path) if template_path.exists() else None,
        "staticReview": review,
        "awsLookups": False,
        "awsCalls": 0,
    }
    write_json(cdk_evidence / "summary.json", cdk_summary)

    fixture = phase6 / "price-fixtures/ap-northeast-2-20260716.json"
    cost = calculate(load_prices(fixture))
    write_json(preflight_evidence / "cost-model.json", cost)
    docker_memory = int(
        subprocess.run(
            ["docker", "info", "--format", "{{.MemTotal}}"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )
    preflight = evaluate_local_preflight(
        LocalPreflight(
            free_disk_bytes=args.free_disk_kib * 1024,
            filesystem_used_percent=args.filesystem_used_percent,
            docker_memory_bytes=docker_memory,
            container_memory_bytes=CONTAINER_MEMORY_BYTES,
            server_memory_bytes=SERVER_MEMORY_BYTES,
            query_memory_bytes=QUERY_MEMORY_BYTES,
            existing_session_volumes=args.existing_session_volumes,
        )
    )
    write_json(preflight_evidence / "local-preflight.json", preflight)
    overall = all(gate["passed"] for gate in gates) and review["passed"] and cost["passed"] and preflight["passed"]
    write_json(
        unit_evidence / "summary.json",
        {"schemaVersion": "1.0", "status": "passed" if overall else "failed", "gates": gates},
    )
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())

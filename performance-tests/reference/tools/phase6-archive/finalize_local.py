#!/usr/bin/env python3
"""Collect local logs, remove one exact Compose project, and prove label inventory zero."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run(command: List[str], *, env: Mapping[str, str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, env=env, check=check)


def inspect_records(kind: str, session_id: str, project: str, env: Mapping[str, str]) -> List[Dict[str, Any]]:
    if kind == "container":
        ids = run(
            ["docker", "ps", "-aq", "--filter", f"label=loopad.local_session_id={session_id}", "--filter", f"label=com.docker.compose.project={project}"],
            env=env,
            check=True,
        ).stdout.split()
        command = ["docker", "inspect"]
    elif kind == "volume":
        ids = run(
            ["docker", "volume", "ls", "-q", "--filter", f"label=loopad.local_session_id={session_id}", "--filter", f"label=com.docker.compose.project={project}"],
            env=env,
            check=True,
        ).stdout.split()
        command = ["docker", "volume", "inspect"]
    else:
        raise ValueError(kind)
    if not ids:
        return []
    return json.loads(run([*command, *ids], env=env, check=True).stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()

    env = dict(os.environ)
    env.update({"LOCAL_SESSION_ID": args.session_id, "LOCAL_RUN_DIR": str(args.run_dir.resolve())})
    evidence = args.run_dir.resolve() / "evidence" / "cleanup"
    evidence.mkdir(parents=True, exist_ok=True)
    before_containers = inspect_records("container", args.session_id, args.project, env)
    before_volumes = inspect_records("volume", args.session_id, args.project, env)
    for container in before_containers:
        container_id = str(container["Id"])
        name = str(container.get("Name", container_id)).lstrip("/")
        logs = run(["docker", "logs", "--timestamps", container_id], env=env)
        (evidence / f"{name}.stdout.log").write_text(logs.stdout, encoding="utf-8")
        (evidence / f"{name}.stderr.log").write_text(logs.stderr, encoding="utf-8")
    before = {
        "capturedAt": utc_now(),
        "containers": before_containers,
        "volumes": before_volumes,
    }
    (evidence / "inventory-before.json").write_text(
        json.dumps(before, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    command = [
        "docker", "compose", "-p", args.project, "-f", str(args.compose_file.resolve()),
        "down", "--volumes", "--remove-orphans",
    ]
    down = run(command, env=env)
    (evidence / "compose-down.stdout").write_text(down.stdout, encoding="utf-8")
    (evidence / "compose-down.stderr").write_text(down.stderr, encoding="utf-8")
    after_containers = inspect_records("container", args.session_id, args.project, env)
    after_volumes = inspect_records("volume", args.session_id, args.project, env)
    passed = down.returncode == 0 and not after_containers and not after_volumes
    result = {
        "schemaVersion": "1.0",
        "localSessionId": args.session_id,
        "composeProject": args.project,
        "command": command,
        "exitCode": down.returncode,
        "status": "passed" if passed else "failed",
        "containersBefore": len(before_containers),
        "volumesBefore": len(before_volumes),
        "containersRemaining": len(after_containers),
        "volumesRemaining": len(after_volumes),
        "containerInventoryAfter": after_containers,
        "volumeInventoryAfter": after_volumes,
        "verifiedAt": utc_now(),
        "dockerVolumePruneUsed": False,
    }
    (args.run_dir.resolve() / "cleanup-verification.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

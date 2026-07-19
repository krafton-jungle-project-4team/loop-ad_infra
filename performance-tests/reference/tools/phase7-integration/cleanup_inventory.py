#!/usr/bin/env python3
"""Fail unless every Docker resource carrying the Phase 7 session label is gone."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def docker_command(resource: str, session_id: str) -> list[str]:
    command = ["docker", resource, "ls", "-q", "--filter", f"label=loopad.local_session_id={session_id}"]
    if resource == "container":
        command.insert(3, "--all")
    return command


def docker_ids(resource: str, session_id: str) -> list[str]:
    command = docker_command(resource, session_id)
    completed = subprocess.run(command, capture_output=True, text=True, check=True)
    return sorted(line for line in completed.stdout.splitlines() if line)


def inventory(session_id: str) -> dict[str, object]:
    resources = {
        "containers": docker_ids("container", session_id),
        "volumes": docker_ids("volume", session_id),
        "networks": docker_ids("network", session_id),
    }
    return {
        "schemaVersion": "1.0",
        "status": "passed" if all(not values for values in resources.values()) else "failed",
        "sessionId": session_id,
        "measuredAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        **resources,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = inventory(args.session_id)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

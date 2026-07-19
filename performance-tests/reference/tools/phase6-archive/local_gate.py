#!/usr/bin/env python3
"""Host-side Compose gate runner with bounded resource evidence."""

from __future__ import annotations

import argparse
import math
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

MEMORY_STOP_PERCENT = 70.0
CPU_STOP_PERCENT = 70.0
FILESYSTEM_STOP_PERCENT = 80.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9.]+)(B|KiB|MiB|GiB|TiB)", value.strip())
    if not match:
        raise ValueError(f"unrecognized Docker size: {value}")
    multipliers = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}
    return int(float(match.group(1)) * multipliers[match.group(2)])


def nearest_rank_percentile(values: List[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


class ResourceMonitor:
    def __init__(self, project: str, output: Path, docker_memory_limit: int, docker_cpu_count: int) -> None:
        self.project = project
        self.output = output
        self.docker_memory_limit = docker_memory_limit
        self.docker_cpu_count = docker_cpu_count
        self.stop_event = threading.Event()
        self.samples: List[Dict[str, Any]] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> Dict[str, Any]:
        self.stop_event.set()
        self.thread.join(timeout=10)
        write_json(self.output, {"schemaVersion": "1.0", "samples": self.samples})
        peak_memory = max((item.get("memoryBytes", 0) for item in self.samples), default=0)
        peak_cpu = max((item.get("cpuPercent", 0.0) for item in self.samples), default=0.0)
        peak_filesystem = max((item.get("filesystemUsedPercent", 0.0) for item in self.samples), default=0.0)
        memory_percentages = [
            float(item["memoryPercentOfDockerLimit"])
            for item in self.samples
            if "memoryPercentOfDockerLimit" in item
        ]
        cpu_capacity_percentages = [
            float(item["cpuPercentOfDockerCapacity"])
            for item in self.samples
            if "cpuPercentOfDockerCapacity" in item
        ]
        return {
            "sampleCount": len(self.samples),
            "dockerMemoryLimitBytes": self.docker_memory_limit,
            "dockerCpuCount": self.docker_cpu_count,
            "peakMemoryBytes": peak_memory,
            "peakMemoryPercentOfDockerLimit": round(peak_memory * 100 / self.docker_memory_limit, 6),
            "p95MemoryPercentOfDockerLimit": round(nearest_rank_percentile(memory_percentages, 0.95), 6),
            "peakCpuPercentSum": round(peak_cpu, 6),
            "p95CpuPercentOfDockerCapacity": round(nearest_rank_percentile(cpu_capacity_percentages, 0.95), 6),
            "peakFilesystemUsedPercent": round(peak_filesystem, 6),
        }

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                ids = subprocess.run(
                    ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.split()
                memory = 0
                cpu = 0.0
                if ids:
                    output = subprocess.run(
                        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                    for line in output.splitlines():
                        row = json.loads(line)
                        memory += parse_bytes(str(row["MemUsage"]).split("/")[0].strip())
                        cpu += float(str(row["CPUPerc"]).rstrip("%"))
                usage = shutil.disk_usage(Path.cwd())
                used_percent = (usage.total - usage.free) * 100 / usage.total
                self.samples.append(
                    {
                        "at": utc_now(),
                        "memoryBytes": memory,
                        "memoryPercentOfDockerLimit": round(memory * 100 / self.docker_memory_limit, 6),
                        "cpuPercent": round(cpu, 6),
                        "cpuPercentOfDockerCapacity": round(cpu / self.docker_cpu_count, 6),
                        "filesystemUsedPercent": round(used_percent, 6),
                    }
                )
            except BaseException as error:
                self.samples.append({"at": utc_now(), "sampleError": str(error)})
            self.stop_event.wait(2)


def compose_command(compose_file: Path, project: str, *args: str) -> List[str]:
    return ["docker", "compose", "-p", project, "-f", str(compose_file), *args]


def inspect_runtime(project: str) -> Dict[str, Any]:
    ids = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    containers = []
    for container_id in ids:
        document = json.loads(
            subprocess.run(
                ["docker", "inspect", container_id],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )[0]
        containers.append(
            {
                "id": container_id,
                "name": document["Name"].lstrip("/"),
                "restartCount": int(document.get("RestartCount", 0)),
                "oomKilled": bool(document["State"].get("OOMKilled", False)),
                "status": document["State"].get("Status"),
            }
        )
    return {
        "containers": containers,
        "restartCount": sum(item["restartCount"] for item in containers),
        "oomCount": sum(1 for item in containers if item["oomKilled"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", choices=["small", "faults", "1m", "15m"], required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--code-sha256", required=True)
    parser.add_argument("--evidence-name")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    compose_file = Path(__file__).with_name("docker-compose.yml").resolve()
    run_dir = args.run_dir.resolve()
    default_evidence_name = {"1m": "scale-1m", "15m": "scale-15m"}.get(args.gate, args.gate)
    evidence = run_dir / "evidence" / (args.evidence_name or default_evidence_name)
    evidence.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "LOCAL_SESSION_ID": args.session_id,
            "LOCAL_RUN_DIR": str(run_dir),
        }
    )
    started_at = utc_now()
    up = compose_command(compose_file, args.project, "up", "-d", "--wait", "--wait-timeout", "180", "clickhouse", "localstack")
    up_result = subprocess.run(up, cwd=root, env=env, text=True, capture_output=True, check=False)
    (evidence / "compose-up.stdout").write_text(up_result.stdout, encoding="utf-8")
    (evidence / "compose-up.stderr").write_text(up_result.stderr, encoding="utf-8")
    if up_result.returncode != 0:
        write_json(
            evidence / "gate-summary.json",
            {"status": "failed", "gate": args.gate, "startedAt": started_at, "finishedAt": utc_now(), "error": "compose startup failed"},
        )
        return 1

    result_inside = f"/work/run/evidence/{evidence.name}/result.json"
    command = compose_command(
        compose_file,
        args.project,
        "--profile",
        "tools",
        "run",
        "--rm",
        "worker",
        "integration_runner.py",
        "--gate",
        args.gate,
        "--run-id",
        args.run_id,
        "--output",
        result_inside,
        "--image-digest",
        args.image_digest,
        "--code-sha256",
        args.code_sha256,
    )
    docker_capacity = (
        subprocess.run(
            ["docker", "info", "--format", "{{.MemTotal}} {{.NCPU}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    )
    docker_memory_limit, docker_cpu_count = (int(value) for value in docker_capacity)
    if docker_memory_limit <= 0 or docker_cpu_count <= 0:
        raise RuntimeError("Docker reported non-positive CPU or memory capacity")
    monitor = ResourceMonitor(
        args.project,
        evidence / "resource-samples.json",
        docker_memory_limit,
        docker_cpu_count,
    )
    monitor.start()
    completed = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, check=False)
    resources = monitor.stop()
    (evidence / "command.stdout").write_text(completed.stdout, encoding="utf-8")
    (evidence / "command.stderr").write_text(completed.stderr, encoding="utf-8")
    runtime = inspect_runtime(args.project)
    result_path = evidence / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
    guard_passed = (
        resources["p95MemoryPercentOfDockerLimit"] < MEMORY_STOP_PERCENT
        and resources["p95CpuPercentOfDockerCapacity"] < CPU_STOP_PERCENT
        and resources["peakFilesystemUsedPercent"] < FILESYSTEM_STOP_PERCENT
        and runtime["oomCount"] == 0
        and runtime["restartCount"] == 0
    )
    passed = completed.returncode == 0 and result is not None and result.get("status") == "passed" and guard_passed
    summary = {
        "schemaVersion": "1.0",
        "gate": args.gate,
        "status": "passed" if passed else "failed",
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "exitCode": completed.returncode,
        "command": command,
        "resources": resources,
        "runtime": runtime,
        "guard": {
            "memoryP95Below70Percent": resources["p95MemoryPercentOfDockerLimit"] < MEMORY_STOP_PERCENT,
            "cpuP95Below70Percent": resources["p95CpuPercentOfDockerCapacity"] < CPU_STOP_PERCENT,
            "filesystemBelow80Percent": resources["peakFilesystemUsedPercent"] < FILESYSTEM_STOP_PERCENT,
            "oomZero": runtime["oomCount"] == 0,
            "restartZero": runtime["restartCount"] == 0,
            "passed": guard_passed,
        },
        "resultPath": str(result_path),
    }
    write_json(evidence / "gate-summary.json", summary)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

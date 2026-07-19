#!/usr/bin/env python3
"""Pure local preflight checks. This module never calls AWS or Docker."""

from __future__ import annotations

from dataclasses import asdict, dataclass

GIB = 1024 ** 3
CONTAINER_MEMORY_BYTES = 5 * GIB
SERVER_MEMORY_BYTES = 5_261_334_937
QUERY_MEMORY_BYTES = 9 * GIB // 2


@dataclass(frozen=True)
class LocalPreflight:
    free_disk_bytes: int
    filesystem_used_percent: float
    docker_memory_bytes: int
    container_memory_bytes: int
    server_memory_bytes: int
    query_memory_bytes: int
    existing_session_volumes: int


def evaluate_local_preflight(value: LocalPreflight) -> dict[str, object]:
    checks = {
        "freeDiskAtLeast30GiB": value.free_disk_bytes >= 30 * GIB,
        "filesystemBelow80Percent": value.filesystem_used_percent < 80.0,
        "dockerCanContainContainer": value.docker_memory_bytes > value.container_memory_bytes,
        "containerMemoryEquals5GiB": value.container_memory_bytes == CONTAINER_MEMORY_BYTES,
        "serverMemoryEquals4Point9GiB": value.server_memory_bytes == SERVER_MEMORY_BYTES,
        "queryMemoryEquals4Point5GiB": value.query_memory_bytes == QUERY_MEMORY_BYTES,
        "queryBelowServerBelowContainer": (
            value.query_memory_bytes < value.server_memory_bytes < value.container_memory_bytes
        ),
        "sessionStartsWithoutVolumes": value.existing_session_volumes == 0,
    }
    return {
        "schemaVersion": "1.0",
        "input": asdict(value),
        "checks": checks,
        "passed": all(checks.values()),
        "awsCalls": 0,
    }

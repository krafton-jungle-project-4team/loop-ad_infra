#!/usr/bin/env python3
"""Exact-label local cleanup inventory helpers."""

from __future__ import annotations

from typing import Iterable, Mapping


def select_owned(
    records: Iterable[Mapping[str, object]],
    *,
    local_session_id: str,
    compose_project: str,
) -> list[Mapping[str, object]]:
    selected = []
    for record in records:
        labels = record.get("Labels", {})
        if not isinstance(labels, Mapping):
            continue
        if (
            labels.get("loopad.local_session_id") == local_session_id
            and labels.get("com.docker.compose.project") == compose_project
        ):
            selected.append(record)
    return selected


def cleanup_result(
    containers: Iterable[Mapping[str, object]],
    volumes: Iterable[Mapping[str, object]],
    *,
    local_session_id: str,
    compose_project: str,
) -> dict[str, object]:
    owned_containers = select_owned(
        containers,
        local_session_id=local_session_id,
        compose_project=compose_project,
    )
    owned_volumes = select_owned(
        volumes,
        local_session_id=local_session_id,
        compose_project=compose_project,
    )
    return {
        "schemaVersion": "1.0",
        "localSessionId": local_session_id,
        "composeProject": compose_project,
        "containersRemaining": len(owned_containers),
        "volumesRemaining": len(owned_volumes),
        "containerIds": [record.get("Id") for record in owned_containers],
        "volumeNames": [record.get("Name") for record in owned_volumes],
        "passed": not owned_containers and not owned_volumes,
    }

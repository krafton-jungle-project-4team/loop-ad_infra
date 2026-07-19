#!/usr/bin/env python3
"""Validate ECS API value ranges in a synthesized Phase 7 CloudFormation template."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def validate_template(template: dict[str, Any]) -> dict[str, Any]:
    health_checks = []
    schema_guard = []
    for logical_id, resource in template.get("Resources", {}).items():
        if resource.get("Type") != "AWS::ECS::TaskDefinition":
            continue
        for container in resource.get("Properties", {}).get("ContainerDefinitions", []):
            health = container.get("HealthCheck")
            if health is None:
                continue
            retries = health.get("Retries")
            item = {
                "taskDefinition": logical_id,
                "container": container.get("Name"),
                "retries": retries,
            }
            health_checks.append(item)
            if not isinstance(retries, int) or isinstance(retries, bool) or not 1 <= retries <= 10:
                raise ValueError(
                    f"{logical_id}/{container.get('Name')} HealthCheck.Retries must be in 1..10"
                )
            if container.get("Name") == "schema-guard":
                schema_guard.append(item)
    if len(schema_guard) != 1 or schema_guard[0]["retries"] > 10:
        raise ValueError("exactly one schema-guard with HealthCheck.Retries <= 10 is required")
    return {
        "schemaVersion": 1,
        "taskDefinitionsChecked": sum(
            1 for resource in template.get("Resources", {}).values()
            if resource.get("Type") == "AWS::ECS::TaskDefinition"
        ),
        "healthChecks": health_checks,
        "schemaGuardRetries": schema_guard[0]["retries"],
        "passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True, type=Path)
    args = parser.parse_args()
    document = json.loads(args.template.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("CloudFormation template must be a JSON object")
    print(json.dumps(validate_template(document), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

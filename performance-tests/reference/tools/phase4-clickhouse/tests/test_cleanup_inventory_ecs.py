from __future__ import annotations

import json
from pathlib import Path

import pytest

from performance_tests_import import load_phase4_module


INVENTORY = load_phase4_module("cleanup_inventory_ecs.py", "phase4_cleanup_inventory_ecs")
CLEANUP = load_phase4_module("prepare_cleanup_ecs.py", "phase4_prepare_cleanup_ecs")


def test_tag_map_accepts_aws_tag_key_casing() -> None:
    assert INVENTORY.tag_map([
        {"Key": "RunId", "Value": "run"},
        {"key": "SessionId", "value": "session"},
    ]) == {"RunId": "run", "SessionId": "session"}


def test_owned_requires_both_run_and_session() -> None:
    tags = {"RunId": "run", "SessionId": "session"}
    assert INVENTORY.owned(tags, "run", "session") is True
    assert INVENTORY.owned(tags, "other", "session") is False
    assert INVENTORY.owned({"RunId": "run"}, "run", "session") is False


class TemplateClient:
    def __init__(self, template: dict) -> None:
        self.template = template

    def get_template(self, StackName: str) -> dict:
        assert StackName == "LoopAdPerfPhase4ClickHouseEcsStack"
        return {"TemplateBody": self.template}


def test_stack_ownership_falls_back_to_exact_template_identity() -> None:
    run_id = "run_20260716_120000_phase4_clickhouse_ecs"
    session_id = "phase4-clickhouse-ecs-20260716T120000Z"
    stack = {"StackName": "LoopAdPerfPhase4ClickHouseEcsStack", "Tags": []}
    template = {
        "Outputs": {
            "RunId": {"Value": run_id},
            "SessionId": {"Value": session_id},
        }
    }
    assert INVENTORY.stack_owned(TemplateClient(template), stack, run_id, session_id)
    assert not INVENTORY.stack_owned(
        TemplateClient(template),
        stack,
        "run_20260716_120001_phase4_clickhouse_ecs",
        session_id,
    )


def test_cleanup_chunk_sizes_are_bounded() -> None:
    values = [{"Key": str(index)} for index in range(2_001)]
    chunks = list(CLEANUP.chunks(values, 1_000))
    assert [len(chunk) for chunk in chunks] == [1_000, 1_000, 1]


def test_partial_deploy_inventory_bundle_derives_only_fixed_identifiers(tmp_path: Path) -> None:
    run_id = "run_20260716_120000_phase4_clickhouse_ecs"
    session_id = "phase4-clickhouse-ecs-20260716T120000Z"
    (tmp_path / "run.json").write_text(json.dumps({
        "runId": run_id,
        "sessionId": session_id,
        "account": "742711170910",
    }), encoding="utf-8")

    bundle = INVENTORY.load_inventory_bundle(tmp_path)

    assert bundle.outputs["ConsumerClusterName"] == f"loopad-{run_id}-consumer"
    assert bundle.outputs["ConsumerRepositoryName"] == (
        f"loop-ad/perf-phase4-clickhouse/{run_id}"
    )
    assert "FailureBucketName" not in bundle.outputs


def test_inventory_bundle_rejects_runtime_output_ownership_mismatch(tmp_path: Path) -> None:
    run_id = "run_20260716_120000_phase4_clickhouse_ecs"
    session_id = "phase4-clickhouse-ecs-20260716T120000Z"
    (tmp_path / "run.json").write_text(json.dumps({
        "runId": run_id,
        "sessionId": session_id,
        "account": "742711170910",
    }), encoding="utf-8")
    (tmp_path / "cdk-outputs.json").write_text(json.dumps({
        INVENTORY.RUNTIME_STACK_NAME: {
            "RunId": "run_20260716_120001_phase4_clickhouse_ecs",
            "SessionId": session_id,
        },
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="ownership"):
        INVENTORY.load_inventory_bundle(tmp_path)

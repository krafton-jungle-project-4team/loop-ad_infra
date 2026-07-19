from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "preflight_ecs.py"
SPEC = importlib.util.spec_from_file_location("phase4_preflight_ecs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_root_requires_explicit_acceptance() -> None:
    assert MODULE.operator_check("arn:aws:iam::742711170910:root", False).passed is False
    assert MODULE.operator_check("arn:aws:iam::742711170910:root", True).passed is True
    assert MODULE.operator_check("arn:aws:iam::742711170910:role/operator", False).passed is True


def test_price_age_is_utc_and_never_negative() -> None:
    now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    assert MODULE.price_age_seconds({"asOf": "2026-07-16T09:59:00Z"}, now) == 60
    assert MODULE.price_age_seconds({"asOf": "2026-07-16T10:01:00Z"}, now) == 0


def test_cost_checks_reject_lambda_model_and_enforce_caps() -> None:
    passing = {
        "workload": "phase4-kinesis-ecs-ec2-clickhouse",
        "operationalMaximumUsd": "11.9",
        "maximumIncludingCleanupUsd": "14.9",
        "cleanupReserveUsd": "3.0",
    }
    assert all(check.passed for check in MODULE.cost_checks(passing))
    assert MODULE.cost_checks(dict(passing, workload=None))[0].passed is False
    assert MODULE.cost_checks(dict(passing, operationalMaximumUsd="17.0"))[1].passed is False
    assert MODULE.cost_checks(dict(passing, maximumIncludingCleanupUsd="20.000001"))[2].passed is False


def test_quota_value_requires_one_exact_named_quota() -> None:
    quotas = [
        {"QuotaName": "Clusters per Region", "Value": 10.0},
        {"QuotaName": "Services per cluster", "Value": 5_000.0},
    ]
    assert MODULE.quota_value(quotas, {"Clusters per Region"}) == 10.0
    with pytest.raises(ValueError, match="expected one quota"):
        MODULE.quota_value(quotas, {"Unknown"})


def test_ecs_default_quota_names_are_current_and_unambiguous() -> None:
    quotas = [
        {"QuotaName": "Clusters per account", "Value": 10_000},
        {"QuotaName": "Container instances per cluster", "Value": 5_000},
        {"QuotaName": "Tasks per service", "Value": 5_000},
        {"QuotaName": "Tasks in PROVISIONING state per cluster", "Value": 500},
    ]
    assert MODULE.quota_value(
        quotas, {"Clusters per Region", "Clusters per account"}
    ) == 10_000
    assert MODULE.quota_value(quotas, {"Container instances per cluster"}) == 5_000
    assert MODULE.quota_value(quotas, {"Tasks per service"}) == 5_000
    assert MODULE.quota_value(
        quotas, {"Tasks in PROVISIONING state per cluster"}
    ) == 500


def test_ecr_current_quota_names_cover_one_image_push() -> None:
    quotas = [
        {"QuotaName": name, "Value": value}
        for name, value in MODULE.ECR_REQUIRED_PUSH_QUOTAS.items()
    ]
    observed = MODULE.named_quota_values(quotas)
    assert all(
        observed[name] >= minimum
        for name, minimum in MODULE.ECR_REQUIRED_PUSH_QUOTAS.items()
    )


def test_dynamodb_current_table_quota_name_is_accepted() -> None:
    quotas = [{"QuotaName": "Maximum number of tables", "Value": 2_500}]
    assert MODULE.quota_value(
        quotas, {"Tables per Region", "Maximum number of tables"}
    ) == 2_500


def test_parse_utc_requires_timezone() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        MODULE.parse_utc("2026-07-16T10:00:00")

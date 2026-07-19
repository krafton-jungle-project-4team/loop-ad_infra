from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "cost_model_ecs.py"
SPEC = importlib.util.spec_from_file_location("phase4_cost_model_ecs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PRICES = {
    "schemaVersion": 1,
    "asOf": "2026-07-16T10:01:20Z",
    "region": "ap-northeast-2",
    "source": "test",
    "prices": {
        "clickHouseR7g2xlargeHour": 0.5168,
        "consumerHostC7gLargeHour": 0.0816,
        "producerC7g2xlargeHour": 0.3264,
        "kinesisProvisionedShardHour": 0.0185,
        "kinesisPutPayloadUnit": 2.04e-8,
        "ebsGp3GbMonth": 0.0912,
        "ebsGp3ThroughputGibpsMonth": 46.6944,
        "secretsManagerSecretMonth": 0.4,
        "secretsManagerApiRequest": 0.000005,
        "vpcEndpointHour": 0.013,
        "vpcEndpointDataGb": 0.01,
        "publicIpv4Hour": 0.005,
        "cloudWatchMetricMonth": 0.3,
        "cloudWatchLogIngestGb": 0.76,
        "cloudWatchLogStorageGbMonth": 0.0314,
        "cloudWatchGetMetricDataMetric": 0.00001,
        "ecrStorageGbMonth": 0.1,
        "dynamoDbReadRequestUnit": 0.0000001355,
        "dynamoDbWriteRequestUnit": 0.00000068,
        "dynamoDbStorageGbMonth": 0.27075,
        "s3StandardStorageGbMonth": 0.025,
        "s3Tier1Request": 0.0000045,
        "s3Tier2Request": 0.00000035,
    },
}


def test_ecs_cost_model_has_no_lambda_and_passes_current_gate() -> None:
    model = MODULE.build_cost_model(PRICES)

    names = {component["name"] for component in model["components"]}
    assert all("Lambda" not in name for name in names)
    assert "ECS consumer hosts c7g.large" in names
    assert model["assumptions"]["consumerHosts"] == 2
    assert model["assumptions"]["interfaceEndpoints"] == 9
    assert model["assumptions"]["kclCustomMetricSeriesUpperBound"] == 4_200
    assert model["assumptions"]["kclPerShardMetricCount"] == 11
    assert model["assumptions"]["kclUniqueWorkersUpperBound"] == 3
    assert model["assumptions"]["hostMemoryCustomMetricSeriesUpperBound"] == 3
    assert model["assumptions"]["cloudWatchLogIngestGiBUpperBound"] == "0.25"
    assert "CloudWatch custom metrics" in names
    assert "CloudWatch GetMetricData" in names
    assert "ECR image storage" in names
    assert "private interface endpoint data" in names
    assert "DynamoDB on-demand reads" in names
    assert "DynamoDB on-demand writes" in names
    assert "S3 Tier 1 requests" in names
    assert model["assumptions"]["vpcEndpointDataGiBUpperBound"] == "25"
    assert model["assumptions"]["dynamoDbWriteRequestUnitsUpperBound"] == 500_000
    assert Decimal(model["operationalMaximumUsd"]) < Decimal("17")
    assert Decimal(model["cleanupReserveUsd"]) == Decimal("3")
    assert Decimal(model["maximumIncludingCleanupUsd"]) <= Decimal("20")
    assert model["gates"]["pass"] is True


def test_high_kinesis_price_closes_both_cost_gates() -> None:
    prices = deepcopy(PRICES)
    prices["prices"]["kinesisProvisionedShardHour"] = 0.12

    model = MODULE.build_cost_model(prices)

    assert model["gates"]["operationalMaximumBelowNewLoadStop"] is False
    assert model["gates"]["maximumIncludingCleanupAtMostHardCap"] is False
    assert model["gates"]["pass"] is False


def test_rejects_wrong_region_and_missing_host_price() -> None:
    wrong_region = deepcopy(PRICES)
    wrong_region["region"] = "us-east-1"
    with pytest.raises(ValueError, match="ap-northeast-2"):
        MODULE.build_cost_model(wrong_region)

    missing = deepcopy(PRICES)
    del missing["prices"]["consumerHostC7gLargeHour"]
    with pytest.raises(ValueError, match="consumerHostC7gLargeHour"):
        MODULE.build_cost_model(missing)

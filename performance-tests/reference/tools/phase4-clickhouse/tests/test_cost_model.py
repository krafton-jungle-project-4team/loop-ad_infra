from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "cost_model.py"
SPEC = importlib.util.spec_from_file_location("phase4_cost_model", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ESM_INVOCATION_BOUNDARY_ALLOWANCE = MODULE.ESM_INVOCATION_BOUNDARY_ALLOWANCE
FULL_LOAD_RECORDS = MODULE.FULL_LOAD_RECORDS
SAFE_RECORDS_PER_INVOCATION = MODULE.SAFE_RECORDS_PER_INVOCATION
VALIDATION_RECORD_ALLOWANCE = MODULE.VALIDATION_RECORD_ALLOWANCE
build_cost_model = MODULE.build_cost_model
ceil_div = MODULE.ceil_div


PRICES = {
    "schemaVersion": 1,
    "asOf": "2026-07-16T10:01:20Z",
    "region": "ap-northeast-2",
    "source": "test",
    "prices": {
        "clickHouseR7g2xlargeHour": 0.5168,
        "producerC7g2xlargeHour": 0.3264,
        "kinesisProvisionedShardHour": 0.0185,
        "kinesisPutPayloadUnit": 2.04e-8,
        "ebsGp3GbMonth": 0.0912,
        "ebsGp3ThroughputGibpsMonth": 46.6944,
        "lambdaArmGbSecond": 0.0000133334,
        "lambdaArmRequest": 2e-7,
        "secretsManagerSecretMonth": 0.4,
        "secretsManagerApiRequest": 0.000005,
        "vpcEndpointHour": 0.013,
        "publicIpv4Hour": 0.005,
    },
}


def test_current_prices_pass_both_cost_gates() -> None:
    model = build_cost_model(PRICES)

    expected_invocations = (
        ceil_div(
            FULL_LOAD_RECORDS + VALIDATION_RECORD_ALLOWANCE,
            SAFE_RECORDS_PER_INVOCATION,
        )
        + ESM_INVOCATION_BOUNDARY_ALLOWANCE
    )
    assert model["assumptions"]["lambdaInvocationUpperBound"] == expected_invocations
    assert Decimal(model["operationalMaximumUsd"]) < Decimal("12")
    assert Decimal(model["cleanupReserveUsd"]) == Decimal("3")
    assert Decimal(model["maximumIncludingCleanupUsd"]) <= Decimal("15")
    assert model["gates"]["pass"] is True


def test_higher_lambda_price_closes_gate() -> None:
    prices = deepcopy(PRICES)
    prices["prices"]["lambdaArmGbSecond"] = 0.001

    model = build_cost_model(prices)

    assert model["gates"]["operationalMaximumBelowNewLoadStop"] is False
    assert model["gates"]["maximumIncludingCleanupAtMostHardCap"] is False
    assert model["gates"]["pass"] is False


def test_rejects_wrong_region_and_missing_price() -> None:
    wrong_region = deepcopy(PRICES)
    wrong_region["region"] = "us-east-1"
    with pytest.raises(ValueError, match="ap-northeast-2"):
        build_cost_model(wrong_region)

    missing = deepcopy(PRICES)
    del missing["prices"]["publicIpv4Hour"]
    with pytest.raises(ValueError, match="publicIpv4Hour"):
        build_cost_model(missing)

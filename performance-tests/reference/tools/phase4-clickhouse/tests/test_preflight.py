from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "preflight.py"
SPEC = importlib.util.spec_from_file_location("phase4_preflight", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_root_requires_explicit_acceptance() -> None:
    rejected = MODULE.operator_check("arn:aws:iam::742711170910:root", False)
    accepted = MODULE.operator_check("arn:aws:iam::742711170910:root", True)
    role = MODULE.operator_check("arn:aws:iam::742711170910:role/operator", False)

    assert rejected.passed is False
    assert accepted.passed is True
    assert role.passed is True


def test_price_age_is_utc_and_never_negative() -> None:
    now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    assert MODULE.price_age_seconds({"asOf": "2026-07-16T09:59:00Z"}, now) == 60
    assert MODULE.price_age_seconds({"asOf": "2026-07-16T10:01:00Z"}, now) == 0


def test_cost_checks_enforce_both_thresholds() -> None:
    passing = {
        "operationalMaximumUsd": "11.9",
        "maximumIncludingCleanupUsd": "14.9",
        "cleanupReserveUsd": "3.0",
    }
    assert all(check.passed for check in MODULE.cost_checks(passing))

    load_closed = dict(passing, operationalMaximumUsd="12.0")
    assert MODULE.cost_checks(load_closed)[0].passed is False

    cap_closed = dict(passing, maximumIncludingCleanupUsd="15.000001")
    assert MODULE.cost_checks(cap_closed)[1].passed is False


def test_parse_utc_requires_timezone() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        MODULE.parse_utc("2026-07-16T10:00:00")

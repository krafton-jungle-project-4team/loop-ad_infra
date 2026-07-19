from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "local_integration.py"
SPEC = importlib.util.spec_from_file_location("local_integration", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    "value",
    ["http://127.0.0.1:4566", "http://localhost:8123", "http://[::1]:8123"],
)
def test_loopback_endpoint_guard_accepts_only_explicit_local_http(value: str) -> None:
    assert MODULE.assert_loopback_endpoint(value, "test") == value


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:4566",
        "https://kinesis.ap-northeast-2.amazonaws.com",
        "http://203.0.113.1:4566",
        "http://user:password@127.0.0.1:4566",
        "http://127.0.0.1:4566?target=aws",
    ],
)
def test_loopback_endpoint_guard_rejects_nonlocal_or_credentialed_urls(value: str) -> None:
    with pytest.raises(ValueError):
        MODULE.assert_loopback_endpoint(value, "test")


def test_lambda_record_preserves_raw_bytes_and_sequence() -> None:
    source = b'{"event_id":"event-1"}'
    record = MODULE.direct_lambda_record(source, 123, "partition-1")
    assert base64.b64decode(record["kinesis"]["data"]) == source
    assert record["kinesis"]["sequenceNumber"] == "123"
    assert record["kinesis"]["partitionKey"] == "partition-1"

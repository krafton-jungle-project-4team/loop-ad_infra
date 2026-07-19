from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "verify_producer_contract.py"
SPEC = importlib.util.spec_from_file_location("verify_producer_contract", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_qualified_producer_contract_matches_repository() -> None:
    result = MODULE.verify_contract()
    assert result["status"] == "passed"
    assert result["candidate"] == "c7g.2xlarge"
    assert result["workers"] == 8
    assert result["expectedRecords"] == 15_000_000


def test_payload_hash_is_the_qualified_hash() -> None:
    assert MODULE.sha256(MODULE.PAYLOAD) == MODULE.EXPECTED_PAYLOAD_SHA256

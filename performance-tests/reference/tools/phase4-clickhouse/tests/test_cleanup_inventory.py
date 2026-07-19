from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "cleanup_inventory.py"
SPEC = importlib.util.spec_from_file_location("phase4_cleanup_inventory", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_ownership_requires_both_run_and_session() -> None:
    inventory = MODULE.Inventory("run-1", "session-1", "123456789012")

    assert inventory._owned({"RunId": "run-1", "SessionId": "session-1"}) is True
    assert inventory._owned({"RunId": "run-1", "SessionId": "other"}) is False
    assert inventory._owned({"RunId": "run-1"}) is False


def test_tag_filters_bind_both_ownership_dimensions() -> None:
    inventory = MODULE.Inventory("run-1", "session-1", "123456789012")

    assert inventory._tag_filters() == [
        {"Key": "RunId", "Values": ["run-1"]},
        {"Key": "SessionId", "Values": ["session-1"]},
    ]

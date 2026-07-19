from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType


PHASE4_DIR = Path(__file__).resolve().parents[1]


def load_phase4_module(filename: str, name: str) -> ModuleType:
    if str(PHASE4_DIR) not in sys.path:
        sys.path.insert(0, str(PHASE4_DIR))
    spec = importlib.util.spec_from_file_location(name, PHASE4_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

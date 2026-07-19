from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


AWS_DIR = Path(__file__).resolve().parents[1] / "aws"
sys.path.insert(0, str(AWS_DIR))

from runner import initialize  # noqa: E402


RUN_ID = "run_20260718_010203_phase7_integration"
SESSION_ID = "phase7-integration-20260718T010203Z"
TREE = "a" * 64


def write(path: Path, document: dict[str, object]) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class Phase7InitializeContractTest(unittest.TestCase):
    def artifacts(self, root: Path) -> tuple[Path, Path, Path, Path]:
        preflight = {
            "passed": True,
            "imageState": "prepared",
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "handoff": {"implementationTreeSha256": TREE},
        }
        image_manifest = {
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "runtimeDeployed": False,
            "implementationTreeSha256": TREE,
            "images": [
                {"role": role, "architecture": architecture, "digest": f"sha256:{digit * 64}"}
                for role, architecture, digit in (
                    ("collector", "linux/amd64", "1"),
                    ("consumer", "linux/arm64", "2"),
                    ("archive", "linux/arm64", "3"),
                )
            ],
        }
        cost = {"passed": True, "operationalMaximumUsd": "30", "maximumIncludingCleanupUsd": "39"}
        identity = {
            "runId": RUN_ID,
            "sessionId": SESSION_ID,
            "identityMode": "balanced-pool-sampled-with-replacement",
            "predeclaredBeforeDeploy": True,
            "userApproved": True,
            "selectionWithReplacement": True,
            "warmupScorePoolsSeparated": True,
            "balancedShardCount": 120,
            "fixturePoolRows": 480,
            "archive": {"equivalenceAndDropContractUnchanged": True},
            "source": {"implementationTreeSha256": TREE},
        }
        return (
            write(root / "preflight.json", preflight),
            write(root / "images.json", image_manifest),
            write(root / "cost.json", cost),
            write(root / "identity.json", identity),
        )

    def test_initialize_binds_all_predeploy_artifacts_and_phase5_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.artifacts(root)
            result = initialize(root / "run", RUN_ID, SESSION_ID, *paths)
            self.assertEqual("skipped", result["phase5"])
            self.assertEqual(64, len(result["identityContractSha256"]))
            self.assertTrue((root / "run" / "inputs" / "identity-contract.json").is_file())

    def test_initialize_rejects_cross_run_preflight_before_creating_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = list(self.artifacts(root))
            preflight = json.loads(paths[0].read_text(encoding="utf-8"))
            preflight["runId"] = "run_20260718_010204_phase7_integration"
            write(paths[0], preflight)
            with self.assertRaisesRegex(RuntimeError, "another Run ID"):
                initialize(root / "run", RUN_ID, SESSION_ID, *paths)
            self.assertFalse((root / "run").exists())

    def test_initialize_rejects_transitive_implementation_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = list(self.artifacts(root))
            identity = json.loads(paths[3].read_text(encoding="utf-8"))
            identity["source"]["implementationTreeSha256"] = "b" * 64
            write(paths[3], identity)
            with self.assertRaisesRegex(RuntimeError, "implementation hashes"):
                initialize(root / "run", RUN_ID, SESSION_ID, *paths)


if __name__ == "__main__":
    unittest.main()

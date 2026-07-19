from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PHASE7 = Path(__file__).resolve().parents[1]
ROOT = PHASE7.parents[1]
sys.path.insert(0, str(PHASE7))

from finalize_evidence import implementation_digest, implementation_files  # noqa: E402


REQUIRED_TRANSITIVE_INPUTS = {
    "package-lock.json",
    "cdk.json",
    "tsconfig.json",
    "bin/loop-ad_aws_cdk.ts",
    "src/cdk-app.ts",
    "src/cdk-app-config.ts",
    "src/perf-phase1-kinesis-stack.ts",
    "src/perf-phase7-integration-config.ts",
    "src/perf-phase7-integration-stack.ts",
    "performance-tests/phase1-kinesis/aws-observation-retry.mjs",
    "performance-tests/phase1-kinesis/connection-path-destination.mjs",
    "performance-tests/phase1-kinesis/invoke-oha.mjs",
    "performance-tests/phase1-kinesis/oha-load-contract.mjs",
    "performance-tests/phase1-kinesis/oha12k-aggregate.mjs",
    "performance-tests/phase1-kinesis/run-ec2-oha-worker.sh",
    "performance-tests/phase4-clickhouse/producer-env/pyproject.toml",
    "performance-tests/phase4-clickhouse/producer-env/uv.lock",
    "performance-tests/phase7-integration/clickhouse-config/memory.xml",
    "performance-tests/phase7-integration/topology-contract.json",
}


class FinalizeManifestTest(unittest.TestCase):
    def test_manifest_covers_every_phase7_transitive_runtime_and_build_input(self) -> None:
        relative = {path.relative_to(ROOT).as_posix() for path in implementation_files(ROOT)}
        self.assertTrue(REQUIRED_TRANSITIVE_INPUTS.issubset(relative), REQUIRED_TRANSITIVE_INPUTS - relative)
        self.assertTrue(any(path.startswith("performance-tests/phase4-clickhouse/consumer/src/test/") for path in relative))
        self.assertTrue(any(path.startswith("performance-tests/phase7-integration/aws/") for path in relative))

    def test_digest_changes_when_any_declared_input_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").mkdir()
            (root / "a/one.txt").write_text("one", encoding="utf-8")
            (root / "two.txt").write_text("two", encoding="utf-8")
            before, _ = implementation_digest(root, ("a", "two.txt"))
            (root / "a/one.txt").write_text("changed", encoding="utf-8")
            after_directory_change, _ = implementation_digest(root, ("a", "two.txt"))
            self.assertNotEqual(before, after_directory_change)
            (root / "two.txt").write_text("changed", encoding="utf-8")
            after_file_change, _ = implementation_digest(root, ("a", "two.txt"))
            self.assertNotEqual(after_directory_change, after_file_change)


if __name__ == "__main__":
    unittest.main()

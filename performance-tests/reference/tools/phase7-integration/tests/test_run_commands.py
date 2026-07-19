from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


AWS_DIR = Path(__file__).resolve().parents[1] / "aws"
sys.path.insert(0, str(AWS_DIR))

import build_run_commands  # noqa: E402
from runner import STAGES, STAGE_TIMEOUT_SECONDS  # noqa: E402


RUN_ID = "run_20260718_190000_phase7_integration"
SESSION_ID = "phase7-integration-20260718T190000Z"


class RunCommandContractTest(unittest.TestCase):
    def test_command_set_is_hash_sealed_exactly_once_before_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "inputs").mkdir()
            (run_dir / "run.json").write_text(json.dumps({
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "status": "initialized",
                "attemptedStages": [],
                "commandSetRequired": True,
                "commandSetSha256": None,
            }), encoding="utf-8")
            commands = {
                stage: {"schemaVersion": 1, "argv": ["/bin/true", stage], "cwd": "/tmp", "environment": {}}
                for stage in STAGES
            }
            seal = build_run_commands.seal_commands(run_dir, commands)
            self.assertRegex(seal["commandSetSha256"], r"^[0-9a-f]{64}$")
            persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(seal["commandSetSha256"], persisted["commandSetSha256"])
            with self.assertRaisesRegex(RuntimeError, "exactly once"):
                build_run_commands.seal_commands(run_dir, commands)

    def test_builder_emits_the_exact_single_attempt_chain_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / RUN_ID
            inputs = run_dir / "inputs"
            inputs.mkdir(parents=True)
            (run_dir / "run.json").write_text(json.dumps({
                "runId": RUN_ID, "sessionId": SESSION_ID,
            }), encoding="utf-8")
            (inputs / "preflight.json").write_text(json.dumps({
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "snapshot": {
                    "amis": {
                        "x86": {"imageId": "ami-0123456789abcdef0"},
                        "arm": {"imageId": "ami-0123456789abcdef1"},
                    },
                    "certificate": {
                        "arn": "arn:aws:acm:ap-northeast-2:742711170910:certificate/00000000-0000-0000-0000-000000000000",
                        "domainName": "event.api.dev.loop-ad.org",
                    },
                },
            }), encoding="utf-8")
            (inputs / "image-manifest.json").write_text(json.dumps({
                "runId": RUN_ID,
                "sessionId": SESSION_ID,
                "images": [
                    {"role": role, "digest": f"sha256:{digit * 64}"}
                    for role, digit in (("collector", "1"), ("consumer", "2"), ("archive", "3"))
                ],
            }), encoding="utf-8")
            ca = Path(directory) / "ca.pem"
            ca.write_text("certificate", encoding="utf-8")

            with mock.patch.object(build_run_commands, "executable", return_value="/checked/uv"):
                commands = build_run_commands.build_commands(run_dir, ca)

        self.assertEqual(STAGES, list(commands))
        self.assertEqual(set(STAGES), set(commands))
        for stage, document in commands.items():
            self.assertEqual(STAGE_TIMEOUT_SECONDS[stage], document["timeoutSeconds"])
            self.assertIsInstance(document["argv"], list)
            self.assertNotIn("shell", document)
            self.assertEqual({
                "CDK_DEFAULT_ACCOUNT": "742711170910",
                "LOOP_AD_REGION": "ap-northeast-2",
                "UV_CACHE_DIR": "/tmp/loopad-phase7-uv-cache",
            }, document["environment"])
            serialized = json.dumps(document)
            self.assertNotRegex(serialized, r"AWS_(?:ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)")
            self.assertNotIn("run_20260717_123647_phase7_integration", serialized)

        deploy = commands["deploy"]["argv"]
        self.assertEqual(1, deploy.count("deploy"))
        self.assertIn("--exclusively", deploy)
        self.assertEqual(1, deploy.count("--outputs-file"))
        self.assertEqual("hard-stop", commands["deploy"]["nonzeroDisposition"])
        self.assertEqual("acceptance-failure", commands["evaluate"]["nonzeroDisposition"])
        self.assertIn(str(ca.resolve()), commands["verify"]["argv"])
        self.assertIn(str(ca.resolve()), commands["warmup"]["argv"])
        self.assertIn(str(ca.resolve()), commands["score_archive"]["argv"])
        self.assertIn("--execute", commands["cleanup"]["argv"])
        self.assertNotIn("--execute", commands["inventory"]["argv"])

    def test_builder_rejects_cross_run_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            inputs = run_dir / "inputs"
            inputs.mkdir()
            (run_dir / "run.json").write_text(json.dumps({
                "runId": RUN_ID, "sessionId": SESSION_ID,
            }), encoding="utf-8")
            for name in ("preflight.json", "image-manifest.json"):
                (inputs / name).write_text(json.dumps({
                    "runId": "run_20260718_190001_phase7_integration",
                    "sessionId": SESSION_ID,
                }), encoding="utf-8")
            ca = Path(directory) / "ca.pem"
            ca.write_text("certificate", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "another run"):
                build_run_commands.build_commands(run_dir, ca)


if __name__ == "__main__":
    unittest.main()

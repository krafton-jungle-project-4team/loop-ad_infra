#!/usr/bin/env python3
"""Package, upload, and bootstrap the unmodified qualified producer on its run host."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cleanup_inventory_ecs import owned, tag_map
from ecs_run_support import (
    AwsRun,
    EXPECTED_POOL_SHA256,
    PAYLOAD_PATH,
    QUALIFIED_IMPLEMENTATION,
    load_bundle,
    wait_until,
    write_private,
)


PACKAGE_SCRIPT = QUALIFIED_IMPLEMENTATION / "package_asset.py"
INSTALL_DIR = "/opt/loopad-producer"
EVIDENCE_DIR = "/var/lib/loopad-producer/evidence"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1_800)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_bundle(args.run_dir)
    aws = AwsRun(bundle)
    identity = aws.assert_identity()
    instance_id = bundle.outputs["ProducerInstanceId"]
    instance = wait_for_producer(aws, instance_id, args.timeout_seconds)

    archive = args.run_dir / "producer-asset.tar.gz"
    manifest_path = args.run_dir / "producer-asset-manifest.json"
    if archive.exists() != manifest_path.exists():
        raise FileExistsError("producer asset and manifest must either both exist or both be absent")
    if not archive.exists():
        subprocess.run([
            sys.executable,
            str(PACKAGE_SCRIPT),
            "--implementation-dir", str(QUALIFIED_IMPLEMENTATION),
            "--payload-pool", str(PAYLOAD_PATH),
            "--output", str(archive),
            "--manifest", str(manifest_path),
        ], check=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != manifest["sha256"]:
        raise RuntimeError("producer asset manifest digest mismatch")

    bucket = bundle.outputs["ArchiveBucketName"]
    s3 = aws.client("s3")
    bucket_tags = tag_map(s3.get_bucket_tagging(Bucket=bucket)["TagSet"])
    if not owned(bucket_tags, bundle.run_id, bundle.session_id):
        raise RuntimeError("producer artifact bucket ownership mismatch")
    key = f"producer/{bundle.run_id}/producer-asset.tar.gz"
    s3.upload_file(
        str(archive),
        bucket,
        key,
        ExtraArgs={
            "ServerSideEncryption": "AES256",
            "Metadata": {"sha256": digest},
            "ContentType": "application/gzip",
        },
    )
    head = s3.head_object(Bucket=bucket, Key=key)
    if head.get("Metadata", {}).get("sha256") != digest:
        raise RuntimeError("uploaded producer asset metadata digest mismatch")

    command = bootstrap_command(bucket, key, digest)
    bootstrap_stdout = aws.run_ssm(
        [command],
        timeout_seconds=args.timeout_seconds,
        instance_id=instance_id,
    )
    verification = aws.run_ssm(
        [
            "set -euo pipefail",
            f"runuser -u ec2-user -- test -x {INSTALL_DIR}/run_stage.sh",
            f"runuser -u ec2-user -- test -x {INSTALL_DIR}/.venv/bin/locust",
            (
                f"test \"$(sha256sum {INSTALL_DIR}/payloads.ndjson | awk '{{print $1}}')\" "
                f"= {EXPECTED_POOL_SHA256}"
            ),
            f"sha256sum {INSTALL_DIR}/payloads.ndjson",
            f"cat {EVIDENCE_DIR}/architecture.txt",
            f"cat {EVIDENCE_DIR}/locust-version.txt",
        ],
        timeout_seconds=180,
        instance_id=instance_id,
    )
    result = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "runId": bundle.run_id,
        "sessionId": bundle.session_id,
        "identity": identity,
        "instance": instance,
        "asset": {
            "bucket": bucket,
            "key": key,
            "sha256": digest,
            "bytes": archive.stat().st_size,
            "manifest": manifest,
            "payloadPoolSha256": EXPECTED_POOL_SHA256,
        },
        "bootstrapStdout": bootstrap_stdout,
        "verification": verification,
        "pass": "aarch64" in verification and "locust 2.31.6 " in verification,
    }
    write_private(args.run_dir / "producer-bootstrap-ecs.json", result)
    print(json.dumps({
        "instanceId": instance_id,
        "assetSha256": digest,
        "pass": result["pass"],
    }, indent=2))
    return 0 if result["pass"] else 2


def wait_for_producer(aws: AwsRun, instance_id: str, timeout_seconds: int) -> dict[str, Any]:
    ec2 = aws.client("ec2")
    ec2.get_waiter("instance_running").wait(
        InstanceIds=[instance_id],
        WaiterConfig={"Delay": 10, "MaxAttempts": max(1, timeout_seconds // 10)},
    )
    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    tags = tag_map(instance.get("Tags", []))
    if not owned(tags, aws.bundle.run_id, aws.bundle.session_id):
        raise RuntimeError("producer instance ownership mismatch")
    if instance["InstanceType"] != "c7g.2xlarge" or instance["Placement"]["AvailabilityZone"] != "ap-northeast-2a":
        raise RuntimeError("producer instance type or availability zone mismatch")
    wait_until(
        "producer SSM online",
        timeout_seconds,
        10,
        lambda: producer_ping_status(aws, instance_id),
        lambda value: value == "Online",
    )
    return {
        "instanceId": instance_id,
        "instanceType": instance["InstanceType"],
        "availabilityZone": instance["Placement"]["AvailabilityZone"],
        "architecture": instance.get("Architecture"),
        "state": instance["State"]["Name"],
        "ssmPingStatus": "Online",
    }


def producer_ping_status(aws: AwsRun, instance_id: str) -> str | None:
    response = aws.client("ssm").describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}],
        MaxResults=5,
    )
    items = response.get("InstanceInformationList", [])
    return str(items[0]["PingStatus"]) if len(items) == 1 else None


def bootstrap_command(bucket: str, key: str, digest: str) -> str:
    s3_uri = f"s3://{bucket}/{key}"
    bootstrap_dir = "/tmp/loopad-producer-bootstrap"
    archive = "/tmp/loopad-producer-bootstrap.tar.gz"
    values = [bucket, key, digest]
    if any(not value or "\n" in value or "\r" in value for value in values):
        raise ValueError("invalid producer bootstrap input")
    return "\n".join([
        "set -euo pipefail",
        f"rm -rf {bootstrap_dir}",
        f"mkdir -p {bootstrap_dir}",
        f"aws s3 cp {shlex.quote(s3_uri)} {archive} --only-show-errors",
        f"printf '%s  %s\\n' {shlex.quote(digest)} {archive} | sha256sum -c -",
        f"tar -xzf {archive} -C {bootstrap_dir} bootstrap.sh",
        (
            f"bash {bootstrap_dir}/bootstrap.sh {shlex.quote(s3_uri)} "
            f"{shlex.quote(digest)} {INSTALL_DIR} {EVIDENCE_DIR}"
        ),
        f"chmod 0755 {INSTALL_DIR}/run_stage.sh",
    ])


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Empty only run-owned S3 buckets and ECR images before CDK stack deletion."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from botocore.exceptions import ClientError

from cleanup_inventory_ecs import (
    IMAGE_STACK_NAME,
    RUNTIME_STACK_NAME,
    load_inventory_bundle,
    owned,
    stack_owned,
    tag_map,
)
from ecs_run_support import AwsRun, RunBundle, write_private


class CleanupPreparation:
    def __init__(self, bundle: RunBundle) -> None:
        self.bundle = bundle
        self.aws = AwsRun(bundle)

    def run(self) -> dict[str, Any]:
        identity = self.aws.assert_identity()
        stack_states = {
            name: self._assert_stack_ownership_or_absence(name)
            for name in [RUNTIME_STACK_NAME, IMAGE_STACK_NAME]
        }
        buckets = self._owned_bucket_names()
        bucket_results = [self._empty_bucket(name) for name in buckets]
        repository_result = self._empty_repository(
            self.bundle.outputs.get(
                "ConsumerRepositoryName",
                f"loop-ad/perf-phase4-clickhouse/{self.bundle.run_id}",
            ),
        )
        return {
            "schemaVersion": 1,
            "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "runId": self.bundle.run_id,
            "sessionId": self.bundle.session_id,
            "identity": identity,
            "stackStates": stack_states,
            "buckets": bucket_results,
            "repository": repository_result,
            "readyForCdkDestroy": True,
        }

    def _assert_stack_ownership_or_absence(self, name: str) -> str:
        try:
            stack = self.aws.client("cloudformation").describe_stacks(StackName=name)["Stacks"][0]
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ValidationError":
                return "absent"
            raise
        if not stack_owned(
            self.aws.client("cloudformation"),
            stack,
            self.bundle.run_id,
            self.bundle.session_id,
        ):
            raise RuntimeError(f"stack ownership mismatch: {name}")
        return str(stack["StackStatus"])

    def _owned_bucket_names(self) -> list[str]:
        client = self.aws.client("s3")
        configured = {
            self.bundle.outputs[key]
            for key in ["FailureBucketName", "ArchiveBucketName"]
            if self.bundle.outputs.get(key)
        }
        candidates = configured or {
            item["Name"] for item in client.list_buckets().get("Buckets", [])
        }
        result: list[str] = []
        for name in sorted(candidates):
            try:
                tags = tag_map(client.get_bucket_tagging(Bucket=name)["TagSet"])
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {
                    "NoSuchBucket", "NoSuchTagSet",
                }:
                    continue
                raise
            if self._owned(tags):
                result.append(name)
        return result

    def _empty_bucket(self, name: str) -> dict[str, Any]:
        client = self.aws.client("s3")
        tags = tag_map(client.get_bucket_tagging(Bucket=name)["TagSet"])
        if not self._owned(tags):
            raise RuntimeError(f"bucket ownership mismatch: {name}")
        deleted_versions = 0
        for page in client.get_paginator("list_object_versions").paginate(Bucket=name):
            identifiers = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
            ]
            for chunk in chunks(identifiers, 1_000):
                response = client.delete_objects(Bucket=name, Delete={"Objects": chunk, "Quiet": True})
                if response.get("Errors"):
                    raise RuntimeError(f"failed to delete bucket versions from {name}")
                deleted_versions += len(chunk)
        deleted_objects = 0
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=name):
            identifiers = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            for chunk in chunks(identifiers, 1_000):
                response = client.delete_objects(Bucket=name, Delete={"Objects": chunk, "Quiet": True})
                if response.get("Errors"):
                    raise RuntimeError(f"failed to delete bucket objects from {name}")
                deleted_objects += len(chunk)
        aborted_uploads = 0
        for page in client.get_paginator("list_multipart_uploads").paginate(Bucket=name):
            for upload in page.get("Uploads", []):
                client.abort_multipart_upload(
                    Bucket=name,
                    Key=upload["Key"],
                    UploadId=upload["UploadId"],
                )
                aborted_uploads += 1
        remaining = sum(
            len(page.get("Contents", []))
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=name)
        )
        if remaining != 0:
            raise RuntimeError(f"bucket is not empty after cleanup preparation: {name}")
        return {
            "name": name,
            "deletedVersions": deleted_versions,
            "deletedObjects": deleted_objects,
            "abortedMultipartUploads": aborted_uploads,
            "remainingObjects": remaining,
        }

    def _empty_repository(self, name: str) -> dict[str, Any]:
        client = self.aws.client("ecr")
        try:
            repository = client.describe_repositories(repositoryNames=[name])["repositories"][0]
        except client.exceptions.RepositoryNotFoundException:
            return {"name": name, "status": "absent", "deletedImages": 0, "remainingImages": 0}
        tags = tag_map(client.list_tags_for_resource(
            resourceArn=repository["repositoryArn"],
        )["tags"])
        if not self._owned(tags):
            raise RuntimeError(f"repository ownership mismatch: {name}")
        deleted = 0
        remaining = 0
        for _ in range(4):
            image_ids = [
                image_id
                for page in client.get_paginator("list_images").paginate(repositoryName=name)
                for image_id in page.get("imageIds", [])
            ]
            if not image_ids:
                remaining = 0
                break
            deleted_this_pass = 0
            failures: list[dict[str, Any]] = []
            for chunk in chunks(image_ids, 100):
                response = client.batch_delete_image(repositoryName=name, imageIds=chunk)
                deleted_count = len(response.get("imageIds", []))
                deleted += deleted_count
                deleted_this_pass += deleted_count
                failures.extend(response.get("failures", []))
            remaining = sum(
                len(page.get("imageIds", []))
                for page in client.get_paginator("list_images").paginate(repositoryName=name)
            )
            if remaining == 0:
                break
            if deleted_this_pass == 0:
                codes = sorted({str(item.get("failureCode")) for item in failures})
                raise RuntimeError(
                    f"failed to delete ECR images from {name}; failures={codes}"
                )
        if remaining != 0:
            raise RuntimeError(f"repository is not empty after cleanup preparation: {name}")
        return {
            "name": name,
            "status": "emptied",
            "deletedImages": deleted,
            "remainingImages": remaining,
        }

    def _owned(self, tags: dict[str, str]) -> bool:
        return owned(tags, self.bundle.run_id, self.bundle.session_id)


def chunks(values: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_inventory_bundle(args.run_dir)
    result = CleanupPreparation(bundle).run()
    output = args.output or args.run_dir / "cleanup-preparation-ecs.json"
    write_private(output, result)
    print(json.dumps({
        "buckets": result["buckets"],
        "repository": result["repository"],
        "readyForCdkDestroy": result["readyForCdkDestroy"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

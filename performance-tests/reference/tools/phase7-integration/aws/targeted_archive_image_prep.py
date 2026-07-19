#!/usr/bin/env python3
"""Deploy one run-owned ECR repository and push one exact ARM64 archive image."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from targeted_archive_cleanup import cleanup, describe_stack
from targeted_archive_common import (
    SDK_CONFIG,
    app_command,
    cdk_context,
    cdk_environment,
    git_identity,
    locked_session,
    repository_name,
    run,
    source_closure,
    stack_names,
    tag_map,
    tags_match,
    utc_now,
    validate_identifiers,
    write_json,
)


DUMMY_DIGEST = "sha256:" + "0" * 64
DUMMY_AMI = "ami-00000000"


def plugin_directories() -> list[Path]:
    candidates = [
        Path.home() / ".docker/cli-plugins",
        Path("/usr/local/lib/docker/cli-plugins"),
        Path("/usr/local/libexec/docker/cli-plugins"),
        Path("/usr/lib/docker/cli-plugins"),
        Path("/usr/libexec/docker/cli-plugins"),
        Path("/Applications/Docker.app/Contents/Resources/cli-plugins"),
    ]
    return [path for path in candidates if (path / "docker-buildx").is_file()]


def docker_environment(config: Path) -> dict[str, str]:
    plugins = plugin_directories()
    if not plugins:
        raise RuntimeError("docker buildx plugin was not found")
    config.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_json(config / "config.json", {"cliPluginsExtraDirs": [str(path) for path in plugins]})
    environment = os.environ.copy()
    environment["DOCKER_CONFIG"] = str(config)
    return environment


def docker_login(ecr: Any, infra_root: Path, environment: dict[str, str]) -> None:
    authorization = ecr.get_authorization_token().get("authorizationData", [])
    if len(authorization) != 1:
        raise RuntimeError("expected one ECR authorization endpoint")
    username, password = base64.b64decode(
        authorization[0]["authorizationToken"]
    ).decode().split(":", 1)
    run(
        [
            "docker", "login", "--username", username, "--password-stdin",
            authorization[0]["proxyEndpoint"],
        ],
        infra_root,
        stdin=password + "\n",
        env=environment,
    )


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    validate_identifiers(args.run_id, args.session_id)
    infra_root = args.infra_root.resolve()
    identity = git_identity(infra_root)
    closure = source_closure(infra_root)
    session = locked_session()
    cloudformation = session.client("cloudformation", config=SDK_CONFIG)
    ecr = session.client("ecr", config=SDK_CONFIG)
    image_stack_name, runtime_stack_name = stack_names(args.session_id)
    if describe_stack(cloudformation, image_stack_name) is not None:
        raise RuntimeError("fresh targeted image stack must be absent before image preparation")
    if describe_stack(cloudformation, runtime_stack_name) is not None:
        raise RuntimeError("fresh targeted runtime stack must be absent before image preparation")
    try:
        ecr.describe_repositories(repositoryNames=[repository_name(args.run_id)])
    except ecr.exceptions.RepositoryNotFoundException:
        pass
    else:
        raise RuntimeError("fresh targeted ECR repository must be absent before image preparation")

    cdk = infra_root / "node_modules/.bin/cdk"
    if not cdk.is_file():
        raise FileNotFoundError("checked workspace CDK executable is missing")
    command = [
        str(cdk), "--app", app_command(infra_root),
        *cdk_context(args.run_id, args.session_id, DUMMY_DIGEST, DUMMY_AMI),
        "deploy", "LoopAdPerfPhase7ArchiveDiagnosticImageStack", "--exclusively",
        "--require-approval", "never", "--concurrency", "1",
    ]
    run(command, infra_root, env=cdk_environment())
    stack = describe_stack(cloudformation, image_stack_name)
    if stack is None or stack.get("StackStatus") != "CREATE_COMPLETE":
        raise RuntimeError("targeted image stack did not reach CREATE_COMPLETE")
    if not tags_match(tag_map(stack.get("Tags", [])), args.run_id, args.session_id):
        raise RuntimeError("targeted image stack ownership tags do not match")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
    if outputs.get("ArchiveRepositoryName") != repository_name(args.run_id):
        raise RuntimeError("targeted image stack repository output does not match")
    repository_uri = outputs.get("ArchiveRepositoryUri")
    if not isinstance(repository_uri, str) or not repository_uri:
        raise RuntimeError("targeted image stack repository URI is missing")

    parent = Path(tempfile.mkdtemp(prefix="loopad-phase7-targeted-image-", dir="/tmp"))
    try:
        environment = docker_environment(parent / "docker-config")
        run(["docker", "buildx", "version"], infra_root, capture=True, env=environment)
        run(["docker", "buildx", "inspect", "--bootstrap"], infra_root, capture=True, env=environment)
        docker_login(ecr, infra_root, environment)
        tag = f"source-{closure['sha256'][:24]}"
        tagged_image = f"{repository_uri}:{tag}"
        run([
            "docker", "buildx", "build", "--platform", "linux/arm64",
            "--provenance=false", "--sbom=false", "--pull", "--push",
            "--file", str(infra_root / "performance-tests/phase7-integration/archive/Dockerfile"),
            "--tag", tagged_image, str(infra_root),
        ], infra_root, env=environment)
        details = ecr.describe_images(
            repositoryName=repository_name(args.run_id),
            imageIds=[{"imageTag": tag}],
        ).get("imageDetails", [])
        if len(details) != 1 or not str(details[0].get("imageDigest", "")).startswith("sha256:"):
            raise RuntimeError("targeted archive image digest is missing")
        digest = details[0]["imageDigest"]
        exact_image = f"{repository_uri}@{digest}"
        run(["docker", "pull", "--platform", "linux/arm64", exact_image], infra_root, env=environment)
        platform = run(
            ["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", exact_image],
            infra_root,
            capture=True,
            env=environment,
        )
        if platform != "linux/arm64":
            raise RuntimeError(f"targeted archive image platform mismatch: {platform}")
    finally:
        shutil.rmtree(parent, ignore_errors=True)

    repository = ecr.describe_repositories(
        repositoryNames=[repository_name(args.run_id)]
    ).get("repositories", [])
    if len(repository) != 1:
        raise RuntimeError("targeted ECR repository cardinality changed after push")
    repository_tags = tag_map(ecr.list_tags_for_resource(
        resourceArn=repository[0]["repositoryArn"]
    ).get("tags", []))
    if not tags_match(repository_tags, args.run_id, args.session_id):
        raise RuntimeError("targeted ECR repository ownership tags do not match")
    return {
        "schemaVersion": 1,
        "workload": "phase7-targeted-archive-diagnostic",
        "preparedAt": utc_now(),
        "runId": args.run_id,
        "sessionId": args.session_id,
        "implementation": identity,
        "sourceClosure": closure,
        "imageStackName": image_stack_name,
        "imageStackStatus": stack["StackStatus"],
        "imageStackDeployAttempts": 1,
        "repository": repository_name(args.run_id),
        "repositoryUri": repository_uri,
        "tag": tag,
        "digest": digest,
        "exactImage": exact_image,
        "platform": platform,
        "runtimeDeployed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--failure-output", required=True, type=Path)
    parser.add_argument("--cleanup-output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = prepare(args)
        write_json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        failure = {
            "schemaVersion": 1,
            "failedAt": utc_now(),
            "stage": "targeted-image-preparation",
            "runId": args.run_id,
            "sessionId": args.session_id,
            "errorType": type(error).__name__,
            "error": str(error),
        }
        write_json(args.failure_output, failure)
        try:
            cleanup_result = cleanup(locked_session(), args.run_id, args.session_id)
            write_json(args.cleanup_output, cleanup_result)
        except Exception as cleanup_error:
            failure["cleanupErrorType"] = type(cleanup_error).__name__
            failure["cleanupError"] = str(cleanup_error)
            write_json(args.failure_output, failure)
        raise


if __name__ == "__main__":
    raise SystemExit(main())

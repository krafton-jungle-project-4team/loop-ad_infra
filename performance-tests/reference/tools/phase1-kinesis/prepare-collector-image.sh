#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COLLECTOR_WORKTREE="${COLLECTOR_WORKTREE:-/private/tmp/loop-ad-event-collector-phase1-20260710-171241}"
EXPECTED_COLLECTOR_SHA="${EXPECTED_COLLECTOR_SHA:-}"
OUTPUT="${OUTPUT:-}"
EXPECTED_EXISTING_DIGEST="${EXPECTED_EXISTING_DIGEST:-}"
: "${CANDIDATE:?Set CANDIDATE to go-sync, go-batch, or java-kpl.}"
EXPERIMENT_MODE="${EXPERIMENT_MODE:-comparison}"
REGION="ap-northeast-2"
REPOSITORY_NAME="loop-ad/event-collector"

[[ "$CANDIDATE" =~ ^(go-sync|go-batch|java-kpl)$ ]] || { printf 'invalid candidate\n' >&2; exit 2; }
if [[ "$EXPERIMENT_MODE" == "capacity" || "$EXPERIMENT_MODE" == "scout" || "$EXPERIMENT_MODE" == "capacity-oha12k" || "$EXPERIMENT_MODE" == "generator-diagnosis" || "$EXPERIMENT_MODE" == "alb-warmup" || "$EXPERIMENT_MODE" == "tcp-alb-path-diagnosis" || "$EXPERIMENT_MODE" == "admission-scaleout-capacity" || "$EXPERIMENT_MODE" == "protocol-crossover" || "$EXPERIMENT_MODE" == "connection-path-crossover" ]]; then
    : "${SESSION_ID:?Set the exact capacity or scout SESSION_ID.}"
    if [[ "$EXPERIMENT_MODE" == "scout" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-capacity-scout-[0-9]{8}T[0-9]{6}Z$ ]] || { printf 'invalid scout session ID\n' >&2; exit 2; }
    elif [[ "$EXPERIMENT_MODE" == "capacity-oha12k" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-capacity-oha12k-[0-9]{8}T[0-9]{6}Z$ ]] || { printf 'invalid oha12k session ID\n' >&2; exit 2; }
    elif [[ "$EXPERIMENT_MODE" == "generator-diagnosis" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-kinesis-generator-diagnosis-[0-9]{8}T[0-9]{6}Z$ ]] || { printf 'invalid generator-diagnosis session ID\n' >&2; exit 2; }
    elif [[ "$EXPERIMENT_MODE" == "alb-warmup" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-kinesis-alb-warmup-[0-9]{8}T[0-9]{6}Z$ ]] || { printf 'invalid alb-warmup session ID\n' >&2; exit 2; }
    elif [[ "$EXPERIMENT_MODE" == "tcp-alb-path-diagnosis" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-kinesis-tcp-alb-path-diagnosis-[0-9]{8}T[0-9]{6}Z$ ]] || { printf 'invalid tcp-alb-path-diagnosis session ID\n' >&2; exit 2; }
    elif [[ "$EXPERIMENT_MODE" == "admission-scaleout-capacity" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-kinesis-admission-scaleout-[0-9]{8}T[0-9]{6}Z$ ]] || { printf 'invalid admission-scaleout-capacity session ID\n' >&2; exit 2; }
	elif [[ "$EXPERIMENT_MODE" == "protocol-crossover" ]]; then
		[[ "$SESSION_ID" =~ ^phase1-kinesis-protocol-crossover-[0-9]{8}T[0-9]{6}Z$ ]] || { printf 'invalid protocol-crossover session ID\n' >&2; exit 2; }
    elif [[ "$EXPERIMENT_MODE" == "connection-path-crossover" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-kinesis-connection-path-[0-9]{8}T[0-9]{6}Z$ ]] || { printf 'invalid connection-path-crossover session ID\n' >&2; exit 2; }
    else
        [[ "$SESSION_ID" =~ ^phase1-capacity-[0-9]{8}T[0-9]{6}Z$ ]] || { printf 'invalid capacity session ID\n' >&2; exit 2; }
    fi
elif [[ "$EXPERIMENT_MODE" == "comparison" ]]; then
    : "${COMPARISON_SESSION_ID:?Set the exact comparison session ID.}"
    SESSION_ID="$COMPARISON_SESSION_ID"
    [[ "$SESSION_ID" =~ ^phase1-compare-[0-9]{8}-[0-9]{6}z$ ]] || { printf 'invalid comparison session ID\n' >&2; exit 2; }
else
    printf 'invalid experiment mode\n' >&2
    exit 2
fi

[[ -d "$COLLECTOR_WORKTREE/.git" || -f "$COLLECTOR_WORKTREE/.git" ]] || { printf 'collector worktree not found\n' >&2; exit 2; }
[[ -z "$(git -C "$COLLECTOR_WORKTREE" status --porcelain)" ]] || { printf 'collector worktree is dirty\n' >&2; exit 2; }
collector_branch="$(git -C "$COLLECTOR_WORKTREE" branch --show-current)"
if [[ "$collector_branch" != "codex/phase1-kinesis-transition" ]]; then
    [[ -z "$collector_branch" && "$EXPECTED_COLLECTOR_SHA" =~ ^[0-9a-f]{40}$ ]] || {
        printf 'unexpected collector branch without an exact detached-worktree SHA\n' >&2
        exit 2
    }
fi
git -C "$COLLECTOR_WORKTREE" cat-file -e '1769eec^{commit}'
git -C "$COLLECTOR_WORKTREE" merge-base --is-ancestor 1769eec HEAD
collector_sha="$(git -C "$COLLECTOR_WORKTREE" rev-parse HEAD)"
[[ "$collector_sha" =~ ^[0-9a-f]{40}$ ]]
if [[ -n "$EXPECTED_COLLECTOR_SHA" ]]; then
    [[ "$collector_sha" == "$EXPECTED_COLLECTOR_SHA" ]] || { printf 'collector HEAD does not match EXPECTED_COLLECTOR_SHA\n' >&2; exit 2; }
fi
if [[ "$EXPERIMENT_MODE" == "capacity" || "$EXPERIMENT_MODE" == "scout" || "$EXPERIMENT_MODE" == "capacity-oha12k" || "$EXPERIMENT_MODE" == "generator-diagnosis" || "$EXPERIMENT_MODE" == "alb-warmup" || "$EXPERIMENT_MODE" == "tcp-alb-path-diagnosis" || "$EXPERIMENT_MODE" == "admission-scaleout-capacity" || "$EXPERIMENT_MODE" == "protocol-crossover" || "$EXPERIMENT_MODE" == "connection-path-crossover" ]]; then
    if [[ "$EXPERIMENT_MODE" == "alb-warmup" ]]; then
        : "${RUN_ID:?Set the exact alb-warmup RUN_ID.}"
        [[ "$RUN_ID" =~ ^run_[0-9]{8}_[0-9]{6}_[a-z0-9][a-z0-9_-]{0,31}$ ]] || { printf 'invalid alb-warmup run ID\n' >&2; exit 2; }
        image_tag="$RUN_ID-$CANDIDATE-$collector_sha"
    else
        image_tag="$SESSION_ID-$CANDIDATE-$collector_sha"
    fi
else
    image_tag="phase1-compare-$CANDIDATE-$collector_sha"
fi

set -a
source "$REPO_ROOT/.env"
set +a
[[ "${LOOP_AD_REGION:-}" == "$REGION" ]]
actual_account="$(aws sts get-caller-identity --query Account --output text)"
[[ -n "${CDK_DEFAULT_ACCOUNT:-}" && "$actual_account" == "$CDK_DEFAULT_ACCOUNT" ]]
repository_uri="$(aws ecr describe-repositories --region "$REGION" --repository-names "$REPOSITORY_NAME" --query 'repositories[0].repositoryUri' --output text)"
registry="${repository_uri%%/*}"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$registry" >/dev/null

existing_digest="$(aws ecr list-images --region "$REGION" --repository-name "$REPOSITORY_NAME" \
    --filter tagStatus=TAGGED --query "imageIds[?imageTag=='$image_tag'].imageDigest | [0]" --output text)"
reused=false
if [[ "$existing_digest" != "None" ]]; then
    [[ "$EXPECTED_EXISTING_DIGEST" =~ ^sha256:[0-9a-f]{64}$ && "$existing_digest" == "$EXPECTED_EXISTING_DIGEST" ]] || {
        printf 'exact Phase 1 image tag already exists without the explicitly expected digest\n' >&2
        exit 2
    }
    reused=true
else
    [[ -z "$EXPECTED_EXISTING_DIGEST" ]] || { printf 'expected existing image digest is absent\n' >&2; exit 2; }
    (
        cd "$COLLECTOR_WORKTREE"
        if [[ "$CANDIDATE" == "java-kpl" ]]; then
            docker buildx build --platform linux/amd64 --provenance=false --sbom=false --load \
                -f java-kpl-prototype/Dockerfile -t "$repository_uri:$image_tag" java-kpl-prototype
        else
            docker buildx build --platform linux/amd64 --provenance=false --sbom=false --load \
                -t "$repository_uri:$image_tag" .
        fi
        docker push "$repository_uri:$image_tag"
    )
fi
remote="$(aws ecr describe-images --region "$REGION" --repository-name "$REPOSITORY_NAME" --image-ids "imageTag=$image_tag" --output json)"

image_digest="$(jq -r '.imageDetails[0].imageDigest' <<<"$remote")"
image_size_bytes="$(jq -r '.imageDetails[0].imageSizeInBytes' <<<"$remote")"
[[ "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$image_size_bytes" =~ ^[0-9]+$ ]]
docker pull "$repository_uri@$image_digest" >/dev/null
architecture="$(docker image inspect "$repository_uri@$image_digest" --format '{{.Architecture}}')"
[[ "$architecture" == "amd64" ]]

result="$(jq -n \
    --arg preparedAt "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    --arg collectorSha "$collector_sha" --arg repositoryName "$REPOSITORY_NAME" \
    --arg candidate "$CANDIDATE" --arg sessionId "$SESSION_ID" --arg experimentMode "$EXPERIMENT_MODE" \
    --arg imageTag "$image_tag" --arg imageDigest "$image_digest" \
    --arg architecture "$architecture" --argjson imageSizeBytes "$image_size_bytes" \
    --argjson reused "$reused" \
    '{preparedAt:$preparedAt,experimentMode:$experimentMode,sessionId:$sessionId,candidate:$candidate,collectorSha:$collectorSha,repositoryName:$repositoryName,imageTag:$imageTag,imageDigest:$imageDigest,imageSizeBytes:$imageSizeBytes,architecture:$architecture,reusedExistingTag:$reused}')"

if [[ -n "$OUTPUT" ]]; then
    printf '%s\n' "$result" >"$OUTPUT"
fi
printf '%s\n' "$result"

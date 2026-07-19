#!/usr/bin/env bash
set -euo pipefail

: "${COLLECTOR_SHA:?Set the exact collector SHA.}"
: "${IMAGE_DIGEST:?Set the exact ECR image digest.}"
: "${CANDIDATE:?Set CANDIDATE to go-sync, go-batch, or java-kpl.}"
EXPERIMENT_MODE="${EXPERIMENT_MODE:-comparison}"
REGION="ap-northeast-2"
REPOSITORY_NAME="loop-ad/event-collector"
if [[ "$EXPERIMENT_MODE" == "capacity" || "$EXPERIMENT_MODE" == "scout" || "$EXPERIMENT_MODE" == "capacity-oha12k" || "$EXPERIMENT_MODE" == "generator-diagnosis" || "$EXPERIMENT_MODE" == "alb-warmup" || "$EXPERIMENT_MODE" == "admission-scaleout-capacity" || "$EXPERIMENT_MODE" == "protocol-crossover" || "$EXPERIMENT_MODE" == "connection-path-crossover" ]]; then
    : "${SESSION_ID:?Set the exact capacity or scout SESSION_ID.}"
    if [[ "$EXPERIMENT_MODE" == "scout" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-capacity-scout-[0-9]{8}T[0-9]{6}Z$ ]]
    elif [[ "$EXPERIMENT_MODE" == "capacity-oha12k" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-capacity-oha12k-[0-9]{8}T[0-9]{6}Z$ ]]
    elif [[ "$EXPERIMENT_MODE" == "generator-diagnosis" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-kinesis-generator-diagnosis-[0-9]{8}T[0-9]{6}Z$ ]]
    elif [[ "$EXPERIMENT_MODE" == "alb-warmup" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-kinesis-alb-warmup-[0-9]{8}T[0-9]{6}Z$ ]]
    elif [[ "$EXPERIMENT_MODE" == "admission-scaleout-capacity" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-kinesis-admission-scaleout-[0-9]{8}T[0-9]{6}Z$ ]]
    elif [[ "$EXPERIMENT_MODE" == "protocol-crossover" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-kinesis-protocol-crossover-[0-9]{8}T[0-9]{6}Z$ ]]
    elif [[ "$EXPERIMENT_MODE" == "connection-path-crossover" ]]; then
        [[ "$SESSION_ID" =~ ^phase1-kinesis-connection-path-[0-9]{8}T[0-9]{6}Z$ ]]
    else
        [[ "$SESSION_ID" =~ ^phase1-capacity-[0-9]{8}T[0-9]{6}Z$ ]]
    fi
    if [[ "$EXPERIMENT_MODE" == "alb-warmup" ]]; then
        : "${RUN_ID:?Set the exact alb-warmup RUN_ID.}"
        [[ "$RUN_ID" =~ ^run_[0-9]{8}_[0-9]{6}_[a-z0-9][a-z0-9_-]{0,31}$ ]]
        IMAGE_TAG="$RUN_ID-$CANDIDATE-$COLLECTOR_SHA"
    else
        IMAGE_TAG="$SESSION_ID-$CANDIDATE-$COLLECTOR_SHA"
    fi
elif [[ "$EXPERIMENT_MODE" == "comparison" ]]; then
    IMAGE_TAG="phase1-compare-$CANDIDATE-$COLLECTOR_SHA"
else
    printf 'invalid experiment mode\n' >&2
    exit 2
fi

[[ "$COLLECTOR_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CANDIDATE" =~ ^(go-sync|go-batch|java-kpl)$ ]]
[[ "$IMAGE_TAG" != "latest" ]]

remote_digest="$(aws ecr describe-images --region "$REGION" --repository-name "$REPOSITORY_NAME" \
    --image-ids "imageTag=$IMAGE_TAG" --query 'imageDetails[0].imageDigest' --output text)"
[[ "$remote_digest" == "$IMAGE_DIGEST" ]] || { printf 'ECR tag digest does not match cleanup input\n' >&2; exit 2; }

aws ecr batch-delete-image --region "$REGION" --repository-name "$REPOSITORY_NAME" \
    --image-ids "imageTag=$IMAGE_TAG" --query '{deleted:imageIds,failures:failures}' --output json

if aws ecr describe-images --region "$REGION" --repository-name "$REPOSITORY_NAME" \
    --image-ids "imageTag=$IMAGE_TAG" >/dev/null 2>&1; then
    printf 'phase 1 image tag still exists after deletion\n' >&2
    exit 1
fi
printf 'deleted exact %s candidate tag; repository and latest were not modified\n' "$EXPERIMENT_MODE"

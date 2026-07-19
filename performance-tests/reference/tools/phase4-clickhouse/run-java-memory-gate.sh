#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE_NAME="loopad-phase4-native-java-kcl:memory-gate"
RESULT_PATH="${1:-${ROOT_DIR}/performance-tests/phase4-clickhouse/java-memory-gate-result.json}"

cd "${ROOT_DIR}"
docker buildx build \
  --platform linux/arm64 \
  --file performance-tests/phase4-clickhouse/consumer/Dockerfile \
  --tag "${IMAGE_NAME}" \
  --load \
  .

docker run --rm --platform linux/arm64 --network none --entrypoint /bin/sh "${IMAGE_NAME}" -c \
  'command -v java >/dev/null && ! command -v node >/dev/null && ! command -v socat >/dev/null'

docker run --rm \
  --platform linux/arm64 \
  --network none \
  --cpus 1 \
  --memory 2g \
  --memory-swap 2g \
  "${IMAGE_NAME}" memory-gate | tee "${RESULT_PATH}"

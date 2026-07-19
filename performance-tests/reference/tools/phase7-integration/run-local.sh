#!/bin/sh
set -eu
umask 077

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PHASE7_DIR="$ROOT/performance-tests/phase7-integration"
COLLECTOR_REPO=${PHASE7_COLLECTOR_REPO:-"$ROOT/../loop-ad_event_collector"}
COLLECTOR_SHA=497315137251af82d0d203ce34702d5543553942
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
LOCAL_SESSION_ID=${LOCAL_SESSION_ID:-"phase7-local-$TIMESTAMP-$$"}
PHASE7_RUN_ID=${PHASE7_RUN_ID:-"run_${TIMESTAMP}_phase7_1_local_integration"}
LOCAL_RUN_DIR=${LOCAL_RUN_DIR:-"$ROOT/performance-tests/$PHASE7_RUN_ID"}
PHASE7_COLLECTOR_CONTEXT=${PHASE7_COLLECTOR_CONTEXT:-"/tmp/loopad-phase7-collector-$LOCAL_SESSION_ID"}
COMPOSE_FILE="$PHASE7_DIR/docker-compose.yml"
CLEANED=0

export LOCAL_SESSION_ID PHASE7_RUN_ID LOCAL_RUN_DIR PHASE7_COLLECTOR_CONTEXT
export PHASE7_INFRA_ROOT="$ROOT"

finalize_evidence() {
  UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/loopad-phase7-uv-cache} uv run \
    --project "$ROOT/performance-tests/phase4-clickhouse/producer-env" \
    python "$PHASE7_DIR/finalize_evidence.py" \
    --infra-root "$ROOT" \
    --run-dir "$LOCAL_RUN_DIR" \
    --run-id "$PHASE7_RUN_ID" \
    --session-id "$LOCAL_SESSION_ID"
}

remove_runtime_secrets() {
  rm -f -- \
    "$LOCAL_RUN_DIR/clickhouse-user" \
    "$LOCAL_RUN_DIR/clickhouse-password" \
    "$LOCAL_RUN_DIR/archive-config.json"
}

cleanup() {
  if [ "$CLEANED" -eq 0 ]; then
    docker compose -f "$COMPOSE_FILE" logs >"$LOCAL_RUN_DIR/compose.log" 2>&1 || true
    docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans >/dev/null 2>&1 || true
    git -C "$COLLECTOR_REPO" worktree remove --force "$PHASE7_COLLECTOR_CONTEXT" >/dev/null 2>&1 || true
    UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/loopad-phase7-uv-cache} uv run \
      --project "$ROOT/performance-tests/phase4-clickhouse/producer-env" \
      python "$PHASE7_DIR/cleanup_inventory.py" \
      --session-id "$LOCAL_SESSION_ID" \
      --output "$LOCAL_RUN_DIR/cleanup-verification.json" >/dev/null 2>&1 || true
    remove_runtime_secrets
    finalize_evidence >/dev/null 2>&1 || true
    CLEANED=1
  fi
}
trap cleanup EXIT INT TERM

command -v docker >/dev/null 2>&1
command -v git >/dev/null 2>&1
command -v uv >/dev/null 2>&1
test -d "$COLLECTOR_REPO/.git"
git -C "$COLLECTOR_REPO" cat-file -e "$COLLECTOR_SHA^{commit}"
mkdir -p "$LOCAL_RUN_DIR"
git -C "$COLLECTOR_REPO" worktree add --detach "$PHASE7_COLLECTOR_CONTEXT" "$COLLECTOR_SHA"
test "$(git -C "$PHASE7_COLLECTOR_CONTEXT" rev-parse HEAD)" = "$COLLECTOR_SHA"
test -z "$(git -C "$PHASE7_COLLECTOR_CONTEXT" status --porcelain)"

docker compose -f "$COMPOSE_FILE" config --quiet
docker compose -f "$COMPOSE_FILE" up --build --detach --wait

UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/loopad-phase7-uv-cache} uv run \
  --project "$ROOT/performance-tests/phase4-clickhouse/producer-env" \
  python "$PHASE7_DIR/local_runner.py" \
  --run-id "$PHASE7_RUN_ID" \
  --run-dir "$LOCAL_RUN_DIR" \
  --compose-file "$COMPOSE_FILE" \
  "$@"

docker compose -f "$COMPOSE_FILE" logs >"$LOCAL_RUN_DIR/compose.log" 2>&1
docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans
git -C "$COLLECTOR_REPO" worktree remove --force "$PHASE7_COLLECTOR_CONTEXT"
UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/loopad-phase7-uv-cache} uv run \
  --project "$ROOT/performance-tests/phase4-clickhouse/producer-env" \
  python "$PHASE7_DIR/cleanup_inventory.py" \
  --session-id "$LOCAL_SESSION_ID" \
  --output "$LOCAL_RUN_DIR/cleanup-verification.json"
remove_runtime_secrets
finalize_evidence
CLEANED=1

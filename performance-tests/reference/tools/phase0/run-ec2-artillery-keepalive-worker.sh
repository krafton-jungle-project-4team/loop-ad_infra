#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BASE_SCENARIO="$SCRIPT_DIR/alb-fixed-response-keepalive.yml"
PAYLOAD_FILE="$SCRIPT_DIR/payloads/sdk-compatible-event-bodies.tsv"

: "${TARGET_BASE_URL:?Set TARGET_BASE_URL to Phase0LoadGeneratorTargetBaseUrl.}"

RUN_ID="${RUN_ID:-run_$(date +%Y%m%d_%H%M%S)_phase0_alb_keepalive}"
WORKER_LABEL="${WORKER_LABEL:-$(hostname -s 2>/dev/null || printf "worker")}"
VUS_PER_WORKER="${VUS_PER_WORKER:-2500}"
RAMP_SECONDS="${RAMP_SECONDS:-30}"
REQUESTS_PER_VU="${REQUESTS_PER_VU:-750}"
VU_THINK_SECONDS="${VU_THINK_SECONDS:-0.15}"
HTTP_TIMEOUT_SECONDS="${HTTP_TIMEOUT_SECONDS:-10}"

if [[ "$RUN_ID" == performance-tests/* ]]; then
    RUN_DIR="$REPO_ROOT/$RUN_ID"
else
    RUN_DIR="$REPO_ROOT/performance-tests/$RUN_ID"
fi

STEP_DIR="$RUN_DIR/$WORKER_LABEL"
SCENARIO="$STEP_DIR/scenario.yml"
EXPECTED_REQUESTS=$((VUS_PER_WORKER * REQUESTS_PER_VU))

mkdir -p "$STEP_DIR"

if command -v artillery >/dev/null 2>&1; then
    ARTILLERY=(artillery)
elif command -v npx >/dev/null 2>&1; then
    npx artillery@latest --version >/dev/null
    ARTILLERY=(npx artillery@latest)
elif command -v docker >/dev/null 2>&1; then
    ARTILLERY_DOCKER_IMAGE="${ARTILLERY_DOCKER_IMAGE:-artilleryio/artillery:latest}"
    docker pull "$ARTILLERY_DOCKER_IMAGE" >/dev/null
    ARTILLERY=(
        docker run --rm
        --network host
        --ulimit nofile=1048576:1048576
        -v "$REPO_ROOT:$REPO_ROOT"
        -w "$REPO_ROOT"
        "$ARTILLERY_DOCKER_IMAGE"
    )
else
    printf "artillery, npx, or docker is required to run this worker\n" >&2
    exit 127
fi

sed \
    -e "s|path: \"./payloads/sdk-compatible-event-bodies.tsv\"|path: \"$PAYLOAD_FILE\"|" \
    -e "s/name: \".* persistent VUs ramp\"/name: \"$WORKER_LABEL $VUS_PER_WORKER persistent VUs\"/" \
    -e "s/duration: [0-9][0-9]*/duration: $RAMP_SECONDS/" \
    -e "s/arrivalCount: [0-9][0-9]*/arrivalCount: $VUS_PER_WORKER/" \
    -e "s/think: [0-9][0-9.]*/think: $VU_THINK_SECONDS/" \
    -e "s/count: [0-9][0-9]*/count: $REQUESTS_PER_VU/" \
    -e "s/timeout: [0-9][0-9.]*/timeout: $HTTP_TIMEOUT_SECONDS/" \
    "$BASE_SCENARIO" >"$SCENARIO"

cat >"$STEP_DIR/worker-plan.json" <<JSON
{
  "workerLabel": "$WORKER_LABEL",
  "vusPerWorker": $VUS_PER_WORKER,
  "rampSeconds": $RAMP_SECONDS,
  "requestsPerVu": $REQUESTS_PER_VU,
  "vuThinkSeconds": "$VU_THINK_SECONDS",
  "httpTimeoutSeconds": "$HTTP_TIMEOUT_SECONDS",
  "expectedRequests": $EXPECTED_REQUESTS,
  "targetBaseUrl": "$TARGET_BASE_URL"
}
JSON

printf "running keep-alive worker %s: %s VUs * %s requests/VU = %s requests\n" \
    "$WORKER_LABEL" "$VUS_PER_WORKER" "$REQUESTS_PER_VU" "$EXPECTED_REQUESTS" | tee "$STEP_DIR/summary.log"

date -u '+%Y-%m-%dT%H:%M:%SZ' >"$STEP_DIR/artillery-started-at.txt"
"${ARTILLERY[@]}" run \
    --target "$TARGET_BASE_URL" \
    --output "$STEP_DIR/worker.json" \
    "$SCENARIO" >"$STEP_DIR/worker.log" 2>&1
date -u '+%Y-%m-%dT%H:%M:%SZ' >"$STEP_DIR/artillery-finished-at.txt"

if ! command -v jq >/dev/null 2>&1; then
    printf "jq is required to validate Artillery JSON output for worker %s\n" "$WORKER_LABEL" >&2
    exit 127
fi

jq --arg workerLabel "$WORKER_LABEL" --argjson expectedRequests "$EXPECTED_REQUESTS" '
    def counter($name): .aggregate.counters[$name] // 0;
    def errorCount:
      ([.aggregate.counters | to_entries[] | select(.key | startswith("errors.")) | .value] | add) // 0;
    {
      workerLabel: $workerLabel,
      expectedRequests: $expectedRequests,
      requests: counter("http.requests"),
      responses: counter("http.responses"),
      codes204: counter("http.codes.204"),
      errors: errorCount,
      vusersCompleted: counter("vusers.completed"),
      vusersFailed: counter("vusers.failed"),
      responseTime: (.aggregate.summaries["http.response_time"] // {})
    } as $summary
    | $summary + {
        successRate: (if $summary.requests > 0 then ($summary.codes204 / $summary.requests) else 0 end)
      }
' "$STEP_DIR/worker.json" >"$STEP_DIR/worker-summary.json"

requests="$(jq -r '.requests' "$STEP_DIR/worker-summary.json")"
codes204="$(jq -r '.codes204' "$STEP_DIR/worker-summary.json")"
errors="$(jq -r '.errors' "$STEP_DIR/worker-summary.json")"
vusers_failed="$(jq -r '.vusersFailed' "$STEP_DIR/worker-summary.json")"

if (( requests != EXPECTED_REQUESTS || codes204 != requests || errors != 0 || vusers_failed != 0 )); then
    printf "worker %s failed: expected=%s requests=%s codes204=%s errors=%s vusersFailed=%s\n" \
        "$WORKER_LABEL" "$EXPECTED_REQUESTS" "$requests" "$codes204" "$errors" "$vusers_failed" | tee -a "$STEP_DIR/summary.log" >&2
    exit 1
fi

printf "worker %s passed: requests=%s codes204=%s\n" "$WORKER_LABEL" "$requests" "$codes204" | tee -a "$STEP_DIR/summary.log"

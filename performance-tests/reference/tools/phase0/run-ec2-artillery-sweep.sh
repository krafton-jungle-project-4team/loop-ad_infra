#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BASE_SCENARIO="$SCRIPT_DIR/alb-fixed-response.yml"
PAYLOAD_FILE="$SCRIPT_DIR/payloads/sdk-compatible-event-bodies.tsv"

: "${TARGET_BASE_URL:?Set TARGET_BASE_URL to Phase0LoadGeneratorTargetBaseUrl.}"

RUN_ID="${RUN_ID:-run_$(date +%Y%m%d_%H%M%S)_phase0_alb_ec2_sweep}"
if [[ "$RUN_ID" == performance-tests/* ]]; then
    RUN_DIR="$REPO_ROOT/$RUN_ID"
else
    RUN_DIR="$REPO_ROOT/performance-tests/$RUN_ID"
fi
MAX_PROCESSES="${MAX_PROCESSES:-4}"
SWEEP_STEPS="${SWEEP_STEPS:-p1_500:1:500:60 p2_750:2:750:90 p3_1000:3:1000:120 p4_1250:4:1250:180}"

mkdir -p "$RUN_DIR"

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
    printf "artillery, npx, or docker is required to run this sweep\n" >&2
    exit 127
fi

validate_step_results() {
    local label="$1"
    local step_dir="$2"
    local expected_workers="$3"

    if ! command -v jq >/dev/null 2>&1; then
        printf "jq is required to validate Artillery JSON output for step %s\n" "$label" >&2
        return 127
    fi

    local json_files=("$step_dir"/worker_*.json)
    if [[ ! -e "${json_files[0]}" ]]; then
        printf "step %s failed: no worker JSON files were written\n" "$label" >&2
        return 1
    fi
    if (( ${#json_files[@]} != expected_workers )); then
        printf "step %s failed: expected %s worker JSON files, found %s\n" "$label" "$expected_workers" "${#json_files[@]}" >&2
        return 1
    fi

    jq -s --arg label "$label" --argjson expectedWorkers "$expected_workers" '
        def counter($name): [.[].aggregate.counters[$name] // 0] | add;
        {
          label: $label,
          expectedWorkers: $expectedWorkers,
          jsonFiles: length,
          requests: counter("http.requests"),
          responses: counter("http.responses"),
          codes204: counter("http.codes.204"),
          socketTimeouts: counter("errors.ERR_SOCKET_TIMEOUT"),
          vusersCompleted: counter("vusers.completed"),
          vusersFailed: counter("vusers.failed")
        } as $summary
        | $summary + {
            successRate: (if $summary.requests > 0 then ($summary.codes204 / $summary.requests) else 0 end)
          }
    ' "${json_files[@]}" >"$step_dir/step-summary.json"

    local requests codes204 socket_timeouts vusers_failed
    requests="$(jq -r '.requests' "$step_dir/step-summary.json")"
    codes204="$(jq -r '.codes204' "$step_dir/step-summary.json")"
    socket_timeouts="$(jq -r '.socketTimeouts' "$step_dir/step-summary.json")"
    vusers_failed="$(jq -r '.vusersFailed' "$step_dir/step-summary.json")"

    if (( requests == 0 )); then
        printf "step %s failed: Artillery recorded zero requests\n" "$label" >&2
        return 1
    fi
    if (( codes204 != requests || socket_timeouts != 0 || vusers_failed != 0 )); then
        printf "step %s failed fixed-response criteria: requests=%s codes204=%s socketTimeouts=%s vusersFailed=%s\n" \
            "$label" "$requests" "$codes204" "$socket_timeouts" "$vusers_failed" | tee -a "$step_dir/summary.log" >&2
        return 1
    fi

    printf "step %s passed fixed-response criteria: requests=%s codes204=%s\n" \
        "$label" "$requests" "$codes204" | tee -a "$step_dir/summary.log"
}

printf "label\tprocesses\tper_process_rps\ttotal_rps\tduration_seconds\n" >"$RUN_DIR/sweep-plan.tsv"

for step in $SWEEP_STEPS; do
    IFS=':' read -r label process_count per_process_rps duration_seconds <<<"$step"

    if (( process_count > MAX_PROCESSES )); then
        printf "refusing step %s: process_count=%s exceeds MAX_PROCESSES=%s\n" "$label" "$process_count" "$MAX_PROCESSES" >&2
        exit 2
    fi

    total_rps=$((process_count * per_process_rps))
    step_dir="$RUN_DIR/$label"
    scenario="$step_dir/scenario.yml"
    mkdir -p "$step_dir"

    printf "%s\t%s\t%s\t%s\t%s\n" "$label" "$process_count" "$per_process_rps" "$total_rps" "$duration_seconds" >>"$RUN_DIR/sweep-plan.tsv"

    sed \
        -e "s|path: \"./payloads/sdk-compatible-event-bodies.tsv\"|path: \"$PAYLOAD_FILE\"|" \
        -e "s/name: \"single Artillery process 2500 rps hold\"/name: \"$label $per_process_rps rps per process\"/" \
        -e "s/duration: 300/duration: $duration_seconds/" \
        -e "s/arrivalRate: 2500/arrivalRate: $per_process_rps/" \
        "$BASE_SCENARIO" >"$scenario"

    printf "running %s: %s processes * %s rps = %s rps for %s seconds\n" \
        "$label" "$process_count" "$per_process_rps" "$total_rps" "$duration_seconds" | tee "$step_dir/summary.log"

    pids=()
    for worker_index in $(seq 1 "$process_count"); do
        "${ARTILLERY[@]}" run \
            --target "$TARGET_BASE_URL" \
            --output "$step_dir/worker_${worker_index}.json" \
            "$scenario" >"$step_dir/worker_${worker_index}.log" 2>&1 &
        pids+=("$!")
    done

    failed=0
    for pid in "${pids[@]}"; do
        if ! wait "$pid"; then
            failed=1
        fi
    done

    if (( failed != 0 )); then
        printf "step %s failed. Check %s/worker_*.log\n" "$label" "$step_dir" >&2
        exit 1
    fi

    if ! validate_step_results "$label" "$step_dir" "$process_count"; then
        printf "step %s failed. Check %s/step-summary.json and %s/worker_*.log\n" "$label" "$step_dir" "$step_dir" >&2
        exit 1
    fi
done

printf "sweep complete: %s\n" "$RUN_DIR"

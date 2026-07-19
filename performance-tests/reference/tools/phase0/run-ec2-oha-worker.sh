#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

: "${TARGET_BASE_URL:?Set TARGET_BASE_URL to Phase0LoadGeneratorTargetBaseUrl.}"

RUN_ID="${RUN_ID:-run_$(date +%Y%m%d_%H%M%S)_phase0_alb_oha}"
HOST_LABEL="${HOST_LABEL:-$(hostname -s 2>/dev/null || printf "host")}"
CONNECTIONS_PER_HOST="${CONNECTIONS_PER_HOST:-3125}"
REQUESTS_PER_HOST="${REQUESTS_PER_HOST:-2343750}"
DURATION_SECONDS="${DURATION_SECONDS:-0}"
BODY_POOL_FILE="${BODY_POOL_FILE:-}"
QUERY_PER_SECOND="${QUERY_PER_SECOND:-0}"
HTTP_TIMEOUT_SECONDS="${HTTP_TIMEOUT_SECONDS:-10}"
TELEMETRY_INTERVAL_SECONDS="${TELEMETRY_INTERVAL_SECONDS:-5}"
START_EPOCH="${START_EPOCH:-0}"
OHA_IMAGE="${OHA_IMAGE:-ghcr.io/hatoo/oha@sha256:76c300321fd0101d7e0588ae0486956a83034d7057a37be052619fa28204a072}"

load_limit=(-n "$REQUESTS_PER_HOST")
load_mode="request-count"
if (( DURATION_SECONDS > 0 )); then
    load_limit=(-z "${DURATION_SECONDS}s" --wait-ongoing-requests-after-deadline)
    load_mode="duration"
fi

rate_args=()
rate_mode="unlimited"
if ! awk -v qps="$QUERY_PER_SECOND" 'BEGIN { exit !(qps ~ /^[0-9]+([.][0-9]+)?$/ && qps >= 0) }'; then
    printf 'QUERY_PER_SECOND must be a non-negative number, got %s\n' "$QUERY_PER_SECOND" >&2
    exit 1
fi
if awk -v qps="$QUERY_PER_SECOND" 'BEGIN { exit !(qps > 0) }'; then
    rate_args=(-q "$QUERY_PER_SECOND" --latency-correction)
    rate_mode="fixed-qps"
fi

if [[ "$RUN_ID" == performance-tests/* ]]; then
    RUN_DIR="$REPO_ROOT/$RUN_ID"
else
    RUN_DIR="$REPO_ROOT/performance-tests/$RUN_ID"
fi

HOST_DIR="$RUN_DIR/$HOST_LABEL"
SERIALIZED_PAYLOAD_POOL="$SCRIPT_DIR/payloads/sdk-compatible-event-bodies.tsv"
mkdir -p "$HOST_DIR"

telemetry_pid=""

stop_host_telemetry() {
    if [[ -n "$telemetry_pid" ]] && kill -0 "$telemetry_pid" 2>/dev/null; then
        kill "$telemetry_pid" 2>/dev/null || true
        wait "$telemetry_pid" 2>/dev/null || true
    fi
    telemetry_pid=""
}

capture_host_telemetry() {
    local host_output="$HOST_DIR/host-telemetry.tsv"
    local process_output="$HOST_DIR/oha-process-telemetry.tsv"

    printf '%b\n' \
        'timestamp_utc\tcpu_user\tcpu_nice\tcpu_system\tcpu_idle\tcpu_iowait\tcpu_irq\tcpu_softirq\tcpu_steal\tload1\tmem_available_kib\trx_bytes\ttx_bytes\tsockets_used\ttcp_inuse\ttcp_orphan\ttcp_tw\ttcp_alloc\ttcp_mem' \
        >"$host_output"
    printf '%b\n' 'timestamp_utc\tpid\tppid\tpcpu\trss_kib\telapsed_seconds\tcommand' >"$process_output"

    while true; do
        local timestamp cpu_label cpu_user cpu_nice cpu_system cpu_idle cpu_iowait cpu_irq cpu_softirq cpu_steal
        local load1 mem_available_kib rx_bytes tx_bytes sockets_used tcp_inuse tcp_orphan tcp_tw tcp_alloc tcp_mem

        timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        read -r cpu_label cpu_user cpu_nice cpu_system cpu_idle cpu_iowait cpu_irq cpu_softirq cpu_steal _ </proc/stat
        load1="$(awk '{ print $1 }' /proc/loadavg)"
        mem_available_kib="$(awk '$1 == "MemAvailable:" { print $2 }' /proc/meminfo)"
        read -r rx_bytes tx_bytes < <(
            awk 'NR > 2 { gsub(/:/, "", $1); if ($1 != "lo") { rx += $2; tx += $10 } } END { print rx + 0, tx + 0 }' /proc/net/dev
        )
        sockets_used="$(awk '$1 == "sockets:" { print $3 }' /proc/net/sockstat)"
        read -r tcp_inuse tcp_orphan tcp_tw tcp_alloc tcp_mem < <(
            awk '$1 == "TCP:" {
                for (field_index = 2; field_index <= NF; field_index += 2) values[$field_index] = $(field_index + 1)
                print values["inuse"] + 0, values["orphan"] + 0, values["tw"] + 0, values["alloc"] + 0, values["mem"] + 0
            }' /proc/net/sockstat
        )

        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$timestamp" "$cpu_user" "$cpu_nice" "$cpu_system" "$cpu_idle" "$cpu_iowait" "$cpu_irq" "$cpu_softirq" "$cpu_steal" \
            "$load1" "$mem_available_kib" "$rx_bytes" "$tx_bytes" "$sockets_used" "$tcp_inuse" "$tcp_orphan" "$tcp_tw" "$tcp_alloc" "$tcp_mem" \
            >>"$host_output"

        ps -eo pid=,ppid=,pcpu=,rss=,etimes=,comm=,args= \
            | awk -v timestamp="$timestamp" '$6 == "oha" || index($0, "/oha ") > 0 {
                print timestamp "\t" $1 "\t" $2 "\t" $3 "\t" $4 "\t" $5 "\t" $6
            }' >>"$process_output"

        sleep "$TELEMETRY_INTERVAL_SECONDS"
    done
}

trap stop_host_telemetry EXIT

body_mode="single-file"
body_pool_rows=1
body_min_bytes=1476
body_max_bytes=1476
body_source="$SERIALIZED_PAYLOAD_POOL"
body_mount="$HOST_DIR/oha-body.json:/payload.json:ro"
body_args=(-D /payload.json)

if [[ -n "$BODY_POOL_FILE" ]]; then
    body_mode="random-line-pool"
    if [[ "$BODY_POOL_FILE" == /* ]]; then
        body_source="$BODY_POOL_FILE"
    else
        body_source="$REPO_ROOT/$BODY_POOL_FILE"
    fi
    cp "$body_source" "$HOST_DIR/oha-body-pool.ndjson"
    body_pool_rows=0
    body_min_bytes=0
    body_max_bytes=0
    while IFS= read -r body; do
        [[ -n "$body" ]] || continue
        printf '%s\n' "$body" | jq -e . >/dev/null
        body_bytes="$(printf '%s' "$body" | wc -c | tr -d ' ')"
        if (( body_bytes < 1024 || body_bytes > 1536 )); then
            printf 'payload pool row must be 1024-1536 bytes, got %s bytes\n' "$body_bytes" >&2
            exit 1
        fi
        body_pool_rows="$((body_pool_rows + 1))"
        if (( body_min_bytes == 0 || body_bytes < body_min_bytes )); then
            body_min_bytes="$body_bytes"
        fi
        if (( body_bytes > body_max_bytes )); then
            body_max_bytes="$body_bytes"
        fi
    done <"$HOST_DIR/oha-body-pool.ndjson"
    if (( body_pool_rows < 2 )); then
        printf 'payload pool must contain at least two non-empty JSON lines\n' >&2
        exit 1
    fi
    sha256sum "$HOST_DIR/oha-body-pool.ndjson" >"$HOST_DIR/oha-body-pool.sha256"
    body_mount="$HOST_DIR/oha-body-pool.ndjson:/payload-pool.ndjson:ro"
    body_args=(-Z /payload-pool.ndjson)
else
    awk -F '\t' 'NR > 1 && $3 == 1476 { printf "%s", $1; found = 1; exit } END { if (!found) exit 1 }' \
        "$SERIALIZED_PAYLOAD_POOL" >"$HOST_DIR/oha-body.json"
    body_bytes="$(wc -c <"$HOST_DIR/oha-body.json" | tr -d ' ')"
    if [[ "$body_bytes" != "1476" ]]; then
        printf 'expected a 1476-byte request body, got %s bytes\n' "$body_bytes" >&2
        exit 1
    fi
    sha256sum "$HOST_DIR/oha-body.json" >"$HOST_DIR/oha-body.sha256"
fi

cat >"$HOST_DIR/host-plan.json" <<JSON
{
  "hostLabel": "$HOST_LABEL",
  "connections": $CONNECTIONS_PER_HOST,
  "requests": $REQUESTS_PER_HOST,
  "durationSeconds": $DURATION_SECONDS,
  "loadMode": "$load_mode",
  "rateMode": "$rate_mode",
  "queryPerSecond": $QUERY_PER_SECOND,
  "httpTimeoutSeconds": $HTTP_TIMEOUT_SECONDS,
  "telemetryIntervalSeconds": $TELEMETRY_INTERVAL_SECONDS,
  "body": {
    "mode": "$body_mode",
    "poolRows": $body_pool_rows,
    "minBytes": $body_min_bytes,
    "maxBytes": $body_max_bytes,
    "source": "$body_source"
  },
  "startEpoch": $START_EPOCH,
  "targetBaseUrl": "$TARGET_BASE_URL",
  "ohaImage": "$OHA_IMAGE"
}
JSON

if (( START_EPOCH > 0 )); then
    now_epoch="$(date +%s)"
    if (( START_EPOCH > now_epoch )); then
        sleep "$((START_EPOCH - now_epoch))"
    fi
fi

cp /proc/net/sockstat "$HOST_DIR/sockstat-start.txt"
if command -v ss >/dev/null 2>&1; then
    ss -s >"$HOST_DIR/ss-start.txt"
fi
capture_host_telemetry &
telemetry_pid="$!"

started_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
started_epoch="$(date +%s)"
set +e
docker run --rm --network host --ulimit nofile=1048576:1048576 \
    -v "$body_mount" \
    "$OHA_IMAGE" \
    --no-tui \
    --output-format json \
    "${load_limit[@]}" \
    "${rate_args[@]}" \
    -c "$CONNECTIONS_PER_HOST" \
    -m POST \
    -T application/json \
    "${body_args[@]}" \
    -t "${HTTP_TIMEOUT_SECONDS}s" \
    "$TARGET_BASE_URL/__fixed" \
    >"$HOST_DIR/oha-report.json" \
    2>"$HOST_DIR/oha.log"
oha_rc="$?"
set -e
ended_epoch="$(date +%s)"
ended_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

stop_host_telemetry
cp /proc/net/sockstat "$HOST_DIR/sockstat-end.txt"
if command -v ss >/dev/null 2>&1; then
    ss -s >"$HOST_DIR/ss-end.txt"
fi

jq empty "$HOST_DIR/oha-report.json"
jq \
    --arg hostLabel "$HOST_LABEL" \
    --arg startedAt "$started_at" \
    --arg endedAt "$ended_at" \
    --argjson startedEpoch "$started_epoch" \
    --argjson endedEpoch "$ended_epoch" \
    --argjson exitCode "$oha_rc" \
    '([.statusCodeDistribution[]?] | add // 0) as $completed
    | ([.errorDistribution[]?] | add // 0) as $errors
    | {
      hostLabel: $hostLabel,
      startedAt: $startedAt,
      endedAt: $endedAt,
      durationSeconds: ($endedEpoch - $startedEpoch),
      exitCode: $exitCode,
      completedRequests: $completed,
      attemptedRequests: ($completed + $errors),
      errorCount: $errors,
      effectiveSuccessRate: (if ($completed + $errors) > 0 then $completed / ($completed + $errors) else 0 end),
      summary,
      latencyPercentiles,
      statusCodeDistribution,
      errorDistribution
    }' "$HOST_DIR/oha-report.json" >"$HOST_DIR/oha-summary.json"

completed_requests="$(jq -r '[.statusCodeDistribution[]?] | add // 0' "$HOST_DIR/oha-report.json")"
error_count="$(jq -r '[.errorDistribution[]?] | add // 0' "$HOST_DIR/oha-report.json")"
attempted_requests="$((completed_requests + error_count))"
effective_success_rate="$(awk -v completed="$completed_requests" -v attempted="$attempted_requests" 'BEGIN {
    if (attempted > 0) print completed / attempted; else print 0
}')"
codes204="$(jq -r '.statusCodeDistribution["204"] // 0' "$HOST_DIR/oha-report.json")"

request_count_failed=0
error_count_failed=0
if [[ "$load_mode" == "request-count" ]]; then
    if (( completed_requests != REQUESTS_PER_HOST )); then
        request_count_failed=1
    fi
    if (( error_count != 0 )); then
        error_count_failed=1
    fi
fi

if (( oha_rc != 0 || request_count_failed != 0 || error_count_failed != 0 || completed_requests <= 0 || codes204 != completed_requests )) \
    || ! awk -v rate="$effective_success_rate" 'BEGIN { exit !(rate >= 0.999) }'; then
    printf 'host %s failed: rc=%s completed=%s codes204=%s errors=%s successRate=%s\n' \
        "$HOST_LABEL" "$oha_rc" "$completed_requests" "$codes204" "$error_count" "$effective_success_rate" >&2
    exit 1
fi

printf 'host %s passed: completed=%s attempted=%s codes204=%s errors=%s effectiveSuccessRate=%s\n' \
    "$HOST_LABEL" "$completed_requests" "$attempted_requests" "$codes204" "$error_count" "$effective_success_rate"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

: "${TARGET_BASE_URL:?Set TARGET_BASE_URL to Phase0LoadGeneratorTargetBaseUrl.}"

RUN_ID="${RUN_ID:-run_$(date +%Y%m%d_%H%M%S)_phase0_alb_keepalive_multiprocess}"
HOST_LABEL="${HOST_LABEL:-$(hostname -s 2>/dev/null || printf "host")}"
PROCESSES_PER_HOST="${PROCESSES_PER_HOST:-5}"
VUS_PER_PROCESS="${VUS_PER_PROCESS:-625}"
RAMP_SECONDS="${RAMP_SECONDS:-30}"
REQUESTS_PER_VU="${REQUESTS_PER_VU:-750}"
VU_THINK_SECONDS="${VU_THINK_SECONDS:-0.15}"
HTTP_TIMEOUT_SECONDS="${HTTP_TIMEOUT_SECONDS:-10}"
TELEMETRY_INTERVAL_SECONDS="${TELEMETRY_INTERVAL_SECONDS:-5}"

if [[ "$RUN_ID" == performance-tests/* ]]; then
    RUN_DIR="$REPO_ROOT/$RUN_ID"
else
    RUN_DIR="$REPO_ROOT/performance-tests/$RUN_ID"
fi

HOST_DIR="$RUN_DIR/$HOST_LABEL"
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
    local process_output="$HOST_DIR/artillery-process-telemetry.tsv"

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
            | awk -v timestamp="$timestamp" 'index($0, "artillery") > 0 && ($6 == "node" || $6 == "nodejs") {
                print timestamp "\t" $1 "\t" $2 "\t" $3 "\t" $4 "\t" $5 "\t" $6
            }' >>"$process_output"

        sleep "$TELEMETRY_INTERVAL_SECONDS"
    done
}

trap stop_host_telemetry EXIT

cp /proc/net/sockstat "$HOST_DIR/sockstat-start.txt"
if command -v ss >/dev/null 2>&1; then
    ss -s >"$HOST_DIR/ss-start.txt"
fi
capture_host_telemetry &
telemetry_pid="$!"

cat >"$HOST_DIR/host-plan.json" <<JSON
{
  "hostLabel": "$HOST_LABEL",
  "processesPerHost": $PROCESSES_PER_HOST,
  "vusPerProcess": $VUS_PER_PROCESS,
  "rampSeconds": $RAMP_SECONDS,
  "requestsPerVu": $REQUESTS_PER_VU,
  "vuThinkSeconds": "$VU_THINK_SECONDS",
  "httpTimeoutSeconds": "$HTTP_TIMEOUT_SECONDS",
  "telemetryIntervalSeconds": $TELEMETRY_INTERVAL_SECONDS,
  "expectedRequests": $((PROCESSES_PER_HOST * VUS_PER_PROCESS * REQUESTS_PER_VU)),
  "targetBaseUrl": "$TARGET_BASE_URL"
}
JSON

pids=()
for process_index in $(seq 1 "$PROCESSES_PER_HOST"); do
    WORKER_LABEL="${HOST_LABEL}-p${process_index}" \
    VUS_PER_WORKER="$VUS_PER_PROCESS" \
    RAMP_SECONDS="$RAMP_SECONDS" \
    REQUESTS_PER_VU="$REQUESTS_PER_VU" \
    VU_THINK_SECONDS="$VU_THINK_SECONDS" \
    HTTP_TIMEOUT_SECONDS="$HTTP_TIMEOUT_SECONDS" \
    RUN_ID="$RUN_ID" \
    TARGET_BASE_URL="$TARGET_BASE_URL" \
    "$SCRIPT_DIR/run-ec2-artillery-keepalive-worker.sh" &
    pids+=("$!")
done

failed=0
for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
        failed=1
    fi
done

stop_host_telemetry
cp /proc/net/sockstat "$HOST_DIR/sockstat-end.txt"
if command -v ss >/dev/null 2>&1; then
    ss -s >"$HOST_DIR/ss-end.txt"
fi

json_files=()
for process_index in $(seq 1 "$PROCESSES_PER_HOST"); do
    json_files+=("$RUN_DIR/${HOST_LABEL}-p${process_index}/worker-summary.json")
done

jq -s --arg hostLabel "$HOST_LABEL" --argjson processesPerHost "$PROCESSES_PER_HOST" '
    def sum_field($name): [.[].[$name] // 0] | add;
    {
      hostLabel: $hostLabel,
      processesPerHost: $processesPerHost,
      requests: sum_field("requests"),
      responses: sum_field("responses"),
      codes204: sum_field("codes204"),
      errors: sum_field("errors"),
      vusersCompleted: sum_field("vusersCompleted"),
      vusersFailed: sum_field("vusersFailed"),
      responseTime: {
        p50Max: ([.[].responseTime.p50 // 0] | max),
        p95Max: ([.[].responseTime.p95 // 0] | max),
        p99Max: ([.[].responseTime.p99 // 0] | max),
        max: ([.[].responseTime.max // 0] | max)
      }
    } as $summary
    | $summary + {
        successRate: (if $summary.requests > 0 then ($summary.codes204 / $summary.requests) else 0 end)
      }
' "${json_files[@]}" >"$HOST_DIR/host-summary.json"

requests="$(jq -r '.requests' "$HOST_DIR/host-summary.json")"
codes204="$(jq -r '.codes204' "$HOST_DIR/host-summary.json")"
errors="$(jq -r '.errors' "$HOST_DIR/host-summary.json")"
vusers_failed="$(jq -r '.vusersFailed' "$HOST_DIR/host-summary.json")"

if (( failed != 0 || codes204 != requests || errors != 0 || vusers_failed != 0 )); then
    printf "host %s failed: failed=%s requests=%s codes204=%s errors=%s vusersFailed=%s\n" \
        "$HOST_LABEL" "$failed" "$requests" "$codes204" "$errors" "$vusers_failed" >&2
    exit 1
fi

printf "host %s passed: processes=%s requests=%s codes204=%s\n" \
    "$HOST_LABEL" "$PROCESSES_PER_HOST" "$requests" "$codes204"

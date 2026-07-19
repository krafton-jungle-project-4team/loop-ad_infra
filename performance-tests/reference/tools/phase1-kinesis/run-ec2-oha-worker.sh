#!/usr/bin/env bash
set -euo pipefail

: "${TARGET_BASE_URL:?Set the internal Phase 1 ALB base URL.}"
: "${RUN_ID:?Set the Phase 1 run ID.}"
: "${QUERY_PER_SECOND:?Set the target requests per second.}"
: "${DURATION_SECONDS:?Set the bounded load duration.}"
: "${PAYLOAD_GZIP_BASE64:?Set the locally verified gzip+base64 payload pool.}"

CONNECTIONS="${CONNECTIONS:-4096}"
PROCESSES_PER_NODE="${PROCESSES_PER_NODE:-1}"
EXPERIMENT_MODE="${EXPERIMENT_MODE:-legacy}"
HTTP_TIMEOUT_SECONDS="${HTTP_TIMEOUT_SECONDS:-10}"
START_EPOCH="${START_EPOCH:-0}"
TELEMETRY_INTERVAL_SECONDS="${TELEMETRY_INTERVAL_SECONDS:-5}"
PERSISTENT_WARMUP_SECONDS="${PERSISTENT_WARMUP_SECONDS:-0}"
MEASUREMENT_SECONDS="${MEASUREMENT_SECONDS:-0}"
CONTROL_WAIT_SECONDS="${CONTROL_WAIT_SECONDS:-90}"
NODE_ID="${NODE_ID:-node-01}"
INSTANCE_ID="${INSTANCE_ID:-}"
TARGET_PATH="${TARGET_PATH:-/events}"
STAGE_LABEL="${STAGE_LABEL:-unspecified}"
REPETITION="${REPETITION:-1}"
INSTRUMENTATION="${INSTRUMENTATION:-on}"
OHA_HTTP_VERSION="${OHA_HTTP_VERSION:-1.1}"
OHA_PARALLEL="${OHA_PARALLEL:-1}"
PROCESS_START_SPACING_SECONDS="${PROCESS_START_SPACING_SECONDS:-0}"
PROCESS_START_OFFSET_SECONDS="${PROCESS_START_OFFSET_SECONDS:-0}"
TARGET_CONNECT_BASE_URL="${TARGET_CONNECT_BASE_URL:-$TARGET_BASE_URL}"
TARGET_HOST_HEADER="${TARGET_HOST_HEADER:-}"
TLS_SERVER_NAME="${TLS_SERVER_NAME:-}"
CA_CERTIFICATE_BASE64="${CA_CERTIFICATE_BASE64:-}"
CA_CERTIFICATE_SHA256="${CA_CERTIFICATE_SHA256:-}"
EVIDENCE_BUCKET="${EVIDENCE_BUCKET:-}"
EVIDENCE_PREFIX="${EVIDENCE_PREFIX:-}"
EXPECTED_POOL_SHA256="${EXPECTED_POOL_SHA256:-f82cd61548b1be8d5df21a91b8e86390422e4d433ac6dc93d87414a3755336c2}"
OHA_IMAGE="${OHA_IMAGE:-ghcr.io/hatoo/oha@sha256:76c300321fd0101d7e0588ae0486956a83034d7057a37be052619fa28204a072}"
WORK_ROOT="/tmp/loop-ad-phase1"
RUN_DIR="$WORK_ROOT/$RUN_ID"
ATTEMPT_LOCK_ROOT="/var/lib/loopad-phase1/attempt-locks"
ATTEMPT_LOCK="$ATTEMPT_LOCK_ROOT/$RUN_ID"
POOL_FILE="$WORK_ROOT/sdk-compatible-event-bodies.ndjson"
PHASE7_MODE='false'
PHASE7_MAIN_RUN_ID=''
telemetry_pid=''
capture_pid=''
capture_finalized='false'
worker_termination_signal=''
cleanup_started='false'
process_pids=()
process_ids=()
container_names=()
PATH_EVIDENCE_MODE='false'
if [[ "$EXPERIMENT_MODE" == 'tcp-alb-path-diagnosis' || "$EXPERIMENT_MODE" == 'admission-scaleout-capacity' || "$EXPERIMENT_MODE" == 'connection-path-crossover' ]]; then
    PATH_EVIDENCE_MODE='true'
fi

[[ "$RUN_ID" =~ ^run_[0-9]{8}_[0-9]{6}_[a-z0-9][a-z0-9_-]{0,31}$ ]] || { printf 'invalid RUN_ID\n' >&2; exit 2; }
if [[ "$EXPERIMENT_MODE" == 'connection-path-crossover' ]]; then
    [[ "$TARGET_BASE_URL" =~ ^https://[a-z0-9.-]+$ ]] || { printf 'connection-path canonical TLS URL is invalid\n' >&2; exit 2; }
    [[ "$TARGET_CONNECT_BASE_URL" =~ ^https://(internal-perf-p1-conn-alb|(internal-)?perf-p1-conn-(direct|proxy))-[a-z0-9-]+\.(ap-northeast-2\.elb\.amazonaws\.com|elb\.ap-northeast-2\.amazonaws\.com)$ ]] || { printf 'connection-path destination is not run-scoped\n' >&2; exit 2; }
else
    [[ "$TARGET_BASE_URL" =~ ^http://internal-(perf-phase1-loop-ad-alb|perf-phase1-generator-control)-[a-z0-9-]+\.ap-northeast-2\.elb\.amazonaws\.com$ ]] || { printf 'TARGET_BASE_URL is not an allowed Phase 1 internal ALB\n' >&2; exit 2; }
fi
[[ "$TARGET_PATH" == "/events" || "$TARGET_PATH" == "/__generator_control" || "$TARGET_PATH" == "/__proxy_control" || "$TARGET_PATH" == "/__collector_control" || "$TARGET_PATH" == "/__decode_control" || "$TARGET_PATH" =~ ^/__delay_control\?delay=(0|50|100|200|300)$ || "$TARGET_PATH" =~ ^/__batch_control\?ack_delay=(0|20|50)$ ]] || { printf 'TARGET_PATH is not allowed\n' >&2; exit 2; }
[[ "$STAGE_LABEL" =~ ^[a-z0-9][a-z0-9_-]{0,31}$ && "$REPETITION" =~ ^[0-9]{1,3}$ ]] || { printf 'stage metadata is invalid\n' >&2; exit 2; }
[[ "$INSTRUMENTATION" == 'on' || "$INSTRUMENTATION" == 'off' ]] || { printf 'INSTRUMENTATION is invalid\n' >&2; exit 2; }
[[ "$EXPERIMENT_MODE" == 'connection-path-crossover' || "$TARGET_CONNECT_BASE_URL" == "$TARGET_BASE_URL" || "$TARGET_CONNECT_BASE_URL" =~ ^http://10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(:8080)?$ ]] || { printf 'TARGET_CONNECT_BASE_URL is not allowed\n' >&2; exit 2; }
if [[ "$TARGET_CONNECT_BASE_URL" != "$TARGET_BASE_URL" ]]; then
    [[ "$PATH_EVIDENCE_MODE" == 'true' ]] || { printf 'destination pinning requires path-evidence mode\n' >&2; exit 2; }
    expected_host_header="${TARGET_BASE_URL#http://}"
    expected_host_header="${expected_host_header#https://}"
    [[ "$TARGET_HOST_HEADER" == "$expected_host_header" ]] || { printf 'destination pinning must preserve the original Host header\n' >&2; exit 2; }
fi
for numeric in "$QUERY_PER_SECOND" "$DURATION_SECONDS" "$CONNECTIONS" "$PROCESSES_PER_NODE" "$HTTP_TIMEOUT_SECONDS" "$START_EPOCH" "$TELEMETRY_INTERVAL_SECONDS" "$PERSISTENT_WARMUP_SECONDS" "$MEASUREMENT_SECONDS" "$CONTROL_WAIT_SECONDS" "$OHA_PARALLEL" "$PROCESS_START_SPACING_SECONDS" "$PROCESS_START_OFFSET_SECONDS"; do
    [[ "$numeric" =~ ^[0-9]+$ ]] || { printf 'numeric input is invalid: %s\n' "$numeric" >&2; exit 2; }
done
(( QUERY_PER_SECOND > 0 && DURATION_SECONDS > 0 && CONNECTIONS > 0 && HTTP_TIMEOUT_SECONDS > 0 )) || exit 2
if [[ "$EXPERIMENT_MODE" == 'connection-path-crossover' ]]; then
    [[ "$OHA_HTTP_VERSION" == '2' && "$OHA_PARALLEL" == '1' && "$PROCESSES_PER_NODE" == '2' ]] || { printf 'connection-path mode requires HTTP/2, -p 1, and two processes per node\n' >&2; exit 2; }
    [[ "$TLS_SERVER_NAME" =~ ^[a-z0-9.-]+$ && "$TARGET_BASE_URL" == "https://$TLS_SERVER_NAME" ]] || { printf 'TLS server name must equal the canonical URL host\n' >&2; exit 2; }
    [[ "$CA_CERTIFICATE_SHA256" =~ ^[0-9a-f]{64}$ && -n "$CA_CERTIFICATE_BASE64" ]] || { printf 'verified CA certificate is required\n' >&2; exit 2; }
fi
if [[ "$PATH_EVIDENCE_MODE" == 'true' ]]; then
    [[ "$TELEMETRY_INTERVAL_SECONDS" == 1 && -n "$EVIDENCE_BUCKET" && -n "$EVIDENCE_PREFIX" ]] || {
        printf 'path-evidence mode requires 1s telemetry and run-scoped S3 evidence delivery\n' >&2; exit 2;
    }
    [[ "$EVIDENCE_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || { printf 'invalid EVIDENCE_BUCKET\n' >&2; exit 2; }
    [[ "$EVIDENCE_PREFIX" =~ ^[A-Za-z0-9][A-Za-z0-9_./=-]{0,255}$ ]] || { printf 'invalid EVIDENCE_PREFIX\n' >&2; exit 2; }
fi
if (( PERSISTENT_WARMUP_SECONDS > 0 )); then
    if [[ "$EXPERIMENT_MODE" == 'connection-path-crossover' ]]; then
        [[ "$PERSISTENT_WARMUP_SECONDS" == 900 && "$MEASUREMENT_SECONDS" == 300 && "$TELEMETRY_INTERVAL_SECONDS" == 1 ]] || {
            printf 'connection-path persistent mode requires 900s warm-up, 300s measurement, and 1s telemetry\n' >&2; exit 2;
        }
    else
        [[ "$EXPERIMENT_MODE" == "alb-warmup" && "$PERSISTENT_WARMUP_SECONDS" == 120 && "$MEASUREMENT_SECONDS" == 15 && "$TELEMETRY_INTERVAL_SECONDS" == 1 ]] || {
            printf 'persistent mode requires alb-warmup, 120s warm-up, 15s measurement, and 1s telemetry\n' >&2; exit 2;
        }
    fi
    (( DURATION_SECONDS >= PERSISTENT_WARMUP_SECONDS + CONTROL_WAIT_SECONDS + MEASUREMENT_SECONDS + HTTP_TIMEOUT_SECONDS )) || {
        printf 'persistent oha duration does not cover warm-up, control, measurement, and timeout\n' >&2; exit 2;
    }
fi
[[ "$NODE_ID" =~ ^node-[0-9]{2}$ ]] || { printf 'invalid NODE_ID\n' >&2; exit 2; }
[[ "$PROCESSES_PER_NODE" == 1 || "$PROCESSES_PER_NODE" == 2 || "$PROCESSES_PER_NODE" == 4 ]] || { printf 'PROCESSES_PER_NODE must be 1, 2, or 4\n' >&2; exit 2; }
(( QUERY_PER_SECOND % PROCESSES_PER_NODE == 0 && CONNECTIONS % PROCESSES_PER_NODE == 0 )) || { printf 'RPS and connections must divide across processes\n' >&2; exit 2; }
command -v docker >/dev/null
command -v jq >/dev/null
if [[ "$PATH_EVIDENCE_MODE" == 'true' ]]; then
    command -v aws >/dev/null
    command -v dig >/dev/null
    command -v tcpdump >/dev/null
fi

stop_telemetry() {
    if [[ -n "$telemetry_pid" ]] && kill -0 "$telemetry_pid" 2>/dev/null; then
        kill "$telemetry_pid" 2>/dev/null || true
        wait "$telemetry_pid" 2>/dev/null || true
    fi
    telemetry_pid=''
}
stop_packet_capture() {
    if [[ -n "$capture_pid" ]] && kill -0 "$capture_pid" 2>/dev/null; then
        kill -INT "$capture_pid" 2>/dev/null || true
        wait "$capture_pid" 2>/dev/null || true
    fi
    capture_pid=''
}
cleanup_worker() {
    local exit_code="$?"
    local process_pid process_index owned_container cleanup_complete terminal_state_file terminal_state_key
    local owned_container_running_count=0
    local owned_runner_process_count=0
    if [[ "$cleanup_started" == 'true' ]]; then return "$exit_code"; fi
    cleanup_started='true'
    trap - EXIT TERM INT
    set +e

    for process_pid in "${process_pids[@]}"; do
        if [[ "$process_pid" =~ ^[0-9]+$ ]] && kill -0 "$process_pid" 2>/dev/null; then
            pkill -TERM -P "$process_pid" 2>/dev/null || true
            kill -TERM "$process_pid" 2>/dev/null || true
        fi
    done
    for process_index in $(seq 1 "$PROCESSES_PER_NODE"); do
        owned_container="loopad-${RUN_ID}-${NODE_ID}-process-$(printf '%02d' "$process_index")"
        docker rm -f "$owned_container" >/dev/null 2>&1 || true
    done
    for process_pid in "${process_pids[@]}"; do
        if [[ "$process_pid" =~ ^[0-9]+$ ]]; then
            pkill -KILL -P "$process_pid" 2>/dev/null || true
            kill -KILL "$process_pid" 2>/dev/null || true
            wait "$process_pid" 2>/dev/null || true
        fi
    done
    # A second exact-name removal closes the runner/container creation race.
    for process_index in $(seq 1 "$PROCESSES_PER_NODE"); do
        owned_container="loopad-${RUN_ID}-${NODE_ID}-process-$(printf '%02d' "$process_index")"
        docker rm -f "$owned_container" >/dev/null 2>&1 || true
        if [[ "$(docker inspect --format '{{.State.Running}}' "$owned_container" 2>/dev/null)" == 'true' ]]; then
            owned_container_running_count="$((owned_container_running_count + 1))"
        fi
    done
    for process_pid in "${process_pids[@]}"; do
        if [[ "$process_pid" =~ ^[0-9]+$ ]] && kill -0 "$process_pid" 2>/dev/null; then
            owned_runner_process_count="$((owned_runner_process_count + 1))"
        fi
    done
    stop_telemetry
    stop_packet_capture
    if [[ "$capture_finalized" != 'true' ]]; then rm -f "$RUN_DIR/syn-handshake.pcap"; fi

    if [[ "$PHASE7_MODE" == 'true' ]]; then
        cleanup_complete='false'
        if (( owned_container_running_count == 0 && owned_runner_process_count == 0 )); then cleanup_complete='true'; fi
        mkdir -p "$RUN_DIR"
        terminal_state_file="$RUN_DIR/worker-terminal-state.json"
        jq -n \
            --arg mainRunId "$PHASE7_MAIN_RUN_ID" --arg workerRunId "$RUN_ID" --arg stage "$STAGE_LABEL" \
            --arg nodeId "$NODE_ID" --arg instanceId "$INSTANCE_ID" --arg state 'terminal' \
            --arg cleanupComplete "$cleanup_complete" --arg terminationSignal "$worker_termination_signal" \
            --argjson exitCode "$exit_code" --argjson ownedContainerRunningCount "$owned_container_running_count" \
            --argjson ownedRunnerProcessCount "$owned_runner_process_count" \
            '{schemaVersion:1,mainRunId:$mainRunId,workerRunId:$workerRunId,stage:$stage,nodeId:$nodeId,
              instanceId:$instanceId,state:$state,cleanupComplete:($cleanupComplete=="true"),exitCode:$exitCode,
              terminationSignal:(if $terminationSignal=="" then null else $terminationSignal end),
              ownedContainerRunningCount:$ownedContainerRunningCount,ownedRunnerProcessCount:$ownedRunnerProcessCount,
              recordedAt:(now|todateiso8601)}' >"$terminal_state_file"
        cp "$terminal_state_file" "$ATTEMPT_LOCK/worker-terminal-state.json"
        terminal_state_key="${EVIDENCE_PREFIX%/}/${NODE_ID}/worker-terminal-state.json"
        aws s3api put-object --region ap-northeast-2 \
            --bucket "$EVIDENCE_BUCKET" --key "$terminal_state_key" --body "$terminal_state_file" \
            --metadata "schema-version=1,main-run-id=$PHASE7_MAIN_RUN_ID,worker-run-id=$RUN_ID,stage=$STAGE_LABEL,node-id=$NODE_ID,instance-id=$INSTANCE_ID,state=terminal,cleanup-complete=$cleanup_complete,owned-container-running-count=$owned_container_running_count,owned-runner-process-count=$owned_runner_process_count" \
            >"$ATTEMPT_LOCK/worker-terminal-state-put-object.json" 2>"$ATTEMPT_LOCK/worker-terminal-state-put-object.stderr" || true
    fi
    return "$exit_code"
}
handle_worker_signal() {
    worker_termination_signal="$1"
    trap - TERM INT
    exit "$2"
}

if [[ "$RUN_ID" =~ ^run_([0-9]{8}_[0-9]{6})_phase7_(warmup|score)$ ]]; then
    PHASE7_MODE='true'
    PHASE7_MAIN_RUN_ID="run_${BASH_REMATCH[1]}_phase7_integration"
    [[ "${BASH_REMATCH[2]}" == "$STAGE_LABEL" ]] || { printf 'Phase 7 RUN_ID and stage mismatch\n' >&2; exit 2; }
    [[ "$INSTANCE_ID" =~ ^i-[0-9a-f]+$ ]] || { printf 'Phase 7 INSTANCE_ID is invalid\n' >&2; exit 2; }
    [[ "$PATH_EVIDENCE_MODE" == 'true' ]] || { printf 'Phase 7 requires path evidence mode\n' >&2; exit 2; }
    [[ "$EVIDENCE_PREFIX" == "generator-evidence/$PHASE7_MAIN_RUN_ID/$STAGE_LABEL" ]] || {
        printf 'Phase 7 evidence prefix is not bound to the exact run and stage\n' >&2; exit 2;
    }
    # Phase 7 stage RUN_IDs are immutable. Keep this marker after exit so a
    # duplicated or delayed SSM SendCommand can never execute the workload twice.
    install -d -m 0700 "$ATTEMPT_LOCK_ROOT"
    if ! mkdir -m 0700 "$ATTEMPT_LOCK"; then
        printf 'duplicate immutable load attempt refused: %s\n' "$RUN_ID" >&2
        exit 73
    fi
    trap 'handle_worker_signal SIGTERM 143' TERM
    trap 'handle_worker_signal SIGINT 130' INT
    trap cleanup_worker EXIT
    printf '%s\t%s\t%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$NODE_ID" "$$" >"$ATTEMPT_LOCK/owner.tsv"
    jq -n --arg mainRunId "$PHASE7_MAIN_RUN_ID" --arg workerRunId "$RUN_ID" --arg stage "$STAGE_LABEL" \
        --arg nodeId "$NODE_ID" --arg instanceId "$INSTANCE_ID" --arg acquiredAt "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        '{schemaVersion:1,mainRunId:$mainRunId,workerRunId:$workerRunId,stage:$stage,nodeId:$nodeId,instanceId:$instanceId,state:"active",acquiredAt:$acquiredAt}' \
        >"$ATTEMPT_LOCK/attempt-state.json"
fi

target_url="${TARGET_CONNECT_BASE_URL}${TARGET_PATH}"
target_dns_name="${TARGET_CONNECT_BASE_URL#*://}"
target_destination_ip="${TARGET_CONNECT_BASE_URL#*://}"
target_destination_ip="${target_destination_ip%:8080}"
if [[ "$TARGET_CONNECT_BASE_URL" == "$TARGET_BASE_URL" ]]; then target_destination_ip=''; fi
target_port='80'
if [[ "$TARGET_CONNECT_BASE_URL" == https://* ]]; then target_port='443'; fi
if [[ "$TARGET_CONNECT_BASE_URL" == *':8080' ]]; then target_port='8080'; fi
oha_header_args=(-H "X-Loopad-Stage: $STAGE_LABEL" -H "X-Loopad-Repetition: $REPETITION" -H "X-Loopad-Target: $TARGET_PATH" -H "X-Loopad-Instrumentation: $INSTRUMENTATION")
if [[ -n "$TARGET_HOST_HEADER" ]]; then oha_header_args+=(-H "Host: $TARGET_HOST_HEADER"); fi
oha_protocol_args=()
oha_mount_args=()
if [[ "$EXPERIMENT_MODE" == 'connection-path-crossover' ]]; then
    target_url="${TARGET_BASE_URL}${TARGET_PATH}"
    connect_destination="${TARGET_CONNECT_BASE_URL#https://}"
    oha_protocol_args=(--http-version 2 -p 1 --cacert /protocol-ca.pem --connect-to "$TLS_SERVER_NAME:443:$connect_destination:443")
    oha_mount_args=(-v "$WORK_ROOT/protocol-ca.pem:/protocol-ca.pem:ro")
fi

primary_interface="$(ip route show default | awk 'NR==1 {print $5}')"
capture_allowance_counters() {
    local output="$1"
    if [[ -n "$primary_interface" ]] && command -v ethtool >/dev/null; then
        ethtool -S "$primary_interface" 2>/dev/null | awk '
            /allowance_exceeded/ {
                gsub(":", "", $1); printf "%s %s\n", $1, $2
            }
        ' >"$output"
    else
        : >"$output"
    fi
}

capture_ena_diagnostics() {
    local output="$1"
    if [[ -n "$primary_interface" ]] && command -v ethtool >/dev/null; then
        ethtool -S "$primary_interface" 2>/dev/null | awk '
            BEGIN {IGNORECASE=1}
            /drop|error|allowance|fail|miss|timeout/ {
                gsub(":", "", $1); printf "%s %s\n", $1, $2
            }
        ' | sort >"$output"
    else
        : >"$output"
    fi
}

capture_dns_snapshot() {
    local output="$1"
    dig +noall +answer "$target_dns_name" A | awk '
        $4 == "A" {printf "{\"name\":\"%s\",\"ttl\":%s,\"address\":\"%s\"}\n", $1, $2, $5}
    ' | jq -s --arg queriedAt "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg dnsName "$target_dns_name" \
        '{queriedAt:$queriedAt,dnsName:$dnsName,answers:.}' >"$output"
}

capture_instance_identity() {
    local token instance_id mac private_ip subnet_id az
    token="$(curl -fsS -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' http://169.254.169.254/latest/api/token)"
    instance_id="$(curl -fsS -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-id)"
    mac="$(curl -fsS -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/mac)"
    private_ip="$(curl -fsS -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/meta-data/network/interfaces/macs/$mac/local-ipv4s")"
    subnet_id="$(curl -fsS -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/meta-data/network/interfaces/macs/$mac/subnet-id")"
    az="$(curl -fsS -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/placement/availability-zone)"
    jq -n --arg instanceId "$instance_id" --arg interface "$primary_interface" --arg eniMac "$mac" \
        --arg privateIp "$private_ip" --arg subnetId "$subnet_id" --arg availabilityZone "$az" \
        '{instanceId:$instanceId,interface:$interface,eniMac:$eniMac,privateIp:$privateIp,subnetId:$subnetId,availabilityZone:$availabilityZone}' \
        >"$RUN_DIR/instance-network-identity.json"
}

rm -rf "$RUN_DIR"
mkdir -p "$RUN_DIR" "$WORK_ROOT"
if [[ "$EXPERIMENT_MODE" == 'connection-path-crossover' ]]; then
    printf '%s' "$CA_CERTIFICATE_BASE64" | base64 -d >"$WORK_ROOT/protocol-ca.pem"
    unset CA_CERTIFICATE_BASE64
    test "$(sha256sum "$WORK_ROOT/protocol-ca.pem" | awk '{print $1}')" = "$CA_CERTIFICATE_SHA256"
    set +e
    protocol_output="$(timeout 10 openssl s_client -connect "$connect_destination:443" -servername "$TLS_SERVER_NAME" -alpn h2 -CAfile "$WORK_ROOT/protocol-ca.pem" </dev/null 2>&1)"
    protocol_rc="$?"
    set -e
    [[ "$protocol_rc" == '0' || "$protocol_rc" == '124' ]] || { printf '%s\n' "$protocol_output" >&2; exit "$protocol_rc"; }
    negotiated_alpn="$(printf '%s\n' "$protocol_output" | awk -F': ' '/^ALPN protocol:/{print $2; exit}')"
    verify_code="$(printf '%s\n' "$protocol_output" | sed -n 's/^Verify return code: \([0-9][0-9]*\).*/\1/p' | tail -1)"
    if [[ "$negotiated_alpn" != 'h2' || ! "$verify_code" =~ ^[0-9]+$ ]]; then
        printf 'TLS readiness probe incomplete: exit=%s alpn=%s verify=%s\n' "$protocol_rc" "${negotiated_alpn:-missing}" "${verify_code:-missing}" >&2
        exit 4
    fi
    oha_version="$(docker run --rm "$OHA_IMAGE" --version 2>&1 | tail -1)"
    jq -n --arg destination "$connect_destination" --arg serverName "$TLS_SERVER_NAME" --arg alpn "$negotiated_alpn" \
        --arg verifyCode "$verify_code" --arg ohaVersion "$oha_version" --arg image "$OHA_IMAGE" \
        '{destination:$destination,tlsServerName:$serverName,requestedAlpn:"h2",negotiatedAlpn:$alpn,verifyReturnCode:($verifyCode|tonumber),httpVersion:"2",http1Fallbacks:0,ohaParallel:1,ohaVersion:$ohaVersion,ohaImage:$image,passed:($alpn=="h2" and $verifyCode=="0")}' \
        >"$RUN_DIR/protocol-correctness.json"
    jq -e .passed "$RUN_DIR/protocol-correctness.json" >/dev/null
fi
encoded_payload_bytes="${#PAYLOAD_GZIP_BASE64}"
printf '%s' "$PAYLOAD_GZIP_BASE64" | base64 -d | gzip -d >"$POOL_FILE.tmp"
unset PAYLOAD_GZIP_BASE64
mv "$POOL_FILE.tmp" "$POOL_FILE"
printf '%s  %s\n' "$EXPECTED_POOL_SHA256" "$POOL_FILE" | sha256sum -c - >"$RUN_DIR/payload-sha256.txt"
jq -n --arg delivery 'ssm-gzip-base64' --arg sha256 "$EXPECTED_POOL_SHA256" \
    --argjson encodedPayloadBytes "$encoded_payload_bytes" \
    '{delivery:$delivery,sha256:$sha256,encodedPayloadBytes:$encodedPayloadBytes}' \
    >"$RUN_DIR/payload-generation.json"

jq -n \
    --arg runId "$RUN_ID" --arg target "$target_url" --arg canonicalTarget "${TARGET_BASE_URL}${TARGET_PATH}" --arg image "$OHA_IMAGE" \
    --arg targetPath "$TARGET_PATH" --arg targetHostHeader "$TARGET_HOST_HEADER" --arg destinationIp "$target_destination_ip" \
    --arg poolSha256 "$EXPECTED_POOL_SHA256" --arg experimentMode "$EXPERIMENT_MODE" --argjson qps "$QUERY_PER_SECOND" \
    --argjson duration "$DURATION_SECONDS" --argjson connections "$CONNECTIONS" \
    --argjson processesPerNode "$PROCESSES_PER_NODE" \
    --argjson timeout "$HTTP_TIMEOUT_SECONDS" --argjson startEpoch "$START_EPOCH" \
    --arg nodeId "$NODE_ID" --arg stageLabel "$STAGE_LABEL" --arg repetition "$REPETITION" --arg instrumentation "$INSTRUMENTATION" --argjson warmupSeconds "$PERSISTENT_WARMUP_SECONDS" --argjson measurementSeconds "$MEASUREMENT_SECONDS" \
    '{runId:$runId,nodeId:$nodeId,experimentMode:$experimentMode,stageLabel:$stageLabel,repetition:$repetition,instrumentation:$instrumentation,target:$target,canonicalTarget:$canonicalTarget,targetPath:$targetPath,targetHostHeader:(if $targetHostHeader=="" then null else $targetHostHeader end),destinationIp:(if $destinationIp=="" then null else $destinationIp end),queryPerSecond:$qps,durationSeconds:$duration,connections:$connections,processesPerNode:$processesPerNode,httpTimeoutSeconds:$timeout,startEpoch:$startEpoch,warmupSeconds:$warmupSeconds,measurementSeconds:$measurementSeconds,ohaImage:$image,payloadSha256:$poolSha256}' \
    >"$RUN_DIR/host-plan.json"

read -r ephemeral_low ephemeral_high </proc/sys/net/ipv4/ip_local_port_range
read -r allocated_files unused_files maximum_files </proc/sys/fs/file-nr
conntrack_count="$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || printf '0')"
conntrack_max="$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || printf '0')"
jq -n --arg interface "$primary_interface" --argjson ulimitNofile "$(ulimit -n)" \
    --argjson systemFileMax "$(cat /proc/sys/fs/file-max)" --argjson allocatedFiles "$allocated_files" \
    --argjson ephemeralPortLow "$ephemeral_low" --argjson ephemeralPortHigh "$ephemeral_high" \
    --argjson conntrackCount "$conntrack_count" --argjson conntrackMax "$conntrack_max" \
    '{interface:$interface,ulimitNofile:$ulimitNofile,systemFileMax:$systemFileMax,allocatedFiles:$allocatedFiles,
      ephemeralPortRange:{low:$ephemeralPortLow,high:$ephemeralPortHigh},conntrack:{count:$conntrackCount,max:$conntrackMax}}' \
    >"$RUN_DIR/load-generator-bootstrap.json"
capture_allowance_counters "$RUN_DIR/ena-allowance-before.txt"
if [[ "$PATH_EVIDENCE_MODE" == 'true' ]]; then
    capture_ena_diagnostics "$RUN_DIR/ena-diagnostics-before.txt"
    capture_dns_snapshot "$RUN_DIR/dns-before.json"
    capture_instance_identity
fi

per_process_rps="$((QUERY_PER_SECOND / PROCESSES_PER_NODE))"
per_process_connections="$((CONNECTIONS / PROCESSES_PER_NODE))"
per_process_request_count="$((per_process_rps * DURATION_SECONDS))"
oha_limit_args=(-z "${DURATION_SECONDS}s" --wait-ongoing-requests-after-deadline)
if [[ "$PHASE7_MODE" == 'true' ]]; then
    oha_limit_args=(-n "$per_process_request_count")
fi
: >"$RUN_DIR/process-plans.ndjson"
for process_index in $(seq 1 "$PROCESSES_PER_NODE"); do
    process_id="process-$(printf '%02d' "$process_index")"
    process_dir="$RUN_DIR/$process_id"
    mkdir -p "$process_dir"
    jq -n --arg processId "$process_id" --arg image "$OHA_IMAGE" --arg target "$target_url" --arg hostHeader "$TARGET_HOST_HEADER" \
        --arg stageLabel "$STAGE_LABEL" --arg repetition "$REPETITION" --arg targetPath "$TARGET_PATH" --arg instrumentation "$INSTRUMENTATION" \
        --arg pool "$POOL_FILE:/payload-pool.ndjson:ro" --arg caMount "$WORK_ROOT/protocol-ca.pem:/protocol-ca.pem:ro" \
        --arg tlsServerName "$TLS_SERVER_NAME" --arg connectDestination "${connect_destination:-}" \
        --argjson connectionPath "$([[ "$EXPERIMENT_MODE" == 'connection-path-crossover' ]] && printf true || printf false)" \
        --argjson phase7 "$PHASE7_MODE" --argjson duration "$DURATION_SECONDS" \
        --argjson requestedRequests "$per_process_request_count" \
        --argjson rps "$per_process_rps" --argjson connections "$per_process_connections" \
        --argjson timeout "$HTTP_TIMEOUT_SECONDS" \
        --arg httpVersion "$OHA_HTTP_VERSION" --argjson parallel "$OHA_PARALLEL" \
        '{processId:$processId,offeredRps:$rps,connections:$connections,httpVersion:$httpVersion,http2Parallel:$parallel,
          command:{executable:"docker",arguments:(["run","--rm","--network","host","--ulimit","nofile=1048576:1048576","-v",$pool] + (if $connectionPath then ["-v",$caMount] else [] end) + [$image,"--no-tui","--output-format","json"] + (if $phase7 then ["-n",($requestedRequests|tostring)] else ["-z",(($duration|tostring)+"s"),"--wait-ongoing-requests-after-deadline"] end) + ["-q",($rps|tostring),"--latency-correction","-c",($connections|tostring)] + (if $connectionPath then ["--http-version","2","-p","1","--cacert","/protocol-ca.pem","--connect-to",($tlsServerName+":443:"+$connectDestination+":443")] else [] end) + ["-m","POST","-T","application/json","-Z","/payload-pool.ndjson","-t",(($timeout|tostring)+"s"),"-H",("X-Loopad-Stage: "+$stageLabel),"-H",("X-Loopad-Repetition: "+$repetition),"-H",("X-Loopad-Target: "+$targetPath),"-H",("X-Loopad-Instrumentation: "+$instrumentation)] + (if $hostHeader=="" then [] else ["-H",("Host: "+$hostHeader)] end) + [$target])}}' \
        >>"$RUN_DIR/process-plans.ndjson"
done
jq -s 'if length == 1 then .[0].command + {processes:.} else {executable:"multiple-oha-processes",arguments:[],processes:.} end' \
    "$RUN_DIR/process-plans.ndjson" >"$RUN_DIR/oha-command.json"

trap 'handle_worker_signal SIGTERM 143' TERM
trap 'handle_worker_signal SIGINT 130' INT
trap cleanup_worker EXIT

start_packet_capture() {
	local capture_filter="tcp port $target_port and (tcp[tcpflags] & (tcp-syn|tcp-rst|tcp-fin) != 0)"
    jq -n --arg interface "$primary_interface" --arg filter "$capture_filter" \
        '{executable:"tcpdump",arguments:["-i",$interface,"-nn","-s","96","-U","-w","syn-handshake.pcap",$filter],snapLengthBytes:96,packetClasses:["SYN","SYN-ACK","RST","FIN"],httpPayloadIntentionallyExcluded:true}' \
        >"$RUN_DIR/packet-capture-command.json"
    tcpdump -i "$primary_interface" -nn -s 96 -U -w "$RUN_DIR/syn-handshake.pcap" "$capture_filter" \
        >"$RUN_DIR/tcpdump.stdout.log" 2>"$RUN_DIR/tcpdump.stderr.log" &
    capture_pid="$!"
    sleep 1
    kill -0 "$capture_pid" 2>/dev/null || { printf 'tcpdump failed to start\n' >&2; exit 6; }
}

finalize_packet_capture() {
    stop_packet_capture
    local raw_pcap="$RUN_DIR/syn-handshake.pcap"
    local packet_count invalid_count sha256
    sha256="$(sha256sum "$raw_pcap" | awk '{print $1}')"
    tcpdump -nn -tttt -r "$raw_pcap" >"$RUN_DIR/syn-handshake.txt" 2>"$RUN_DIR/tcpdump-read.log"
    tcpdump -nn -tt -q -r "$raw_pcap" 2>/dev/null | awk 'BEGIN{print "timestamp_epoch\tpacket"} {timestamp=$1; $1=""; sub(/^ /, ""); gsub(/\t/, " "); print timestamp "\t" $0}' \
        >"$RUN_DIR/syn-handshake.tsv"
    packet_count="$(tcpdump -nn -r "$raw_pcap" 2>/dev/null | wc -l | tr -d ' ')"
    invalid_count="$(tcpdump -nn -r "$raw_pcap" 'tcp and (tcp[tcpflags] & (tcp-syn|tcp-rst|tcp-fin) = 0)' 2>/dev/null | wc -l | tr -d ' ')"
    [[ "$invalid_count" == '0' ]] || { printf 'capture contained a non-handshake packet\n' >&2; exit 6; }
    jq -n --arg sha256 "$sha256" --argjson packetCount "$packet_count" --argjson invalidPacketCount "$invalid_count" \
        '{rawPcapSha256:$sha256,packetCount:$packetCount,invalidPacketCount:$invalidPacketCount,snapLengthBytes:96,rawPcapCommitted:false,rawPcapDeleted:true,textVerified:true,tsvVerified:true}' \
        >"$RUN_DIR/packet-capture-manifest.json"
    rm -f "$raw_pcap"
    capture_finalized='true'
}

capture_telemetry() {
    printf '%s\n' $'timestamp_utc\tcpu_user\tcpu_nice\tcpu_system\tcpu_idle\tcpu_iowait\tcpu_irq\tcpu_softirq\tcpu_steal\tload1\tmem_available_kib\trx_bytes\ttx_bytes\tsockets_used\ttcp_inuse\ttcp_orphan\ttcp_tw\ttcp_alloc\ttcp_mem\tfile_allocated\tconntrack_count\toha_established\toha_syn_sent\toha_open_fds\toha_cpu_percent\toha_rss_kib\tbw_allowance_exceeded\tpps_allowance_exceeded\tconntrack_allowance_exceeded\ttcp_retrans_segs\ttcp_syn_retrans\ttcp_timeouts\tconntrack_max' >"$RUN_DIR/load-generator-telemetry.tsv"
    printf '%s\n' $'timestamp_utc\tdestination_ip\testablished\tsyn_sent' >"$RUN_DIR/destination-sockets.tsv"
    printf '%s\n' $'timestamp_utc\tcpu_index\tprocessed\tdropped\ttime_squeeze\tcpu_collision\treceived_rps\tflow_limit_count' >"$RUN_DIR/softnet-stat.tsv"
    printf '%s\n' $'timestamp_utc\tcounter\tvalue' >"$RUN_DIR/ena-diagnostics-telemetry.tsv"
	: >"$RUN_DIR/ss-tin.log"
	: >"$RUN_DIR/kernel-network-snapshots.log"
	: >"$RUN_DIR/ethtool-all-counters.log"
    local telemetry_sample=0
    while true; do
        telemetry_sample="$((telemetry_sample + 1))"
        sample_timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        read -r _ user nice system idle iowait irq softirq steal _ </proc/stat
        read -r rx tx < <(awk 'NR>2 {gsub(/:/,"",$1); if($1!="lo"){rx+=$2;tx+=$10}} END{print rx+0,tx+0}' /proc/net/dev)
        read -r inuse orphan tw alloc mem < <(awk '$1=="TCP:" {for(i=2;i<=NF;i+=2)v[$i]=$(i+1); print v["inuse"]+0,v["orphan"]+0,v["tw"]+0,v["alloc"]+0,v["mem"]+0}' /proc/net/sockstat)
        oha_cpu="$(ps -eo pcpu=,comm= | awk '$2=="oha"{sum+=$1} END{print sum+0}')"
        oha_rss="$(ps -eo rss=,comm= | awk '$2=="oha"{sum+=$1} END{print sum+0}')"
        oha_established="$(ss -Htanp state established 2>/dev/null | awk '/oha/{count++} END{print count+0}')"
        oha_syn_sent="$(ss -Htanp state syn-sent 2>/dev/null | awk '/oha/{count++} END{print count+0}')"
        oha_open_fds="$(for pid in $(pgrep -x oha 2>/dev/null || true); do find "/proc/$pid/fd" -mindepth 1 -maxdepth 1 2>/dev/null; done | wc -l)"
        file_allocated="$(awk '{print $1}' /proc/sys/fs/file-nr)"
        conntrack_count="$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || printf '0')"
        conntrack_max_sample="$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || printf '0')"
        nstat_output="$(nstat -asz TcpRetransSegs TcpExtTCPSynRetrans TcpExtTCPTimeouts 2>/dev/null || true)"
        tcp_retrans_segs="$(awk '$1=="TcpRetransSegs"{print $2+0}' <<<"$nstat_output")"
        tcp_syn_retrans="$(awk '$1=="TcpExtTCPSynRetrans"{print $2+0}' <<<"$nstat_output")"
        tcp_timeouts="$(awk '$1=="TcpExtTCPTimeouts"{print $2+0}' <<<"$nstat_output")"
        allowance="$(if [[ -n "$primary_interface" ]] && command -v ethtool >/dev/null; then ethtool -S "$primary_interface" 2>/dev/null; fi)"
        bw_allowance="$(awk '$1=="bw_out_allowance_exceeded:" || $1=="bw_in_allowance_exceeded:" {sum+=$2} END{print sum+0}' <<<"$allowance")"
        pps_allowance="$(awk '$1=="pps_allowance_exceeded:" {print $2+0}' <<<"$allowance")"
        conntrack_allowance="$(awk '$1=="conntrack_allowance_exceeded:" {print $2+0}' <<<"$allowance")"
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$sample_timestamp" "$user" "$nice" "$system" "$idle" "$iowait" "$irq" "$softirq" "$steal" \
            "$(awk '{print $1}' /proc/loadavg)" "$(awk '$1=="MemAvailable:"{print $2}' /proc/meminfo)" "$rx" "$tx" \
            "$(awk '$1=="sockets:"{print $3}' /proc/net/sockstat)" "$inuse" "$orphan" "$tw" "$alloc" "$mem" "$file_allocated" "$conntrack_count" \
            "$oha_established" "$oha_syn_sent" "$oha_open_fds" "$oha_cpu" "$oha_rss" "$bw_allowance" "$pps_allowance" "$conntrack_allowance" \
            "${tcp_retrans_segs:-0}" "${tcp_syn_retrans:-0}" "${tcp_timeouts:-0}" "$conntrack_max_sample" \
            >>"$RUN_DIR/load-generator-telemetry.tsv"
        ss -Htanp 2>/dev/null | awk -v timestamp="$sample_timestamp" '
            /oha/ && ($1=="ESTAB" || $1=="SYN-SENT") {
                destination=$5; sub(/:[0-9]+$/, "", destination)
                key=destination SUBSEP $1; count[key]++
            }
            END {
                for (key in count) {
                    split(key, parts, SUBSEP); destinations[parts[1]]=1
                    if (parts[2]=="ESTAB") established[parts[1]]=count[key]
                    if (parts[2]=="SYN-SENT") synsent[parts[1]]=count[key]
                }
                for (destination in destinations) printf "%s\t%s\t%d\t%d\n", timestamp, destination, established[destination]+0, synsent[destination]+0
            }' >>"$RUN_DIR/destination-sockets.tsv"
        awk -v timestamp="$sample_timestamp" '{
            printf "%s\t%d\t%d\t%d\t%d\t%d\t%d\t%d\n", timestamp, NR-1, strtonum("0x"$1), strtonum("0x"$2), strtonum("0x"$3), strtonum("0x"$8), strtonum("0x"$9), strtonum("0x"$11)
        }' /proc/net/softnet_stat >>"$RUN_DIR/softnet-stat.tsv"
        if [[ -n "$primary_interface" ]] && command -v ethtool >/dev/null; then
            ethtool -S "$primary_interface" 2>/dev/null | awk -v timestamp="$sample_timestamp" 'BEGIN{IGNORECASE=1} /drop|error|allowance|fail|miss|timeout/ {gsub(":", "", $1); printf "%s\t%s\t%s\n", timestamp, $1, $2}' \
                >>"$RUN_DIR/ena-diagnostics-telemetry.tsv"
        fi
		if (( telemetry_sample % 30 == 1 )); then
			{
				printf 'timestamp_utc=%s sample=%s\n' "$sample_timestamp" "$telemetry_sample"
				ss -tin 2>&1 || true
			} >>"$RUN_DIR/ss-tin.log"
		fi
		{
			printf 'timestamp_utc=%s\n' "$sample_timestamp"
			printf '[proc_net_snmp]\n'; cat /proc/net/snmp
			printf '[proc_net_netstat]\n'; cat /proc/net/netstat
			printf '[nstat]\n'; nstat -az 2>&1 || true
			printf '[tc_qdisc]\n'; tc -s qdisc show 2>&1 || true
			printf '[ip_link]\n'; ip -s link 2>&1 || true
			printf '[interrupts]\n'; cat /proc/interrupts
			printf '[softirqs]\n'; cat /proc/softirqs
			printf '[cgroup_cpu_stat]\n'; cat /sys/fs/cgroup/cpu.stat 2>&1 || true
		} >>"$RUN_DIR/kernel-network-snapshots.log"
		if [[ -n "$primary_interface" ]] && command -v ethtool >/dev/null; then
			{
				printf 'timestamp_utc=%s interface=%s\n' "$sample_timestamp" "$primary_interface"
				ethtool -S "$primary_interface" 2>&1 || true
			} >>"$RUN_DIR/ethtool-all-counters.log"
		fi
        sleep "$TELEMETRY_INTERVAL_SECONDS"
    done
}

if [[ "$PATH_EVIDENCE_MODE" == 'true' ]]; then
    start_packet_capture
    capture_telemetry &
    telemetry_pid="$!"
fi
if (( START_EPOCH > 0 )); then
    now="$(date +%s)"
    (( START_EPOCH <= now )) || sleep "$((START_EPOCH - now))"
fi

if [[ "$PATH_EVIDENCE_MODE" != 'true' ]]; then
    capture_telemetry &
    telemetry_pid="$!"
fi
started_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
process_pids=()
process_ids=()
container_names=()
for process_index in $(seq 1 "$PROCESSES_PER_NODE"); do
    process_id="process-$(printf '%02d' "$process_index")"
    process_dir="$RUN_DIR/$process_id"
    container_name="loopad-${RUN_ID}-${NODE_ID}-${process_id}"
    process_delay="$(( PROCESS_START_OFFSET_SECONDS + (process_index - 1) * PROCESS_START_SPACING_SECONDS ))"
    (
    (( process_delay == 0 )) || sleep "$process_delay"
    docker run --rm --name "$container_name" --network host --ulimit nofile=1048576:1048576 \
        -v "$POOL_FILE:/payload-pool.ndjson:ro" "${oha_mount_args[@]}" "$OHA_IMAGE" \
        --no-tui --output-format json "${oha_limit_args[@]}" \
        -q "$per_process_rps" --latency-correction -c "$per_process_connections" "${oha_protocol_args[@]}" -m POST -T application/json \
        -Z /payload-pool.ndjson -t "${HTTP_TIMEOUT_SECONDS}s" "${oha_header_args[@]}" "$target_url" \
        >"$process_dir/oha-report.json" 2>"$process_dir/oha.log"
    ) &
    process_pids+=("$!")
    process_ids+=("$process_id")
    container_names+=("$container_name")
done

max_process_start_delay="$(( PROCESS_START_OFFSET_SECONDS + (PROCESSES_PER_NODE - 1) * PROCESS_START_SPACING_SECONDS ))"
container_start_deadline="$(( $(date +%s) + max_process_start_delay + 60 ))"
for container_name in "${container_names[@]}"; do
    while [[ "$(docker inspect --format '{{.State.Running}}' "$container_name" 2>/dev/null || true)" != 'true' ]]; do
        if (( $(date +%s) >= container_start_deadline )); then
            printf 'container did not reach running state: %s\n' "$container_name" >&2
            for owned_container in "${container_names[@]}"; do docker kill "$owned_container" >/dev/null 2>&1 || true; done
            exit 5
        fi
        sleep 1
    done
done

capture_process_identities() {
    local label="$1"
    : >"$RUN_DIR/process-identities-$label.ndjson"
    for process_offset in "${!process_ids[@]}"; do
        process_id="${process_ids[$process_offset]}"
        container_name="${container_names[$process_offset]}"
        docker inspect --format '{{.Id}} {{.State.Pid}} {{.State.Running}} {{.State.StartedAt}}' "$container_name" | \
            jq -R --arg processId "$process_id" --arg containerName "$container_name" --argjson runnerPid "${process_pids[$process_offset]}" \
                --arg capturedAt "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
                'split(" ") | {processId:$processId,containerName:$containerName,runnerPid:$runnerPid,containerId:.[0],hostOhaPid:(.[1]|tonumber),running:(.[2]=="true"),containerStartedAt:.[3],capturedAt:$capturedAt}' \
                >>"$RUN_DIR/process-identities-$label.ndjson"
    done
    jq -s --arg nodeId "$NODE_ID" --arg label "$label" '{nodeId:$nodeId,label:$label,processes:.}' \
        "$RUN_DIR/process-identities-$label.ndjson" >"$RUN_DIR/process-identities-$label.json"
}

gate_decision='legacy-duration'
measurement_started_at=''
measurement_ended_at=''
if [[ "$PATH_EVIDENCE_MODE" == 'true' ]]; then capture_process_identities start; fi
if (( PERSISTENT_WARMUP_SECONDS > 0 )); then
    capture_process_identities start
    sleep "$PERSISTENT_WARMUP_SECONDS"
    capture_process_identities ready
    jq -n --arg nodeId "$NODE_ID" --arg runId "$RUN_ID" --arg readyAt "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        --argjson warmupSeconds "$PERSISTENT_WARMUP_SECONDS" --argjson configuredConnections "$CONNECTIONS" \
        '{nodeId:$nodeId,runId:$runId,readyAt:$readyAt,warmupSeconds:$warmupSeconds,configuredConnections:$configuredConnections}' \
        >"$RUN_DIR/warmup-ready.json"
    control_deadline="$(( $(date +%s) + CONTROL_WAIT_SECONDS ))"
    while (( $(date +%s) < control_deadline )); do
        if [[ -s "$RUN_DIR/control-go-epoch.txt" ]]; then gate_decision='go'; break; fi
        if [[ -e "$RUN_DIR/control-stop" ]]; then gate_decision='stop'; break; fi
        sleep 1
    done
    [[ "$gate_decision" != 'legacy-duration' ]] || gate_decision='control-timeout'
    if [[ "$gate_decision" == 'go' ]]; then
        measurement_start_epoch="$(cat "$RUN_DIR/control-go-epoch.txt")"
        [[ "$measurement_start_epoch" =~ ^[0-9]+$ ]] || { printf 'invalid measurement start epoch\n' >&2; gate_decision='invalid-go'; }
        now="$(date +%s)"
        (( measurement_start_epoch <= now )) || sleep "$((measurement_start_epoch - now))"
        measurement_started_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        sleep "$MEASUREMENT_SECONDS"
        measurement_ended_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    fi
    capture_process_identities final
    for container_name in "${container_names[@]}"; do
        docker kill --signal INT "$container_name" >/dev/null 2>&1 || true
    done
    jq -n --arg decision "$gate_decision" --arg startedAt "$measurement_started_at" --arg endedAt "$measurement_ended_at" \
        --argjson seconds "$MEASUREMENT_SECONDS" '{decision:$decision,startedAt:(if $startedAt=="" then null else $startedAt end),endedAt:(if $endedAt=="" then null else $endedAt end),seconds:$seconds}' \
        >"$RUN_DIR/measurement-window.json"
fi
oha_rc=0
for process_offset in "${!process_pids[@]}"; do
    set +e
    wait "${process_pids[$process_offset]}"
    process_rc="$?"
    set -e
    process_pids[$process_offset]=''
    printf '%s\n' "$process_rc" >"$RUN_DIR/${process_ids[$process_offset]}/exit-code.txt"
    (( process_rc == 0 )) || oha_rc="$process_rc"
done
if [[ "$PATH_EVIDENCE_MODE" == 'true' ]]; then finalize_packet_capture; fi
if (( PERSISTENT_WARMUP_SECONDS > 0 )) && [[ "$gate_decision" == 'go' ]]; then
    oha_rc=0
fi
ended_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
stop_telemetry
telemetry_pid=''
capture_allowance_counters "$RUN_DIR/ena-allowance-after.txt"
if [[ "$PATH_EVIDENCE_MODE" == 'true' ]]; then
    capture_ena_diagnostics "$RUN_DIR/ena-diagnostics-after.txt"
    capture_dns_snapshot "$RUN_DIR/dns-after.json"
fi

for process_id in "${process_ids[@]}"; do
    jq empty "$RUN_DIR/$process_id/oha-report.json"
done
max_tcp_inuse="$(awk -F '\t' 'NR>1 && $15+0>max{max=$15+0} END{print max+0}' "$RUN_DIR/load-generator-telemetry.tsv")"
max_oha_established="$(awk -F '\t' 'NR>1 && $22+0>max{max=$22+0} END{print max+0}' "$RUN_DIR/load-generator-telemetry.tsv")"
max_oha_cpu="$(awk -F '\t' 'NR>1 && $25+0>max{max=$25+0} END{print max+0}' "$RUN_DIR/load-generator-telemetry.tsv")"
max_oha_rss="$(awk -F '\t' 'NR>1 && $26+0>max{max=$26+0} END{print max+0}' "$RUN_DIR/load-generator-telemetry.tsv")"
for process_index in $(seq 1 "$PROCESSES_PER_NODE"); do
    process_id="process-$(printf '%02d' "$process_index")"
    process_dir="$RUN_DIR/$process_id"
    process_rc="$(cat "$process_dir/exit-code.txt")"
    jq --arg processId "$process_id" --arg startedAt "$started_at" --arg endedAt "$ended_at" \
        --argjson exitCode "$process_rc" --argjson configuredConnections "$per_process_connections" \
        --argjson offeredRps "$per_process_rps" '
    ([.statusCodeDistribution[]?]|add//0) as $completed
    | ([.errorDistribution[]?]|add//0) as $errors
    | {
        processId:$processId, startedAt:$startedAt, endedAt:$endedAt, exitCode:$exitCode,
        offeredRps:$offeredRps,
        completedRequests:$completed, transportErrors:$errors, attemptedRequests:($completed+$errors),
        http202:(.statusCodeDistribution["202"]//0), http400:(.statusCodeDistribution["400"]//0),
        http413:(.statusCodeDistribution["413"]//0), http429:(.statusCodeDistribution["429"]//0),
        http5xx:([.statusCodeDistribution|to_entries[]?|select(.key|startswith("5"))|.value]|add//0),
        actualRps:.summary.requestsPerSec, configuredConnections:$configuredConnections,
        firstByteMs:{average:(.details.firstByte.average*1000),p50:(.firstBytePercentiles.p50*1000),p95:(.firstBytePercentiles.p95*1000),p99:(.firstBytePercentiles.p99*1000)},
        latencyCorrectedMs:{p50:(.latencyPercentiles.p50*1000),p95:(.latencyPercentiles.p95*1000),p99:(.latencyPercentiles.p99*1000)},
        latencyMs:{p50:(.latencyPercentiles.p50*1000),p95:(.latencyPercentiles.p95*1000),p99:(.latencyPercentiles.p99*1000)},
        statusCodeDistribution, errorDistribution
      }' "$process_dir/oha-report.json" >"$process_dir/oha-summary.json"
done

jq -s --arg startedAt "$started_at" --arg endedAt "$ended_at" --argjson exitCode "$oha_rc" \
    --argjson configuredConnections "$CONNECTIONS" --argjson processesPerNode "$PROCESSES_PER_NODE" \
    --argjson startEpoch "$START_EPOCH" --argjson maxTcpInuse "$max_tcp_inuse" \
    --argjson maxOhaEstablished "$max_oha_established" --argjson maxOhaCpuPercent "$max_oha_cpu" \
    --argjson maxOhaRssKib "$max_oha_rss" '
    def merge_distribution($field): reduce .[] as $item ({};
        reduce ($item[$field] | to_entries[]) as $entry (.;
            .[$entry.key] = ((.[$entry.key] // 0) + $entry.value)));
    (map(.completedRequests) | add // 0) as $completed
    | (map(.attemptedRequests) | add // 0) as $attempted
    | {
        startedAt:$startedAt,endedAt:$endedAt,exitCode:$exitCode,startEpoch:$startEpoch,
        processesPerNode:$processesPerNode,configuredConnections:$configuredConnections,
        offeredRps:(map(.offeredRps)|add//0),actualRps:(map(.actualRps)|add//0),
        completedRequests:$completed,attemptedRequests:$attempted,
        transportErrors:(map(.transportErrors)|add//0),http202:(map(.http202)|add//0),
        http400:(map(.http400)|add//0),http413:(map(.http413)|add//0),
        http429:(map(.http429)|add//0),http5xx:(map(.http5xx)|add//0),
        connectionObservation:{maxTcpInuse:$maxTcpInuse,maxOhaEstablished:$maxOhaEstablished},
        processObservation:{maxOhaCpuPercent:$maxOhaCpuPercent,maxOhaRssKib:$maxOhaRssKib},
        firstByteMs:{
            average:(if $completed == 0 then null else (map(.firstByteMs.average*.completedRequests)|add)/$completed end),
            p50:(map(.firstByteMs.p50)|max),p95:(map(.firstByteMs.p95)|max),p99:(map(.firstByteMs.p99)|max)
        },
        latencyCorrectedMs:{p50:(map(.latencyCorrectedMs.p50)|max),p95:(map(.latencyCorrectedMs.p95)|max),p99:(map(.latencyCorrectedMs.p99)|max)},
        latencyMs:{p50:(map(.latencyCorrectedMs.p50)|max),p95:(map(.latencyCorrectedMs.p95)|max),p99:(map(.latencyCorrectedMs.p99)|max)},
        statusCodeDistribution:merge_distribution("statusCodeDistribution"),
        errorDistribution:merge_distribution("errorDistribution"),
        processes:.
    }' "$RUN_DIR"/process-*/oha-summary.json >"$RUN_DIR/oha-summary.json"

if (( PROCESSES_PER_NODE == 1 )); then
    cp "$RUN_DIR/process-01/oha-report.json" "$RUN_DIR/oha-report.json"
else
    jq -s '{processReports:.}' "$RUN_DIR"/process-*/oha-report.json >"$RUN_DIR/oha-report.json"
fi
: >"$RUN_DIR/oha.log"
for process_id in "${process_ids[@]}"; do
    sed "s/^/[$process_id] /" "$RUN_DIR/$process_id/oha.log" >>"$RUN_DIR/oha.log"
done

if (( PERSISTENT_WARMUP_SECONDS > 0 )); then
    jq --slurpfile measurement "$RUN_DIR/measurement-window.json" \
        --slurpfile startIdentity "$RUN_DIR/process-identities-start.json" \
        --slurpfile readyIdentity "$RUN_DIR/process-identities-ready.json" \
        --slurpfile finalIdentity "$RUN_DIR/process-identities-final.json" \
        '. + {persistent:{measurement:$measurement[0],identity:{start:$startIdentity[0],ready:$readyIdentity[0],final:$finalIdentity[0]}}}' \
        "$RUN_DIR/oha-summary.json" >"$RUN_DIR/oha-summary.tmp"
    mv "$RUN_DIR/oha-summary.tmp" "$RUN_DIR/oha-summary.json"
fi

gzip -9 "$RUN_DIR/oha.log"
for process_id in "${process_ids[@]}"; do
    gzip -9 "$RUN_DIR/$process_id/oha.log"
done

artifact_files=(
    host-plan.json oha-command.json payload-generation.json payload-sha256.txt load-generator-bootstrap.json
    ena-allowance-before.txt ena-allowance-after.txt load-generator-telemetry.tsv oha-report.json oha-summary.json oha.log.gz
    process-plans.ndjson "${process_ids[@]}"
)
if [[ "$EXPERIMENT_MODE" == 'connection-path-crossover' ]]; then
    artifact_files+=(protocol-correctness.json)
fi
if (( PERSISTENT_WARMUP_SECONDS > 0 )); then
    artifact_files+=(warmup-ready.json measurement-window.json process-identities-start.json process-identities-ready.json process-identities-final.json)
elif [[ "$PATH_EVIDENCE_MODE" == 'true' ]]; then
    artifact_files+=(process-identities-start.json)
fi
if [[ "$PATH_EVIDENCE_MODE" == 'true' ]]; then
    gzip -9 "$RUN_DIR/ss-tin.log" "$RUN_DIR/kernel-network-snapshots.log" "$RUN_DIR/ethtool-all-counters.log" \
        "$RUN_DIR/tcpdump.stdout.log" "$RUN_DIR/tcpdump.stderr.log" "$RUN_DIR/tcpdump-read.log"
    artifact_files+=(
        instance-network-identity.json dns-before.json dns-after.json destination-sockets.tsv softnet-stat.tsv
        ena-diagnostics-before.txt ena-diagnostics-after.txt ena-diagnostics-telemetry.tsv
		ss-tin.log.gz kernel-network-snapshots.log.gz ethtool-all-counters.log.gz
        packet-capture-command.json packet-capture-manifest.json syn-handshake.txt syn-handshake.tsv
        tcpdump.stdout.log.gz tcpdump.stderr.log.gz tcpdump-read.log.gz
    )
fi
tar -C "$RUN_DIR" -czf "$RUN_DIR/artifacts.tgz" "${artifact_files[@]}"
printf 'OHA_SUMMARY=%s\n' "$(jq -c . "$RUN_DIR/oha-summary.json")"
if [[ "$PATH_EVIDENCE_MODE" == 'true' ]]; then
    artifact_s3_uri="s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX%/}/${NODE_ID}/artifacts.tgz"
    aws s3 cp "$RUN_DIR/artifacts.tgz" "$artifact_s3_uri" --only-show-errors
    printf 'ARTIFACT_S3_URI=%s\n' "$artifact_s3_uri"
else
    artifact_base64="$(base64 -w0 "$RUN_DIR/artifacts.tgz")"
    (( ${#artifact_base64} < 22000 )) || { printf 'compressed artifact exceeds SSM output limit\n' >&2; exit 3; }
    printf 'ARTIFACT_TGZ_BASE64=%s\n' "$artifact_base64"
fi
exit "$oha_rc"

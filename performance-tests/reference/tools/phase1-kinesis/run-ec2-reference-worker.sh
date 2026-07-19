#!/bin/bash
set -euo pipefail

: "${RUN_ID:?}"
: "${QUERY_PER_SECOND:?}"
: "${DURATION_SECONDS:?}"
: "${WARMUP_SECONDS:=0}"
: "${CONNECTIONS:?}"
: "${PROCESSES_PER_NODE:?}"
: "${START_EPOCH:?}"
: "${NODE_ID:?}"
: "${TARGET_CONNECT_BASE_URL:?}"
: "${TARGET_HOST_HEADER:?}"
: "${TARGET_PATH:?}"
: "${STAGE_LABEL:?}"
: "${REPETITION:?}"
: "${PAYLOAD_GZIP_BASE64:?}"
: "${EXPECTED_POOL_SHA256:?}"
: "${REFERENCE_BINARY_S3_URI:?}"
: "${REFERENCE_BINARY_SHA256:?}"
: "${EVIDENCE_BUCKET:?}"
: "${EVIDENCE_PREFIX:?}"
: "${RANDOM_SEED:?}"
: "${REFERENCE_PROTOCOL:?}"
: "${STREAMS_PER_CONNECTION:?}"
: "${CA_CERTIFICATE_BASE64:?}"
: "${CA_CERTIFICATE_SHA256:?}"
: "${TLS_SERVER_NAME:?}"

[[ "$RUN_ID" =~ ^run_[0-9]{8}_[0-9]{6}_[a-z0-9][a-z0-9_-]{0,31}$ ]]
[[ "$NODE_ID" =~ ^node-[0-9]{2}$ ]]
[[ "$REFERENCE_BINARY_SHA256" =~ ^[0-9a-f]{64}$ ]]
(( QUERY_PER_SECOND > 0 && DURATION_SECONDS > 0 && WARMUP_SECONDS >= 0 && CONNECTIONS > 0 && PROCESSES_PER_NODE == 2 ))
(( QUERY_PER_SECOND % PROCESSES_PER_NODE == 0 && CONNECTIONS % PROCESSES_PER_NODE == 0 ))
[[ "$REFERENCE_PROTOCOL" == h1 || "$REFERENCE_PROTOCOL" == h2 || "$REFERENCE_PROTOCOL" == h3 ]]
(( STREAMS_PER_CONNECTION == 1 ))
[[ "$TARGET_CONNECT_BASE_URL" == https://* || "$REFERENCE_PROTOCOL" == h1 ]]
[[ "$CA_CERTIFICATE_SHA256" =~ ^[0-9a-f]{64}$ ]]

ROOT="/tmp/loopad-reference-$RUN_ID-$STAGE_LABEL-$REPETITION-$NODE_ID"
rm -rf "$ROOT"
mkdir -p "$ROOT"
printf '%s' "$PAYLOAD_GZIP_BASE64" | base64 -d | gzip -d >"$ROOT/payloads.ndjson"
test "$(sha256sum "$ROOT/payloads.ndjson" | awk '{print $1}')" = "$EXPECTED_POOL_SHA256"
aws s3 cp "$REFERENCE_BINARY_S3_URI" "$ROOT/reference-driver" --region ap-northeast-2 --only-show-errors
test "$(sha256sum "$ROOT/reference-driver" | awk '{print $1}')" = "$REFERENCE_BINARY_SHA256"
chmod 0755 "$ROOT/reference-driver"
printf '%s' "$CA_CERTIFICATE_BASE64" | base64 -d >"$ROOT/protocol-ca.pem"
test "$(sha256sum "$ROOT/protocol-ca.pem" | awk '{print $1}')" = "$CA_CERTIFICATE_SHA256"

interface="$(ip route show default | awk 'NR==1 {print $5}')"
target_port=80
[[ "$TARGET_CONNECT_BASE_URL" == https://* ]] && target_port=443
[[ "$TARGET_CONNECT_BASE_URL" == *":8080"* ]] && target_port=8080

snapshot_full() {
  local label="$1"
  {
    printf 'timestamp_utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%S.%NZ')"
    printf '[ss_tin]\n'; ss -tin 2>&1 || true
    printf '[proc_net_snmp]\n'; cat /proc/net/snmp
    printf '[proc_net_netstat]\n'; cat /proc/net/netstat
    printf '[sockstat]\n'; cat /proc/net/sockstat
    printf '[softnet]\n'; cat /proc/net/softnet_stat
    printf '[nstat]\n'; nstat -az 2>&1 || true
    printf '[tc_qdisc]\n'; tc -s qdisc show 2>&1 || true
    printf '[ip_link]\n'; ip -s link 2>&1 || true
    printf '[ethtool]\n'; ethtool -S "$interface" 2>&1 || true
    printf '[interrupts]\n'; cat /proc/interrupts
    printf '[softirqs]\n'; cat /proc/softirqs
    printf '[conntrack]\n'; cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || true; cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || true
  } >"$ROOT/host-$label.log"
}
snapshot_full before

pcap="$ROOT/handshakes.pcap"
cleanup_raw_capture() {
  if [[ -n "${tcpdump_pid:-}" ]]; then sudo -n kill -INT "$tcpdump_pid" 2>/dev/null || true; fi
  rm -f "$pcap"
}
trap cleanup_raw_capture EXIT
header_only_filter="tcp port $target_port and ((tcp[tcpflags] & (tcp-syn|tcp-rst) != 0) or ((tcp[tcpflags] & tcp-fin != 0) and (ip[2:2] - ((ip[0] & 0x0f) << 2) - ((tcp[12] & 0xf0) >> 2) = 0)))"
if [[ "$REFERENCE_PROTOCOL" == h3 ]]; then
  header_only_filter="udp port $target_port"
fi
sudo -n tcpdump -i any -nn -s 96 -w "$pcap" "$header_only_filter" >"$ROOT/tcpdump.stdout" 2>"$ROOT/tcpdump.stderr" &
tcpdump_pid=$!

printf 'timestamp_unix_ns\testablished\tsyn_sent\ttime_wait\tconntrack\topen_fds\tprocs_running\tload1\ttcp_retrans_segs\n' >"$ROOT/host-250ms.tsv"
(
  end_epoch=$(( START_EPOCH + WARMUP_SECONDS + DURATION_SECONDS + 60 ))
  while (( $(date +%s) <= end_epoch )); do
    timestamp="$(date +%s%N)"
    sockets="$(ss -Htan "dport = :$target_port" 2>/dev/null || true)"
    established="$(printf '%s\n' "$sockets" | awk '$1=="ESTAB"{n++} END{print n+0}')"
    syn_sent="$(printf '%s\n' "$sockets" | awk '$1=="SYN-SENT"{n++} END{print n+0}')"
    time_wait="$(printf '%s\n' "$sockets" | awk '$1=="TIME-WAIT"{n++} END{print n+0}')"
    conntrack="$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || printf 0)"
    open_fds=0
    for pid in $(pgrep -f "$ROOT/reference-driver" || true); do
      # The process can exit between pgrep and find.  Under set -euo pipefail
      # that expected /proc race must not terminate the telemetry sampler and
      # make an otherwise valid load invocation fail.
      count="$({ find "/proc/$pid/fd" -mindepth 1 -maxdepth 1 2>/dev/null || true; } | wc -l)"
      open_fds=$(( open_fds + count ))
    done
    read -r load1 _ _ procs _ </proc/loadavg
    procs_running="${procs%/*}"
    retrans="$(awk '/^Tcp:/{if(!header){for(i=1;i<=NF;i++)name[i]=$i;header=1}else{for(i=1;i<=NF;i++)if(name[i]=="RetransSegs")print $i}}' /proc/net/snmp | tail -1)"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$timestamp" "$established" "$syn_sent" "$time_wait" "$conntrack" "$open_fds" "$procs_running" "$load1" "${retrans:-0}" >>"$ROOT/host-250ms.tsv"
    sleep 0.25
  done
) &
telemetry_pid=$!

pids=()
per_process_rps=$(( QUERY_PER_SECOND / PROCESSES_PER_NODE ))
per_process_connections=$(( CONNECTIONS / PROCESSES_PER_NODE ))
for process_number in 1 2; do
  process_label="$(printf 'process-%02d' "$process_number")"
  output="$ROOT/$process_label"
  mkdir -p "$output"
  "$ROOT/reference-driver" \
    --connect-url "$TARGET_CONNECT_BASE_URL" \
    --host-header "$TARGET_HOST_HEADER" \
    --path "$TARGET_PATH" \
    --rps "$per_process_rps" \
    --connections "$per_process_connections" \
    --protocol "$REFERENCE_PROTOCOL" \
    --streams-per-connection "$STREAMS_PER_CONNECTION" \
    --ca-cert "$ROOT/protocol-ca.pem" \
    --tls-server-name "$TLS_SERVER_NAME" \
	    --duration-seconds "$DURATION_SECONDS" \
	    --warmup-seconds "$WARMUP_SECONDS" \
    --timeout-seconds 10 \
    --start-unix "$START_EPOCH" \
    --payload-pool "$ROOT/payloads.ndjson" \
    --seed "$(( RANDOM_SEED + process_number ))" \
    --stage "$STAGE_LABEL" \
    --repetition "$REPETITION" \
    --node "$NODE_ID" \
    --process "$process_label" \
    --output-dir "$output" >"$output/stdout.log" 2>"$output/stderr.log" &
  pids+=("$!")
done

exit_code=0
for pid in "${pids[@]}"; do
  if ! wait "$pid"; then exit_code=1; fi
done
if (( exit_code != 0 )); then
  kill "$telemetry_pid" 2>/dev/null || true
fi
if ! wait "$telemetry_pid"; then
  (( exit_code != 0 )) || exit_code=1
fi
sudo -n kill -INT "$tcpdump_pid" 2>/dev/null || true
wait "$tcpdump_pid" 2>/dev/null || true
snapshot_full after

sha256sum "$pcap" >"$ROOT/handshakes.pcap.sha256"
sudo -n tcpdump -nn -tttt -r "$pcap" >"$ROOT/handshakes.txt" 2>"$ROOT/tcpdump-read.stderr"
if [[ "$REFERENCE_PROTOCOL" == h3 ]]; then
  cp "$ROOT/handshakes.txt" "$ROOT/handshakes-header-only.txt"
else
  grep -Ev 'length [1-9][0-9]*$' "$ROOT/handshakes.txt" >"$ROOT/handshakes-header-only.txt" || true
  test "$(wc -l <"$ROOT/handshakes.txt")" = "$(wc -l <"$ROOT/handshakes-header-only.txt")"
fi
rm -f "$pcap"

jq -s '{
  streams_per_connection:.[0].streams_per_connection,
	collector_instrumentation_modes:(map(.collector_instrumentation)|unique),
	  persistent_warmup_seconds:.[0].persistent_warmup_seconds,
	  connection_reuse_across_measurement_reset:all(.[];if .persistent_warmup_seconds > 0 then .connection_reuse_across_measurement_reset else true end),
	  warmup:{
	    expectedRequests:(map(.warmup_summary.expected_requests // 0)|add),
	    completedRequests:(map(.warmup_summary.completed_requests // 0)|add),
	    http202:(map(.warmup_summary.statuses["202"] // 0)|add),
	    errors:(map(.warmup_summary.errors // 0)|add),
	    traceDropped:(map(.warmup_summary.trace_dropped // 0)|add),
	    hiddenRetries:(map(.warmup_summary.hidden_retries // 0)|add),
	    protocolErrors:(map(.warmup_summary.protocol_errors // 0)|add),
	    collectorInstrumentationModes:(map(.warmup_summary.collector_instrumentation // "on")|unique),
	    valid:all(.[];if .persistent_warmup_seconds > 0 then (.warmup_valid and (.warmup_summary.valid // false)) else true end)
	  },
  scheduled_requests:(map(.scheduled_requests)|add),
  dispatched_requests:(map(.dispatched_requests)|add),
  completed_requests:(map(.completed_requests)|add),
  expected_requests:(map(.expected_requests)|add),
  achieved_rps:(map(.achieved_rps)|add),
  startedAt:(map(.started_at)|min),
	measurementStartedAt:(map(.measurement_started_at)|min),
	measurementStartEpochs:(map(.measurement_started_at)|unique),
	measurement_start_synchronized:((map(.measurement_started_at)|unique|length) == 1),
  endedAt:(map(.ended_at)|max),
  connection_establishment_count:(map(.connection_establishment_count)|add),
  configured_connections:(map(.connections)|add),
  observed_physical_connections:(map(.observed_physical_connections)|add),
  logical_devices:(map(.logical_devices)|add),
  target_physical_connections:(map(.target_physical_connections)|add),
  cross_device_reuse_violations:(map(.cross_device_reuse_violations)|add),
  max_concurrent_streams_per_connection:(map(.max_concurrent_streams_per_connection)|max),
  protocol_fallbacks:(map(.protocol_fallbacks)|add),
  tls_verification:"required",
  protocol_errors:(map(.protocol_errors)|add),
  schedule_accuracy:((map(.scheduled_requests)|add)/(map(.expected_requests)|add)),
  dispatch_accuracy:((map(.dispatched_requests)|add)/(map(.expected_requests)|add)),
  connection_establishment_accuracy:((map(.connection_establishment_count)|add)/(map(.connections)|add)),
  errors:(map(.errors)|add),
  transportErrors:(map(.errors)|add),
  attemptedRequests:(map(.expected_requests)|add),
  completedRequests:(map(.completed_requests)|add),
  http202:(map(.statuses["202"] // 0)|add),
  http429:(map(.statuses["429"] // 0)|add),
  http5xx:(map([.statuses | to_entries[]? | select((.key|tonumber) >= 500 and (.key|tonumber) < 600) | .value] | add // 0)|add),
  latencyMs:{
	  p50:((map(.total_ns.p50)|max)/1000000),
	  p95:((map(.total_ns.p95)|max)/1000000),
	  p99:((map(.total_ns.p99)|max)/1000000)
  },
	  serviceLatencyMs:{
	    p50:((map(.service_latency_ns.p50)|max)/1000000),
	    p95:((map(.service_latency_ns.p95)|max)/1000000),
	    p99:((map(.service_latency_ns.p99)|max)/1000000)
	  },
	  scheduledLatencyMs:{
	    p50:((map(.scheduled_latency_ns.p50)|max)/1000000),
	    p95:((map(.scheduled_latency_ns.p95)|max)/1000000),
	    p99:((map(.scheduled_latency_ns.p99)|max)/1000000)
	  },
	  worker_service_latency:(map({node,process,p50Ms:(.service_latency_ns.p50/1000000),p95Ms:(.service_latency_ns.p95/1000000),p99Ms:(.service_latency_ns.p99/1000000)})),
	  service_latency_histograms:map(.service_latency_histogram),
	  scheduled_latency_histograms:map(.scheduled_latency_histogram),
  firstByteMs:{
	  p95:((map(.ttfb_ns.p95)|max)/1000000),
	  p99:((map(.ttfb_ns.p99)|max)/1000000)
  },
	  trace_dropped:(map((.trace_dropped // 0) + (.warmup_summary.trace_dropped // 0))|add),
  hidden_retries:(map(.hidden_retries)|add),
  silent_drops:(map(.silent_drops)|add),
	aborted:any(.[];.aborted == true),
	valid:(((map(.measurement_started_at)|unique|length) == 1) and ((map(.collector_instrumentation)|unique) == ["on"]) and
	  all(.[];.valid and (.aborted != true) and (if .persistent_warmup_seconds > 0 then (.warmup_valid and (.warmup_summary.valid // false) and
	    .connection_reuse_across_measurement_reset and .warmup_summary.collector_instrumentation == "off") else true end)))
}' "$ROOT"/process-*/reference-summary.json >"$ROOT/reference-summary.json"

jq -e .valid "$ROOT/reference-summary.json" >/dev/null || exit_code=1

gzip -9 "$ROOT/host-before.log" "$ROOT/host-after.log"
find "$ROOT"/process-* -type f \( -name '*.log' -o -name '*.ndjson' \) -exec gzip -9 {} +
tar -C "$ROOT" -czf "$ROOT/artifacts.tgz" \
  reference-summary.json host-250ms.tsv host-before.log.gz host-after.log.gz handshakes.txt handshakes-header-only.txt handshakes.pcap.sha256 tcpdump.stderr tcpdump-read.stderr process-01 process-02
artifact_uri="s3://$EVIDENCE_BUCKET/${EVIDENCE_PREFIX%/}/$NODE_ID/artifacts.tgz"
aws s3 cp "$ROOT/artifacts.tgz" "$artifact_uri" --region ap-northeast-2 --only-show-errors
printf 'ARTIFACT_S3_URI=%s\n' "$artifact_uri"
exit "$exit_code"

package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"encoding/json"
	"encoding/pem"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/quic-go/quic-go/http3"
)

func TestWorkerReusesOneHTTP11Connection(t *testing.T) {
	serverConnection, clientConnection := net.Pipe()
	done := make(chan error, 1)
	go func() {
		reader := bufio.NewReader(serverConnection)
		for range 2 {
			request, err := http.ReadRequest(reader)
			if err != nil {
				done <- err
				return
			}
			_, _ = io.Copy(io.Discard, request.Body)
			_ = request.Body.Close()
			if _, err := io.WriteString(serverConnection, "HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\nConnection: keep-alive\r\n\r\n"); err != nil {
				done <- err
				return
			}
		}
		done <- nil
	}()
	record := &recorder{bins: map[int]*secondBin{}, start: time.Now(), maxTraceBytes: 1 << 20}
	connection := &timedConn{Conn: clientConnection}
	item := &worker{id: 1, config: config{hostHeader: "test", path: "/__collector_control", timeoutSeconds: 1}, recorder: record, connection: connection, reader: bufio.NewReader(connection), newConnection: true}
	for id := uint64(1); id <= 2; id++ {
		now := time.Now()
		trace := item.execute(job{id: id, scheduled: now, dispatched: now, payload: []byte("{}")}, now)
		if trace.Error != "" || trace.Status != http.StatusAccepted {
			t.Fatalf("unexpected trace: %+v", trace)
		}
	}
	_ = clientConnection.Close()
	_ = serverConnection.Close()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestRequestHeaderPreservesHostPathAndPayloadLength(t *testing.T) {
	cfg := config{hostHeader: "internal.example", path: "/__collector_control", stage: "crossover", repetition: "2", node: "node-01", process: "process-02"}
	traceID := "Root=1-5f84c7a1-0123456789abcdef01234567"
	header := requestHeader(cfg, job{id: 42, payload: []byte("{\"event\":\"ok\"}")}, "node-01/process-02/000042", traceID)
	for _, expected := range []string{"POST /__collector_control HTTP/1.1", "Host: internal.example", "Content-Length: 14", "X-Request-Id: ref-node-01-process-02-42", "X-Amzn-Trace-Id: " + traceID, "X-Loopad-Device-Id: node-01/process-02/000042", "Connection: keep-alive"} {
		if !strings.Contains(header, expected) {
			t.Fatalf("missing %q in %q", expected, header)
		}
	}
}

func TestAmznTraceIDIsValidStableAndProcessUnique(t *testing.T) {
	item := job{id: 42, scheduled: time.Unix(1_700_000_000, 0)}
	cfg := config{stage: "a0", repetition: "1", node: "node-01", process: "process-01", seed: 7}
	first := amznTraceID(cfg, item)
	if first != amznTraceID(cfg, item) {
		t.Fatal("trace ID must be deterministic")
	}
	if len(first) != len("Root=1-00000000-000000000000000000000000") || !strings.HasPrefix(first, "Root=1-6553f100-") {
		t.Fatalf("invalid X-Ray root format %q", first)
	}
	cfg.process = "process-02"
	if first == amznTraceID(cfg, item) {
		t.Fatal("different processes must not share trace IDs")
	}
}

func TestRecorderRejectsCrossDeviceConnectionReuseAndConcurrentStreams(t *testing.T) {
	start := time.Now()
	record := newRecorder(start)
	for index, device := range []string{"device-a", "device-b"} {
		record.completed(requestTrace{
			ID: uint64(index + 1), DeviceID: device, ConnectionKey: "physical-1", Protocol: "HTTP/2.0",
			ConnectionAssignedUnixNanoseconds: start.UnixNano(), ResponseCompleteUnixNanoseconds: start.UnixNano(),
		})
	}
	record.observeStreamConcurrency(2)
	if record.crossDeviceReuse != 1 {
		t.Fatalf("expected one cross-device reuse violation, got %d", record.crossDeviceReuse)
	}
	if record.streamHigh.Load() != 2 {
		t.Fatalf("expected stream high-water 2, got %d", record.streamHigh.Load())
	}
}

func TestStatsUsesNearestRankQuantiles(t *testing.T) {
	result := stats([]int64{10, 20, 30, 40, 50, 60, 70, 80, 90, 100})
	if result["p50"] != 50 || result["p95"] != 100 || result["max"] != 100 {
		t.Fatalf("unexpected stats: %#v", result)
	}
}

func TestSlowAndErrorRequestsAreAlwaysSampled(t *testing.T) {
	if !sampled(requestTrace{TotalNanoseconds: int64(150 * time.Millisecond)}) {
		t.Fatal("slow request must be sampled")
	}
	if !sampled(requestTrace{Error: "timeout"}) {
		t.Fatal("error request must be sampled")
	}
}

func TestWorkerSurfacesResponseTimeoutWithoutRetry(t *testing.T) {
	serverConnection, clientConnection := net.Pipe()
	defer serverConnection.Close()
	defer clientConnection.Close()
	go func() {
		request, err := http.ReadRequest(bufio.NewReader(serverConnection))
		if err == nil {
			_, _ = io.Copy(io.Discard, request.Body)
			_ = request.Body.Close()
		}
	}()
	record := &recorder{bins: map[int]*secondBin{}, start: time.Now(), maxTraceBytes: 1 << 20}
	connection := &timedConn{Conn: clientConnection}
	item := &worker{id: 1, config: config{hostHeader: "test", path: "/__collector_control", timeoutSeconds: 1}, recorder: record, connection: connection, reader: bufio.NewReader(connection), newConnection: true}
	now := time.Now()
	trace := item.execute(job{id: 1, scheduled: now, dispatched: now, payload: []byte("{}")}, now)
	if trace.Error == "" || trace.Status != 0 || item.connection != nil {
		t.Fatalf("timeout must be explicit and close the failed socket: %+v", trace)
	}
	if !strings.Contains(trace.Error, "response read") {
		t.Fatalf("unexpected timeout phase: %s", trace.Error)
	}
}

func TestRecorderAttributesAssignmentAndCompletionToActualTimeBins(t *testing.T) {
	start := time.Unix(1_700_000_000, 0)
	record := &recorder{bins: map[int]*secondBin{}, start: start, maxTraceBytes: 1 << 20}
	record.completed(requestTrace{
		ID: 1, ScheduledUnixNanoseconds: start.UnixNano(), DispatchUnixNanoseconds: start.UnixNano(),
		ConnectionAssignedUnixNanoseconds: start.Add(2 * time.Second).UnixNano(),
		ResponseCompleteUnixNanoseconds:   start.Add(3 * time.Second).UnixNano(),
		Status:                            http.StatusAccepted,
	})
	if record.bins[2].Assigned != 1 || record.bins[2].Completed != 0 || record.bins[3].Completed != 1 {
		t.Fatalf("actual-time attribution failed: %#v", record.bins)
	}
}

func TestHTTPSHTTP1DialPinsALPN(t *testing.T) {
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusAccepted)
	}))
	server.EnableHTTP2 = true
	server.TLS = &tls.Config{NextProtos: []string{"h2", "http/1.1"}}
	server.StartTLS()
	defer server.Close()
	caPath, serverName := writeTestServerCA(t, server)
	connection, err := dial(t.Context(), config{connectURL: server.URL, timeoutSeconds: 2, caCertPath: caPath, tlsServerName: serverName})
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	if connection.protocol != "http/1.1" {
		t.Fatalf("unexpected negotiated protocol %q", connection.protocol)
	}
}

func TestHTTP2RunUsesOnePhysicalConnectionPerDeviceAndOneStream(t *testing.T) {
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.ProtoMajor != 2 {
			t.Errorf("expected HTTP/2, got %s", request.Proto)
		}
		_, _ = io.Copy(io.Discard, request.Body)
		_ = request.Body.Close()
		time.Sleep(20 * time.Millisecond)
		w.WriteHeader(http.StatusAccepted)
	}))
	server.EnableHTTP2 = true
	server.StartTLS()
	defer server.Close()
	caPath, serverName := writeTestServerCA(t, server)
	parsed, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	directory := t.TempDir()
	payload := filepath.Join(directory, "payload.ndjson")
	if err := os.WriteFile(payload, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := config{
		connectURL: server.URL, hostHeader: parsed.Host, path: "/events", payloadPath: payload,
		outputDir: directory, stage: "test", repetition: "1", node: "node-01", process: "process-01",
		protocol: "h2", rps: 20, connections: 1, streamsPerConnection: 1, caCertPath: caPath, tlsServerName: serverName,
		durationSeconds: 1, timeoutSeconds: 2, startUnix: time.Now().Add(1100 * time.Millisecond).Unix(), seed: 1,
	}
	if err := runHTTP2(cfg); err != nil {
		t.Fatal(err)
	}
	content, err := os.ReadFile(filepath.Join(directory, "reference-summary.json"))
	if err != nil {
		t.Fatal(err)
	}
	var summary struct {
		Valid                       bool           `json:"valid"`
		ObservedPhysicalConnections int            `json:"observed_physical_connections"`
		ConcurrentHighWater         int            `json:"concurrent_physical_connections_high_water"`
		ObservedProtocols           map[string]int `json:"observed_protocols"`
		ProtocolErrors              int            `json:"protocol_errors"`
		CrossDeviceReuse            int            `json:"cross_device_reuse_violations"`
		MaxConcurrentStreams        int            `json:"max_concurrent_streams_per_connection"`
	}
	if err := json.Unmarshal(content, &summary); err != nil {
		t.Fatal(err)
	}
	if !summary.Valid || summary.ObservedPhysicalConnections != 1 || summary.ConcurrentHighWater != 1 || summary.ProtocolErrors != 0 || summary.CrossDeviceReuse != 0 || summary.MaxConcurrentStreams != 1 || summary.ObservedProtocols["HTTP/2.0"] != 20 {
		t.Fatalf("unexpected HTTP/2 summary: %+v", summary)
	}
}

func TestHTTP2PersistentWarmupReusesConnectionAndResetsMeasurement(t *testing.T) {
	requestCount := atomic.Int64{}
	warmupInstrumentationCount := atomic.Int64{}
	measurementInstrumentationCount := atomic.Int64{}
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.ProtoMajor != 2 {
			t.Errorf("expected HTTP/2, got %s", request.Proto)
		}
		_, _ = io.Copy(io.Discard, request.Body)
		_ = request.Body.Close()
		requestCount.Add(1)
		if request.Header.Get("X-Loopad-Instrumentation") == "off" {
			warmupInstrumentationCount.Add(1)
		} else if request.Header.Get("X-Loopad-Instrumentation") == "on" {
			measurementInstrumentationCount.Add(1)
		}
		w.WriteHeader(http.StatusAccepted)
	}))
	server.EnableHTTP2 = true
	server.StartTLS()
	defer server.Close()
	caPath, serverName := writeTestServerCA(t, server)
	parsed, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	directory := t.TempDir()
	payload := filepath.Join(directory, "payload.ndjson")
	if err := os.WriteFile(payload, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := config{
		connectURL: server.URL, hostHeader: parsed.Host, path: "/events", payloadPath: payload,
		outputDir: directory, stage: "test", repetition: "1", node: "node-01", process: "process-01",
		protocol: "h2", rps: 10, connections: 1, streamsPerConnection: 1, caCertPath: caPath, tlsServerName: serverName,
		warmupSeconds: 1, durationSeconds: 1, timeoutSeconds: 2, startUnix: time.Now().Add(1100 * time.Millisecond).Unix(), seed: 1,
	}
	if err := runHTTP2(cfg); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"warmup-summary.json", "warmup-per-second.ndjson", "reference-summary.json", "per-second.ndjson"} {
		if _, err := os.Stat(filepath.Join(directory, name)); err != nil {
			t.Fatalf("missing %s: %v", name, err)
		}
	}
	var summary struct {
		Valid                     bool      `json:"valid"`
		MeasurementStartedAt      time.Time `json:"measurement_started_at"`
		CollectorInstrumentation  string    `json:"collector_instrumentation"`
		PersistentWarmup          int       `json:"persistent_warmup_seconds"`
		ReuseAcrossReset          bool      `json:"connection_reuse_across_measurement_reset"`
		WarmupValid               bool      `json:"warmup_valid"`
		WarmupPhysical            int       `json:"warmup_physical_connections"`
		SharedWarmupPhysical      int       `json:"measurement_physical_connections_shared_with_warmup"`
		MeasurementNewConnections int       `json:"measurement_new_connections"`
		NewConnections            int       `json:"new_connections"`
		CompletedRequests         int       `json:"completed_requests"`
		WarmupSummary             struct {
			Valid                    bool           `json:"valid"`
			CollectorInstrumentation string         `json:"collector_instrumentation"`
			CompletedRequests        int            `json:"completed_requests"`
			Statuses                 map[string]int `json:"statuses"`
		} `json:"warmup_summary"`
		ServiceLatencyHistogram struct {
			Count         int `json:"count"`
			OverflowCount int `json:"overflow_count"`
		} `json:"service_latency_histogram"`
	}
	content, err := os.ReadFile(filepath.Join(directory, "reference-summary.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(content, &summary); err != nil {
		t.Fatal(err)
	}
	if !summary.Valid || summary.PersistentWarmup != 1 || !summary.ReuseAcrossReset || !summary.WarmupValid || !summary.WarmupSummary.Valid ||
		!summary.MeasurementStartedAt.Equal(time.Unix(cfg.startUnix+int64(cfg.warmupSeconds)+10, 0).UTC()) ||
		summary.WarmupPhysical != 1 || summary.SharedWarmupPhysical != 1 || summary.MeasurementNewConnections != 0 || summary.NewConnections != 1 ||
		summary.WarmupSummary.CompletedRequests != 10 || summary.WarmupSummary.Statuses["202"] != 10 ||
		summary.CompletedRequests != 10 || summary.ServiceLatencyHistogram.Count != 10 || summary.ServiceLatencyHistogram.OverflowCount != 0 ||
		summary.CollectorInstrumentation != "on" || summary.WarmupSummary.CollectorInstrumentation != "off" || requestCount.Load() != 20 ||
		warmupInstrumentationCount.Load() != 10 || measurementInstrumentationCount.Load() != 10 {
		t.Fatalf("persistent warm-up did not preserve one connection and isolate measurement: summary=%+v requests=%d", summary, requestCount.Load())
	}
}

func TestHTTP3RunUsesOneQUICConnectionPerDeviceAndOneStream(t *testing.T) {
	certificateServer := httptest.NewUnstartedServer(http.NotFoundHandler())
	certificateServer.StartTLS()
	caPath, serverName := writeTestServerCA(t, certificateServer)
	certificate := certificateServer.TLS.Certificates[0]
	certificateServer.Close()

	packetConnection, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	requestCount := atomic.Int64{}
	server := &http3.Server{
		TLSConfig: &tls.Config{Certificates: []tls.Certificate{certificate}},
		Handler: http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
			if request.ProtoMajor != 3 || request.Header.Get("X-Loopad-Device-Id") == "" {
				t.Errorf("invalid HTTP/3 request: proto=%s device=%q", request.Proto, request.Header.Get("X-Loopad-Device-Id"))
			}
			_, _ = io.Copy(io.Discard, request.Body)
			_ = request.Body.Close()
			requestCount.Add(1)
			response.WriteHeader(http.StatusAccepted)
		}),
	}
	serverDone := make(chan error, 1)
	go func() { serverDone <- server.Serve(packetConnection) }()
	t.Cleanup(func() {
		shutdownContext, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownContext)
		_ = packetConnection.Close()
		<-serverDone
	})

	directory := t.TempDir()
	payload := filepath.Join(directory, "payload.ndjson")
	if err := os.WriteFile(payload, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := config{
		connectURL: "https://" + packetConnection.LocalAddr().String(), hostHeader: "collector.test", path: "/events", payloadPath: payload,
		outputDir: directory, stage: "test", repetition: "1", node: "node-01", process: "process-01",
		protocol: "h3", rps: 20, connections: 1, streamsPerConnection: 1, caCertPath: caPath, tlsServerName: serverName,
		durationSeconds: 1, timeoutSeconds: 2, startUnix: time.Now().Add(1100 * time.Millisecond).Unix(), seed: 1,
	}
	if err := runHTTP3(cfg); err != nil {
		t.Fatal(err)
	}
	var summary struct {
		Valid                bool           `json:"valid"`
		ObservedProtocols    map[string]int `json:"observed_protocols"`
		ObservedConnections  int            `json:"observed_physical_connections"`
		MaximumStreams       int            `json:"max_concurrent_streams_per_connection"`
		CrossDeviceViolation int            `json:"cross_device_reuse_violations"`
	}
	content, err := os.ReadFile(filepath.Join(directory, "reference-summary.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(content, &summary); err != nil {
		t.Fatal(err)
	}
	if !summary.Valid || summary.ObservedConnections != 1 || summary.MaximumStreams != 1 || summary.CrossDeviceViolation != 0 || summary.ObservedProtocols["HTTP/3.0"] != 20 || requestCount.Load() != 20 {
		t.Fatalf("unexpected HTTP/3 summary: %+v requests=%d", summary, requestCount.Load())
	}
}

func TestCorrectnessTraceOverrideSamplesEveryRequest(t *testing.T) {
	if !sampled(requestTrace{ID: 1, ForceTrace: true}) {
		t.Fatal("trace-all correctness request was not sampled")
	}
}

func TestHTTP2QueueHoldsTheWholeOpenLoopSchedule(t *testing.T) {
	cfg := config{rps: 3750, connections: 15, durationSeconds: 90, streamsPerConnection: 1}
	if got, want := http2QueueSize(cfg), 22501; got != want {
		t.Fatalf("queue size = %d, want %d", got, want)
	}
}

func TestFixedLatencyHistogramHasFiniteMergeableBucketsAndExplicitOverflow(t *testing.T) {
	histogram := fixedLatencyHistogram([]int64{0, int64(time.Millisecond), int64(250 * time.Millisecond), int64(12 * time.Second)}, 10)
	counts := histogram["counts"].([]uint64)
	if histogram["count"].(int) != 4 || histogram["overflow_count"].(uint64) != 1 || len(counts) != 11_001 {
		t.Fatalf("unexpected histogram metadata: %#v", histogram)
	}
	if got := histogramQuantileUpper(counts, int64(time.Millisecond), .95); got != int64(11*time.Second) {
		t.Fatalf("overflow bucket must have a finite upper bound, got %s", time.Duration(got))
	}
}

func TestResetForMeasurementClearsRequestMetricsButPreservesConnections(t *testing.T) {
	start := time.Now()
	record := newRecorder(start)
	record.initialConnected.Store(1)
	record.connected.Store(1)
	record.tcpActive.Store(1)
	record.tcpHigh.Store(2)
	record.bins[0] = &secondBin{Completed: 1, Total: []int64{1}}
	record.physical["connection"] = struct{}{}
	record.traceDropped = 3
	record.resetForMeasurement(start.Add(time.Second))
	if len(record.bins) != 0 || len(record.physical) != 0 || record.traceDropped != 0 || record.initialConnected.Load() != 1 || record.connected.Load() != 1 || record.tcpHigh.Load() != 1 {
		t.Fatalf("measurement reset contract failed: %+v", record)
	}
}

func TestLiveProgressUsesCumulativeServiceLatencyAndExactStatusCounters(t *testing.T) {
	start := time.Unix(1_700_000_000, 0)
	record := newRecorder(start)
	record.initialConnected.Store(2)
	record.connected.Store(2)
	record.tcpActive.Store(2)
	for index, item := range []struct {
		at         time.Time
		latency    time.Duration
		status     int
		connection string
	}{
		{start, 900 * time.Millisecond, 202, "first"},
		{start.Add(120 * time.Second), 99 * time.Millisecond, 202, "first"},
		{start.Add(120 * time.Second), 100 * time.Millisecond, 429, "second"},
		{start.Add(120 * time.Second), 100 * time.Millisecond, 500, "second"},
	} {
		record.completed(requestTrace{ID: uint64(index + 1), DeviceID: item.connection, ConnectionKey: item.connection,
			ScheduledUnixNanoseconds: item.at.Add(-item.latency).UnixNano(), ConnectionAssignedUnixNanoseconds: item.at.Add(-item.latency).UnixNano(),
			ResponseCompleteUnixNanoseconds: item.at.UnixNano(), TotalNanoseconds: int64(item.latency), Status: item.status})
	}
	record.bins[120].Scheduled = 3
	record.bins[120].Dispatched = 3
	progress := record.liveProgress(config{rps: 3, connections: 2, timeoutSeconds: 10}, start.Add(130*time.Second))
	histogram := progress["service_histogram"].(map[string]any)
	if histogram["count"] != uint64(4) || histogram["p95_upper_nanoseconds"] != int64(900*time.Millisecond) || histogram["overflow_count"] != uint64(0) ||
		progress["http_202"] != uint64(2) || progress["http_429"] != uint64(1) || progress["http_5xx"] != uint64(1) ||
		progress["observed_physical_connections"] != 2 || progress["current_tcp_connections"] != int64(2) {
		t.Fatalf("unexpected live progress: %#v", progress)
	}
}

func TestHTTP2AbortWritesPartialSummaryAndLiveProgress(t *testing.T) {
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		_, _ = io.Copy(io.Discard, request.Body)
		_ = request.Body.Close()
		w.WriteHeader(http.StatusAccepted)
	}))
	server.EnableHTTP2 = true
	server.StartTLS()
	defer server.Close()
	caPath, serverName := writeTestServerCA(t, server)
	parsed, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	directory := t.TempDir()
	payload := filepath.Join(directory, "payload.ndjson")
	if err := os.WriteFile(payload, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	abortRequested.Store(false)
	channel := make(chan struct{})
	abortSignal = channel
	t.Cleanup(func() {
		abortRequested.Store(false)
		abortSignal = nil
	})
	cfg := config{
		connectURL: server.URL, hostHeader: parsed.Host, path: "/events", payloadPath: payload,
		outputDir: directory, stage: "abort", repetition: "1", node: "node-01", process: "process-01",
		protocol: "h2", rps: 20, connections: 1, streamsPerConnection: 1, caCertPath: caPath, tlsServerName: serverName,
		durationSeconds: 5, timeoutSeconds: 2, startUnix: time.Now().Add(1100 * time.Millisecond).Unix(), seed: 1,
	}
	done := make(chan error, 1)
	go func() { done <- runHTTP2(cfg) }()
	time.Sleep(2200 * time.Millisecond)
	abortRequested.Store(true)
	close(channel)
	if runErr := <-done; runErr == nil || !strings.Contains(runErr.Error(), "aborted during scored measurement") {
		t.Fatalf("expected explicit abort result, got %v", runErr)
	}
	var summary struct {
		Aborted           bool `json:"aborted"`
		Valid             bool `json:"valid"`
		ExpectedRequests  int  `json:"expected_requests"`
		CompletedRequests int  `json:"completed_requests"`
	}
	content, err := os.ReadFile(filepath.Join(directory, "reference-summary.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(content, &summary); err != nil {
		t.Fatal(err)
	}
	if !summary.Aborted || summary.Valid || summary.CompletedRequests >= summary.ExpectedRequests {
		t.Fatalf("abort summary did not preserve partial evidence: %+v", summary)
	}
	if _, err := os.Stat(filepath.Join(directory, "live-progress.json")); err != nil {
		t.Fatalf("live progress was not preserved: %v", err)
	}
}

func TestHTTP2ConnectionGenerationContractAccountsForALBGoAway(t *testing.T) {
	cfg := config{protocol: "h2", rps: 3750, connections: 15, durationSeconds: 90, streamsPerConnection: 1}
	if got, want := http2ExpectedConnectionGenerations(cfg, cfg.rps*cfg.durationSeconds), 3; got != want {
		t.Fatalf("connection generations = %d, want %d", got, want)
	}
	if got := http2ExpectedConnectionGenerations(config{protocol: "h1", connections: 15}, 337_500); got != 1 {
		t.Fatalf("HTTP/1 connection generations = %d, want 1", got)
	}
}

func writeTestServerCA(t *testing.T, server *httptest.Server) (string, string) {
	t.Helper()
	certificate := server.Certificate()
	path := filepath.Join(t.TempDir(), "ca.pem")
	content := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certificate.Raw})
	if err := os.WriteFile(path, content, 0o600); err != nil {
		t.Fatal(err)
	}
	serverName := certificate.Subject.CommonName
	if len(certificate.DNSNames) > 0 {
		serverName = certificate.DNSNames[0]
	}
	return path, serverName
}

type contextRoundTripper struct{ calls int }

func (r *contextRoundTripper) RoundTrip(request *http.Request) (*http.Response, error) {
	r.calls++
	return nil, request.Context().Err()
}

func TestHTTP2TimeoutStartsAtScheduledTime(t *testing.T) {
	roundTripper := &contextRoundTripper{}
	record := newRecorder(time.Now().Add(-3 * time.Second))
	group := &http2Connection{
		id:       1,
		config:   config{connectURL: "https://example.invalid", hostHeader: "example.invalid", path: "/events", timeoutSeconds: 1},
		recorder: record,
		client:   &http.Client{Transport: roundTripper},
	}
	scheduled := time.Now().Add(-2 * time.Second)
	trace := group.execute(job{id: 1, scheduled: scheduled, dispatched: scheduled, payload: []byte("{}")}, time.Now(), 1)
	if roundTripper.calls != 1 || !strings.Contains(trace.Error, "context deadline exceeded") || trace.TotalNanoseconds < int64(time.Second) {
		t.Fatalf("scheduled deadline was not enforced: calls=%d trace=%+v", roundTripper.calls, trace)
	}
}

package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"hash/fnv"
	"io"
	"math"
	"math/rand/v2"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/quic-go/quic-go"
	"github.com/quic-go/quic-go/http3"
)

var abortRequested atomic.Bool
var abortSignal <-chan struct{}

const liveProgressBucketWidthNanoseconds int64 = int64(10 * time.Millisecond)
const liveProgressMaxNanoseconds int64 = int64(11 * time.Second)

type config struct {
	connectURL, hostHeader, path, payloadPath, outputDir             string
	caCertPath, tlsServerName                                        string
	stage, repetition, node, process                                 string
	protocol                                                         string
	traceAll                                                         bool
	rps, connections, warmupSeconds, durationSeconds, timeoutSeconds int
	streamsPerConnection                                             int
	startUnix, seed                                                  int64
	warmupSummary                                                    map[string]any
	warmupPhysical                                                   map[string]struct{}
	warmupConnected                                                  uint64
	aborted                                                          bool
	instrumentation                                                  string
}

type job struct {
	id              uint64
	scheduled       time.Time
	dispatched      time.Time
	payload         []byte
	payloadIndex    int
	done            *sync.WaitGroup
	instrumentation string
}

type requestTrace struct {
	ForceTrace                        bool   `json:"-"`
	ID                                uint64 "json:\"id\""
	TraceID                           string "json:\"x_amzn_trace_id\""
	ConnectionID                      int    "json:\"connection_id\""
	DeviceID                          string "json:\"device_id\""
	PayloadIndex                      int    "json:\"payload_index\""
	ScheduledUnixNanoseconds          int64  "json:\"scheduled_unix_nanoseconds\""
	DispatchUnixNanoseconds           int64  "json:\"dispatch_unix_nanoseconds\""
	ConnectionAssignedUnixNanoseconds int64  "json:\"connection_assigned_unix_nanoseconds\""
	HeaderWriteUnixNanoseconds        int64  "json:\"header_write_unix_nanoseconds\""
	BodyFirstWriteUnixNanoseconds     int64  "json:\"body_first_write_unix_nanoseconds\""
	BodyLastWriteUnixNanoseconds      int64  "json:\"body_last_write_unix_nanoseconds\""
	RequestCompleteUnixNanoseconds    int64  "json:\"request_complete_unix_nanoseconds\""
	FirstResponseByteUnixNanoseconds  int64  "json:\"first_response_byte_unix_nanoseconds\""
	ResponseCompleteUnixNanoseconds   int64  "json:\"response_complete_unix_nanoseconds\""
	DispatchLatenessNanoseconds       int64  "json:\"dispatch_lateness_nanoseconds\""
	AssignmentLatenessNanoseconds     int64  "json:\"assignment_lateness_nanoseconds\""
	WriteNanoseconds                  int64  "json:\"write_nanoseconds\""
	TTFBNanoseconds                   int64  "json:\"ttfb_nanoseconds\""
	TotalNanoseconds                  int64  "json:\"total_nanoseconds\""
	Status                            int    "json:\"status\""
	Error                             string "json:\"error,omitempty\""
	NewConnection                     bool   "json:\"new_connection\""
	Protocol                          string "json:\"protocol\""
	NegotiatedProtocol                string "json:\"negotiated_protocol,omitempty\""
	ConnectionKey                     string "json:\"connection_key,omitempty\""
	StreamSlot                        int    "json:\"stream_slot,omitempty\""
	HiddenRetryCount                  int    "json:\"hidden_retry_count,omitempty\""
	ConnectStartUnixNanoseconds       int64  "json:\"connect_start_unix_nanoseconds,omitempty\""
	ConnectDoneUnixNanoseconds        int64  "json:\"connect_done_unix_nanoseconds,omitempty\""
	TLSStartUnixNanoseconds           int64  "json:\"tls_start_unix_nanoseconds,omitempty\""
	TLSDoneUnixNanoseconds            int64  "json:\"tls_done_unix_nanoseconds,omitempty\""
}

type secondBin struct {
	Scheduled, Dispatched, Assigned, Completed, Catchup, NewConnections, ReusedConnections, ClosedConnections, Errors uint64
	DispatchLateness, AssignmentLateness, Connect, Write, TTFB, Total, ScheduledTotal                                 []int64
	Statuses                                                                                                          map[int]uint64
	ActiveHigh, IdleLow, BusyHigh                                                                                     int64
}

type recorder struct {
	mu                  sync.Mutex
	bins                map[int]*secondBin
	traces              []requestTrace
	traceBytes          uint64
	traceDropped        uint64
	maxTraceBytes       uint64
	start               time.Time
	active              atomic.Int64
	idle                atomic.Int64
	busy                atomic.Int64
	connected           atomic.Uint64
	initialConnected    atomic.Uint64
	closed              atomic.Uint64
	protocols           map[string]uint64
	physical            map[string]struct{}
	protocolErrors      uint64
	connectionOwners    map[string]string
	connectionUses      map[string]uint64
	crossDeviceReuse    uint64
	streamHigh          atomic.Int64
	tcpActive           atomic.Int64
	tcpHigh             atomic.Int64
	hiddenRetries       atomic.Uint64
	liveServiceCounts   []uint64
	liveServiceOverflow uint64
}

type trackedConn struct {
	net.Conn
	recorder *recorder
	closed   atomic.Bool
}

func (c *trackedConn) Close() error {
	if c.closed.CompareAndSwap(false, true) {
		c.recorder.tcpActive.Add(-1)
		c.recorder.closed.Add(1)
	}
	return c.Conn.Close()
}

type timedConn struct {
	net.Conn
	firstRead atomic.Int64
	protocol  string
	key       string
}

func (c *timedConn) Read(buffer []byte) (int, error) {
	c.firstRead.CompareAndSwap(0, time.Now().UnixNano())
	return c.Conn.Read(buffer)
}

type worker struct {
	id            int
	config        config
	recorder      *recorder
	connection    *timedConn
	reader        *bufio.Reader
	newConnection bool
}

func main() {
	cfg := parseFlags()
	abortRequested.Store(false)
	abortChannel := make(chan struct{})
	abortSignal = abortChannel
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(signals)
	go func() {
		<-signals
		abortRequested.Store(true)
		close(abortChannel)
	}()
	if err := run(cfg); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
}

func run(cfg config) error {
	if cfg.warmupSeconds > 0 && cfg.protocol != "h2" {
		return fmt.Errorf("persistent warm-up is currently supported only for HTTP/2")
	}
	if cfg.protocol == "h3" {
		return runHTTP3(cfg)
	}
	if cfg.protocol == "h2" {
		return runHTTP2(cfg)
	}
	return runHTTP1(cfg)
}

func runHTTP1(cfg config) error {
	payloads, payloadSHA, err := readPayloads(cfg.payloadPath)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(cfg.outputDir, 0o700); err != nil {
		return err
	}
	start := time.Unix(cfg.startUnix, 0)
	record := newRecorder(start)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if delay := time.Until(start); delay > 0 {
		time.Sleep(delay)
	}
	if time.Now().After(start.Add(time.Second)) {
		return fmt.Errorf("stage start missed by more than one second")
	}

	// Start connection establishment at the same epoch as the open-loop request
	// schedule. Preconnecting would turn a driver crossover into a ramp/prewarm
	// crossover and make its causal interpretation invalid.
	jobs := make(chan job, max(cfg.connections*4, cfg.rps*5))
	var workerGroup sync.WaitGroup
	connectionResult := make(chan error, 1)
	go func() { connectionResult <- connectWorkers(ctx, cfg, record, jobs, &workerGroup) }()
	total := cfg.rps * cfg.durationSeconds
	connectionComplete := false
	rng := rand.New(rand.NewPCG(uint64(cfg.seed), uint64(cfg.seed)^0x9e3779b97f4a7c15))
	period := time.Second / time.Duration(cfg.rps)
	for index := 0; index < total; index++ {
		scheduled := start.Add(time.Duration(index) * time.Second / time.Duration(cfg.rps))
		if wait := time.Until(scheduled); wait > 0 {
			time.Sleep(wait)
		}
		record.scheduled(scheduled)
		payloadIndex := rng.IntN(len(payloads))
		item := job{id: uint64(index + 1), scheduled: scheduled, payload: payloads[payloadIndex], payloadIndex: payloadIndex}
		item.dispatched = time.Now()
		if connectionComplete {
			jobs <- item
		} else {
			select {
			case jobs <- item:
			case connectionErr := <-connectionResult:
				connectionComplete = true
				if connectionErr != nil {
					close(jobs)
					workerGroup.Wait()
					return connectionErr
				}
				jobs <- item
			}
		}
		record.dispatched(item, period)
	}
	close(jobs)
	var connectionErr error
	if !connectionComplete {
		connectionErr = <-connectionResult
	}
	workerGroup.Wait()
	if connectionErr != nil {
		return connectionErr
	}
	return writeOutput(cfg, payloadSHA, total, record)
}

func newRecorder(start time.Time) *recorder {
	return &recorder{
		bins: map[int]*secondBin{}, maxTraceBytes: 2 * 1024 * 1024 * 1024, start: start,
		protocols: map[string]uint64{}, physical: map[string]struct{}{}, connectionOwners: map[string]string{}, connectionUses: map[string]uint64{},
		liveServiceCounts: make([]uint64, liveProgressMaxNanoseconds/liveProgressBucketWidthNanoseconds+1),
	}
}

func (r *recorder) resetForMeasurement(start time.Time) {
	_ = r.detachForMeasurement(start)
}

func (r *recorder) detachForMeasurement(start time.Time) *recorder {
	r.mu.Lock()
	defer r.mu.Unlock()
	snapshot := &recorder{
		bins: r.bins, traces: r.traces, traceBytes: r.traceBytes, traceDropped: r.traceDropped, maxTraceBytes: r.maxTraceBytes, start: r.start,
		protocols: r.protocols, physical: r.physical, protocolErrors: r.protocolErrors, connectionOwners: r.connectionOwners,
		connectionUses: r.connectionUses, crossDeviceReuse: r.crossDeviceReuse, liveServiceCounts: r.liveServiceCounts,
		liveServiceOverflow: r.liveServiceOverflow,
	}
	snapshot.active.Store(r.active.Load())
	snapshot.idle.Store(r.idle.Load())
	snapshot.busy.Store(r.busy.Load())
	snapshot.connected.Store(r.connected.Load())
	snapshot.initialConnected.Store(r.initialConnected.Load())
	snapshot.closed.Store(r.closed.Load())
	snapshot.streamHigh.Store(r.streamHigh.Load())
	snapshot.tcpActive.Store(r.tcpActive.Load())
	snapshot.tcpHigh.Store(r.tcpHigh.Load())
	snapshot.hiddenRetries.Store(r.hiddenRetries.Load())
	r.bins = map[int]*secondBin{}
	r.traces = nil
	r.traceBytes = 0
	r.traceDropped = 0
	r.start = start
	r.protocols = map[string]uint64{}
	r.physical = map[string]struct{}{}
	r.protocolErrors = 0
	r.connectionOwners = map[string]string{}
	r.connectionUses = map[string]uint64{}
	r.crossDeviceReuse = 0
	r.streamHigh.Store(0)
	r.hiddenRetries.Store(0)
	r.tcpHigh.Store(r.tcpActive.Load())
	r.liveServiceCounts = make([]uint64, liveProgressMaxNanoseconds/liveProgressBucketWidthNanoseconds+1)
	r.liveServiceOverflow = 0
	return snapshot
}

func (r *recorder) physicalSnapshot() map[string]struct{} {
	r.mu.Lock()
	defer r.mu.Unlock()
	result := make(map[string]struct{}, len(r.physical))
	for key := range r.physical {
		result[key] = struct{}{}
	}
	return result
}

type http2Connection struct {
	id        int
	config    config
	recorder  *recorder
	client    *http.Client
	transport *http.Transport
	jobs      chan job
	seen      atomic.Bool
	current   atomic.Int64
	wg        sync.WaitGroup
}

func runHTTP2(cfg config) error {
	payloads, payloadSHA, err := readPayloads(cfg.payloadPath)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(cfg.outputDir, 0o700); err != nil {
		return err
	}
	target, err := url.Parse(cfg.connectURL)
	if err != nil || target.Scheme != "https" || target.Host == "" {
		return fmt.Errorf("HTTP/2 requires an https connect URL")
	}
	warmupStart := time.Unix(cfg.startUnix, 0)
	record := newRecorder(warmupStart)
	stopProgress := startLiveProgress(cfg, record)
	defer stopProgress()
	if delay := time.Until(warmupStart); delay > 0 {
		time.Sleep(delay)
	}
	if time.Now().After(warmupStart.Add(time.Second)) {
		return fmt.Errorf("stage start missed by more than one second")
	}

	groups := make([]*http2Connection, 0, cfg.connections)
	queueSize := http2QueueSize(cfg)
	for id := 1; id <= cfg.connections; id++ {
		dialer := &net.Dialer{Timeout: time.Duration(cfg.timeoutSeconds) * time.Second, KeepAlive: 30 * time.Second}
		tlsConfig, tlsErr := verifiedTLSConfig(cfg, "h2")
		if tlsErr != nil {
			return tlsErr
		}
		transport := &http.Transport{
			ForceAttemptHTTP2: true,
			MaxConnsPerHost:   1, MaxIdleConns: 1, MaxIdleConnsPerHost: 1,
			IdleConnTimeout: 330 * time.Second,
			DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
				connection, dialErr := dialer.DialContext(ctx, network, address)
				if dialErr != nil {
					return nil, dialErr
				}
				record.tcpOpened()
				return &trackedConn{Conn: connection, recorder: record}, nil
			},
			TLSClientConfig: tlsConfig,
		}
		group := &http2Connection{
			id: id, config: cfg, recorder: record, transport: transport,
			client: &http.Client{Transport: transport}, jobs: make(chan job, queueSize),
		}
		groups = append(groups, group)
		group.wg.Add(1)
		record.idle.Add(1)
		record.active.Add(1)
		go group.bootstrap()
	}

	rng := rand.New(rand.NewPCG(uint64(cfg.seed), uint64(cfg.seed)^0x9e3779b97f4a7c15))
	nextID := uint64(1)
	var warmupRecord *recorder
	warmupAborted := false
	if cfg.warmupSeconds > 0 {
		warmupExpected := cfg.rps * cfg.warmupSeconds
		nextID, warmupAborted = runHTTP2Phase(cfg, groups, record, payloads, rng, warmupStart, cfg.warmupSeconds, nextID, "off")
		if warmupAborted {
			warmupCfg := cfg
			warmupCfg.durationSeconds = cfg.warmupSeconds
			warmupCfg.warmupSeconds = 0
			warmupCfg.aborted = true
			warmupCfg.instrumentation = "off"
			if err := writeWarmupOutput(warmupCfg, payloadSHA, warmupExpected, record); err != nil {
				return err
			}
			closeHTTP2Groups(groups)
			return errors.New("load stage aborted during persistent warm-up")
		}
		cfg.warmupPhysical = record.physicalSnapshot()
		cfg.warmupConnected = record.connected.Load()
	}
	measurementStart := warmupStart
	if cfg.warmupSeconds > 0 {
		measurementStart = warmupStart.Add(time.Duration(cfg.warmupSeconds+10) * time.Second)
		if !measurementStart.After(time.Now()) {
			missedAt := time.Now()
			closeHTTP2Groups(groups)
			warmupRecord = record.detachForMeasurement(measurementStart)
			warmupCfg := cfg
			warmupCfg.durationSeconds = cfg.warmupSeconds
			warmupCfg.warmupSeconds = 0
			warmupCfg.instrumentation = "off"
			if err := writeWarmupOutput(warmupCfg, payloadSHA, cfg.rps*cfg.warmupSeconds, warmupRecord); err != nil {
				return err
			}
			warmupContent, err := os.ReadFile(filepath.Join(cfg.outputDir, "warmup-summary.json"))
			if err != nil {
				return err
			}
			measurementCfg := cfg
			measurementCfg.startUnix = measurementStart.Unix()
			measurementCfg.aborted = true
			measurementCfg.instrumentation = "on"
			if err := json.Unmarshal(warmupContent, &measurementCfg.warmupSummary); err != nil {
				return fmt.Errorf("decode warm-up summary: %w", err)
			}
			if err := writeOutput(measurementCfg, payloadSHA, cfg.rps*cfg.durationSeconds, record); err != nil {
				return err
			}
			return fmt.Errorf("shared measurement epoch missed: now=%s measurement_start=%s", missedAt.UTC(), measurementStart.UTC())
		}
		warmupRecord = record.detachForMeasurement(measurementStart)
	}
	total := cfg.rps * cfg.durationSeconds
	_, measurementAborted := runHTTP2Phase(cfg, groups, record, payloads, rng, measurementStart, cfg.durationSeconds, nextID, "on")
	closeHTTP2Groups(groups)
	measurementCfg := cfg
	measurementCfg.startUnix = measurementStart.Unix()
	measurementCfg.aborted = measurementAborted
	measurementCfg.instrumentation = "on"
	if warmupRecord != nil {
		warmupCfg := cfg
		warmupCfg.durationSeconds = cfg.warmupSeconds
		warmupCfg.warmupSeconds = 0
		warmupCfg.aborted = warmupAborted
		warmupCfg.instrumentation = "off"
		if err := writeWarmupOutput(warmupCfg, payloadSHA, cfg.rps*cfg.warmupSeconds, warmupRecord); err != nil {
			return err
		}
		warmupContent, err := os.ReadFile(filepath.Join(cfg.outputDir, "warmup-summary.json"))
		if err != nil {
			return err
		}
		if err := json.Unmarshal(warmupContent, &measurementCfg.warmupSummary); err != nil {
			return fmt.Errorf("decode warm-up summary: %w", err)
		}
	}
	if err := writeOutput(measurementCfg, payloadSHA, total, record); err != nil {
		return err
	}
	if measurementAborted {
		return errors.New("load stage aborted during scored measurement")
	}
	return nil
}

func closeHTTP2Groups(groups []*http2Connection) {
	for _, group := range groups {
		close(group.jobs)
	}
	for _, group := range groups {
		group.wg.Wait()
		group.transport.CloseIdleConnections()
	}
}

func writeWarmupOutput(cfg config, payloadSHA string, expected int, record *recorder) error {
	if err := writeOutput(cfg, payloadSHA, expected, record); err != nil {
		return err
	}
	for _, pair := range [][2]string{{"reference-summary.json", "warmup-summary.json"}, {"per-second.ndjson", "warmup-per-second.ndjson"}, {"traces.ndjson", "warmup-traces.ndjson"}} {
		if err := os.Rename(filepath.Join(cfg.outputDir, pair[0]), filepath.Join(cfg.outputDir, pair[1])); err != nil {
			return err
		}
	}
	return nil
}

func runHTTP2Phase(cfg config, groups []*http2Connection, record *recorder, payloads [][]byte, rng *rand.Rand, start time.Time, durationSeconds int, firstID uint64, instrumentation string) (uint64, bool) {
	total := cfg.rps * durationSeconds
	period := time.Second / time.Duration(cfg.rps)
	var completed sync.WaitGroup
	scheduledCount := 0
phaseLoop:
	for index := 0; index < total; index++ {
		if abortRequested.Load() {
			break
		}
		scheduled := start.Add(time.Duration(index) * time.Second / time.Duration(cfg.rps))
		if wait := time.Until(scheduled); wait > 0 {
			timer := time.NewTimer(wait)
			if abortSignal == nil {
				<-timer.C
			} else {
				select {
				case <-timer.C:
				case <-abortSignal:
				}
			}
			if abortRequested.Load() && !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
		}
		if abortRequested.Load() {
			break
		}
		record.scheduled(scheduled)
		payloadIndex := rng.IntN(len(payloads))
		item := job{id: firstID + uint64(index), scheduled: scheduled, payload: payloads[payloadIndex], payloadIndex: payloadIndex, dispatched: time.Now(), done: &completed, instrumentation: instrumentation}
		completed.Add(1)
		if abortSignal == nil {
			groups[index%len(groups)].jobs <- item
		} else {
			select {
			case groups[index%len(groups)].jobs <- item:
			case <-abortSignal:
				completed.Done()
				break phaseLoop
			}
		}
		record.dispatched(item, period)
		scheduledCount++
	}
	completed.Wait()
	return firstID + uint64(scheduledCount), scheduledCount != total
}

type http3Connection struct {
	id        int
	config    config
	recorder  *recorder
	client    *http.Client
	transport *http3.Transport
	jobs      chan job
	current   atomic.Int64
	dialCount atomic.Uint64
	connKey   atomic.Value
	wg        sync.WaitGroup
}

func runHTTP3(cfg config) error {
	payloads, payloadSHA, err := readPayloads(cfg.payloadPath)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(cfg.outputDir, 0o700); err != nil {
		return err
	}
	target, err := url.Parse(cfg.connectURL)
	if err != nil || target.Scheme != "https" || target.Host == "" {
		return fmt.Errorf("HTTP/3 requires an https connect URL")
	}
	start := time.Unix(cfg.startUnix, 0)
	record := newRecorder(start)
	if delay := time.Until(start); delay > 0 {
		time.Sleep(delay)
	}
	if time.Now().After(start.Add(time.Second)) {
		return fmt.Errorf("stage start missed by more than one second")
	}

	total := cfg.rps * cfg.durationSeconds
	groups := make([]*http3Connection, 0, cfg.connections)
	queueSize := max(4, (total+cfg.connections-1)/cfg.connections+1)
	for id := 1; id <= cfg.connections; id++ {
		tlsConfig, tlsErr := verifiedTLSConfig(cfg, http3.NextProtoH3)
		if tlsErr != nil {
			return tlsErr
		}
		group := &http3Connection{id: id, config: cfg, recorder: record, jobs: make(chan job, queueSize)}
		transport := &http3.Transport{
			TLSClientConfig: tlsConfig,
			QUICConfig: &quic.Config{
				Versions:             []quic.Version{quic.Version1},
				HandshakeIdleTimeout: time.Duration(cfg.timeoutSeconds) * time.Second,
				MaxIdleTimeout:       330 * time.Second,
				KeepAlivePeriod:      30 * time.Second,
				MaxIncomingStreams:   -1,
			},
			Dial: func(ctx context.Context, addr string, tlsCfg *tls.Config, quicCfg *quic.Config) (*quic.Conn, error) {
				connection, dialErr := quic.DialAddr(ctx, addr, tlsCfg, quicCfg)
				if dialErr != nil {
					return nil, dialErr
				}
				key := fmt.Sprintf("%p", connection)
				group.connKey.Store(key)
				count := group.dialCount.Add(1)
				record.connected.Add(1)
				if count == 1 {
					record.initialConnected.Add(1)
				}
				return connection, nil
			},
		}
		group.transport = transport
		group.client = &http.Client{Transport: transport}
		groups = append(groups, group)
		group.wg.Add(1)
		record.idle.Add(1)
		record.active.Add(1)
		go group.serve()
	}

	rng := rand.New(rand.NewPCG(uint64(cfg.seed), uint64(cfg.seed)^0x9e3779b97f4a7c15))
	period := time.Second / time.Duration(cfg.rps)
	for index := 0; index < total; index++ {
		scheduled := start.Add(time.Duration(index) * time.Second / time.Duration(cfg.rps))
		if wait := time.Until(scheduled); wait > 0 {
			time.Sleep(wait)
		}
		record.scheduled(scheduled)
		payloadIndex := rng.IntN(len(payloads))
		item := job{id: uint64(index + 1), scheduled: scheduled, payload: payloads[payloadIndex], payloadIndex: payloadIndex, dispatched: time.Now()}
		groups[index%len(groups)].jobs <- item
		record.dispatched(item, period)
	}
	for _, group := range groups {
		close(group.jobs)
	}
	for _, group := range groups {
		group.wg.Wait()
		if closeErr := group.transport.Close(); closeErr != nil {
			return closeErr
		}
		record.closed.Add(group.dialCount.Load())
	}
	return writeOutput(cfg, payloadSHA, total, record)
}

func (c *http3Connection) serve() {
	defer c.wg.Done()
	for item := range c.jobs {
		assigned := time.Now()
		current := c.current.Add(1)
		c.recorder.observeStreamConcurrency(current)
		c.recorder.idle.Add(-1)
		c.recorder.busy.Add(1)
		trace := c.execute(item, assigned)
		c.recorder.busy.Add(-1)
		c.recorder.idle.Add(1)
		c.current.Add(-1)
		c.recorder.completed(trace)
	}
}

func (c *http3Connection) execute(item job, assigned time.Time) requestTrace {
	trace := requestTrace{
		ForceTrace: c.config.traceAll,
		ID:         item.id, TraceID: amznTraceID(c.config, item), ConnectionID: c.id, DeviceID: deviceID(c.config, c.id), StreamSlot: 1, PayloadIndex: item.payloadIndex,
		ScheduledUnixNanoseconds: item.scheduled.UnixNano(), DispatchUnixNanoseconds: item.dispatched.UnixNano(),
		ConnectionAssignedUnixNanoseconds: assigned.UnixNano(),
		DispatchLatenessNanoseconds:       max(0, item.dispatched.Sub(item.scheduled).Nanoseconds()),
		AssignmentLatenessNanoseconds:     max(0, assigned.Sub(item.scheduled).Nanoseconds()),
		Protocol:                          "h3",
	}
	ctx, cancel := context.WithDeadline(context.Background(), item.scheduled.Add(time.Duration(c.config.timeoutSeconds)*time.Second))
	defer cancel()
	requestURL := strings.TrimRight(c.config.connectURL, "/") + c.config.path
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, requestURL, bytes.NewReader(item.payload))
	if err != nil {
		return completeHTTP2Error(trace, "request create", err)
	}
	request.GetBody = nil
	request.Host = c.config.hostHeader
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Request-Id", fmt.Sprintf("ref-%s-%s-%d", c.config.node, c.config.process, item.id))
	request.Header.Set("X-Amzn-Trace-Id", trace.TraceID)
	request.Header.Set("X-Loopad-Stage", c.config.stage)
	request.Header.Set("X-Loopad-Repetition", c.config.repetition)
	request.Header.Set("X-Loopad-Target", "reference")
	instrumentation := item.instrumentation
	if instrumentation == "" {
		instrumentation = "on"
	}
	request.Header.Set("X-Loopad-Instrumentation", instrumentation)
	request.Header.Set("X-Loopad-Device-Id", trace.DeviceID)
	beforeDials := c.dialCount.Load()
	started := time.Now()
	trace.HeaderWriteUnixNanoseconds = started.UnixNano()
	response, err := c.client.Do(request)
	trace.RequestCompleteUnixNanoseconds = started.UnixNano()
	trace.FirstResponseByteUnixNanoseconds = time.Now().UnixNano()
	trace.NewConnection = c.dialCount.Load() > beforeDials
	if key := c.connKey.Load(); key != nil {
		trace.ConnectionKey = key.(string)
	}
	if err != nil {
		return completeHTTP2Error(trace, "round trip", err)
	}
	trace.Protocol = response.Proto
	trace.Status = response.StatusCode
	if response.TLS != nil {
		trace.NegotiatedProtocol = response.TLS.NegotiatedProtocol
	}
	_, bodyErr := io.Copy(io.Discard, response.Body)
	closeErr := response.Body.Close()
	trace.ResponseCompleteUnixNanoseconds = time.Now().UnixNano()
	trace.TotalNanoseconds = trace.ResponseCompleteUnixNanoseconds - trace.DispatchUnixNanoseconds
	trace.TTFBNanoseconds = trace.FirstResponseByteUnixNanoseconds - trace.RequestCompleteUnixNanoseconds
	trace.WriteNanoseconds = trace.RequestCompleteUnixNanoseconds - trace.HeaderWriteUnixNanoseconds
	if response.ProtoMajor != 3 || trace.NegotiatedProtocol != http3.NextProtoH3 {
		trace.Error = fmt.Sprintf("protocol mismatch: response=%s alpn=%s", response.Proto, trace.NegotiatedProtocol)
		return trace
	}
	if bodyErr != nil {
		return completeHTTP2Error(trace, "response body", bodyErr)
	}
	if closeErr != nil {
		return completeHTTP2Error(trace, "response close", closeErr)
	}
	return trace
}

func http2QueueSize(cfg config) int {
	total := cfg.rps * max(cfg.durationSeconds, cfg.warmupSeconds)
	return max(cfg.streamsPerConnection*4, (total+cfg.connections-1)/cfg.connections+1)
}

func (c *http2Connection) serve(slot int) {
	defer c.wg.Done()
	for item := range c.jobs {
		c.process(item, slot)
	}
}

func (c *http2Connection) bootstrap() {
	defer c.wg.Done()
	item, ok := <-c.jobs
	if !ok {
		return
	}
	c.process(item, 1)
	// The first request establishes exactly one HTTP/2 connection for this
	// transport. Starting every stream goroutine before that handshake lets the
	// standard transport race multiple TCP dials for the same logical group.
	for slot := 2; slot <= c.config.streamsPerConnection; slot++ {
		c.wg.Add(1)
		c.recorder.idle.Add(1)
		c.recorder.active.Add(1)
		go c.serve(slot)
	}
	for item := range c.jobs {
		c.process(item, 1)
	}
}

func (c *http2Connection) process(item job, slot int) {
	if item.done != nil {
		defer item.done.Done()
	}
	assigned := time.Now()
	current := c.current.Add(1)
	c.recorder.observeStreamConcurrency(current)
	defer c.current.Add(-1)
	c.recorder.idle.Add(-1)
	c.recorder.busy.Add(1)
	trace := c.execute(item, assigned, slot)
	c.recorder.busy.Add(-1)
	c.recorder.idle.Add(1)
	c.recorder.completed(trace)
}

func (c *http2Connection) execute(item job, assigned time.Time, slot int) requestTrace {
	trace := requestTrace{
		ForceTrace: c.config.traceAll,
		ID:         item.id, TraceID: amznTraceID(c.config, item), ConnectionID: c.id, DeviceID: deviceID(c.config, c.id), StreamSlot: slot, PayloadIndex: item.payloadIndex,
		ScheduledUnixNanoseconds: item.scheduled.UnixNano(), DispatchUnixNanoseconds: item.dispatched.UnixNano(),
		ConnectionAssignedUnixNanoseconds: assigned.UnixNano(),
		DispatchLatenessNanoseconds:       max(0, item.dispatched.Sub(item.scheduled).Nanoseconds()),
		AssignmentLatenessNanoseconds:     max(0, assigned.Sub(item.scheduled).Nanoseconds()),
		Protocol:                          "h2",
	}
	requestURL := strings.TrimRight(c.config.connectURL, "/") + c.config.path
	ctx, cancel := context.WithDeadline(context.Background(), item.scheduled.Add(time.Duration(c.config.timeoutSeconds)*time.Second))
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, requestURL, bytes.NewReader(item.payload))
	if err != nil {
		return completeHTTP2Error(trace, "request create", err)
	}
	// A replayable request body permits net/http to retry internally after a
	// reused HTTP/2 connection failure. This experiment must surface that event
	// as an error instead of silently issuing the same request on another socket.
	request.GetBody = nil
	request.Host = c.config.hostHeader
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Request-Id", fmt.Sprintf("ref-%s-%s-%d", c.config.node, c.config.process, item.id))
	request.Header.Set("X-Amzn-Trace-Id", trace.TraceID)
	request.Header.Set("X-Loopad-Stage", c.config.stage)
	request.Header.Set("X-Loopad-Repetition", c.config.repetition)
	request.Header.Set("X-Loopad-Target", "reference")
	request.Header.Set("X-Loopad-Instrumentation", "on")
	request.Header.Set("X-Loopad-Device-Id", trace.DeviceID)
	var connectStart, connectDone, tlsStart, tlsDone, wroteHeaders, wroteRequest, firstResponse, gotConnCount atomic.Int64
	var connectionKey, negotiatedProtocol atomic.Value
	var newConnection atomic.Bool
	clientTrace := &httptrace.ClientTrace{
		ConnectStart: func(_, _ string) { connectStart.Store(time.Now().UnixNano()) },
		ConnectDone: func(_, _ string, connectErr error) {
			done := time.Now().UnixNano()
			connectDone.Store(done)
			if started := connectStart.Load(); connectErr == nil && started > 0 {
				c.recorder.connection(time.Duration(done - started))
			}
		},
		TLSHandshakeStart: func() { tlsStart.Store(time.Now().UnixNano()) },
		TLSHandshakeDone: func(state tls.ConnectionState, _ error) {
			tlsDone.Store(time.Now().UnixNano())
			negotiatedProtocol.Store(state.NegotiatedProtocol)
		},
		GotConn: func(info httptrace.GotConnInfo) {
			gotConnCount.Add(1)
			connectionKey.Store(fmt.Sprintf("%p", info.Conn))
			newConnection.Store(!info.Reused)
			if tlsConnection, ok := info.Conn.(*tls.Conn); ok {
				negotiatedProtocol.Store(tlsConnection.ConnectionState().NegotiatedProtocol)
			}
			if !info.Reused {
				c.recorder.connected.Add(1)
				if c.seen.CompareAndSwap(false, true) {
					c.recorder.initialConnected.Add(1)
				}
			}
		},
		WroteHeaders: func() { wroteHeaders.Store(time.Now().UnixNano()) },
		WroteRequest: func(info httptrace.WroteRequestInfo) {
			wroteRequest.Store(time.Now().UnixNano())
		},
		GotFirstResponseByte: func() { firstResponse.Store(time.Now().UnixNano()) },
	}
	request = request.WithContext(httptrace.WithClientTrace(request.Context(), clientTrace))
	response, err := c.client.Do(request)
	trace.ConnectStartUnixNanoseconds = connectStart.Load()
	trace.ConnectDoneUnixNanoseconds = connectDone.Load()
	trace.TLSStartUnixNanoseconds = tlsStart.Load()
	trace.TLSDoneUnixNanoseconds = tlsDone.Load()
	trace.HeaderWriteUnixNanoseconds = wroteHeaders.Load()
	trace.BodyLastWriteUnixNanoseconds = wroteRequest.Load()
	trace.RequestCompleteUnixNanoseconds = trace.BodyLastWriteUnixNanoseconds
	trace.FirstResponseByteUnixNanoseconds = firstResponse.Load()
	trace.NewConnection = newConnection.Load()
	trace.HiddenRetryCount = max(0, int(gotConnCount.Load()-1))
	if value := connectionKey.Load(); value != nil {
		trace.ConnectionKey = value.(string)
	}
	if value := negotiatedProtocol.Load(); value != nil {
		trace.NegotiatedProtocol = value.(string)
	}
	if trace.RequestCompleteUnixNanoseconds > 0 {
		trace.WriteNanoseconds = trace.RequestCompleteUnixNanoseconds - trace.HeaderWriteUnixNanoseconds
	}
	if trace.FirstResponseByteUnixNanoseconds > 0 && trace.RequestCompleteUnixNanoseconds > 0 {
		trace.TTFBNanoseconds = trace.FirstResponseByteUnixNanoseconds - trace.RequestCompleteUnixNanoseconds
	}
	if err != nil {
		return completeHTTP2Error(trace, "round trip", err)
	}
	trace.Protocol = response.Proto
	trace.Status = response.StatusCode
	_, bodyErr := io.Copy(io.Discard, response.Body)
	closeErr := response.Body.Close()
	trace.ResponseCompleteUnixNanoseconds = time.Now().UnixNano()
	trace.TotalNanoseconds = trace.ResponseCompleteUnixNanoseconds - trace.DispatchUnixNanoseconds
	if response.ProtoMajor != 2 || trace.NegotiatedProtocol != "h2" {
		trace.Error = fmt.Sprintf("protocol mismatch: response=%s alpn=%s", response.Proto, trace.NegotiatedProtocol)
		return trace
	}
	if bodyErr != nil {
		return completeHTTP2Error(trace, "response body", bodyErr)
	}
	if closeErr != nil {
		return completeHTTP2Error(trace, "response close", closeErr)
	}
	return trace
}

func completeHTTP2Error(trace requestTrace, phase string, err error) requestTrace {
	trace.Error = phase + ": " + err.Error()
	trace.ResponseCompleteUnixNanoseconds = time.Now().UnixNano()
	trace.TotalNanoseconds = trace.ResponseCompleteUnixNanoseconds - trace.DispatchUnixNanoseconds
	return trace
}

func connectWorkers(ctx context.Context, cfg config, record *recorder, jobs <-chan job, workerGroup *sync.WaitGroup) error {
	type result struct {
		worker   *worker
		duration time.Duration
		err      error
	}
	results := make(chan result, cfg.connections)
	for index := 0; index < cfg.connections; index++ {
		go func(id int) {
			started := time.Now()
			connection, err := dial(ctx, cfg)
			if err != nil {
				results <- result{err: err}
				return
			}
			item := &worker{id: id, config: cfg, recorder: record, connection: connection, reader: bufio.NewReaderSize(connection, 64*1024), newConnection: true}
			results <- result{worker: item, duration: time.Since(started)}
		}(index + 1)
	}
	var connectionErrors int
	for range cfg.connections {
		result := <-results
		if result.err != nil {
			connectionErrors++
			continue
		}
		record.connected.Add(1)
		record.initialConnected.Add(1)
		record.idle.Add(1)
		record.active.Add(1)
		record.connection(result.duration)
		workerGroup.Add(1)
		go func(item *worker) {
			defer workerGroup.Done()
			item.serve(jobs)
			if item.connection != nil {
				_ = item.connection.Close()
				record.closed.Add(1)
				item.connection = nil
			}
		}(result.worker)
	}
	if connectionErrors > 0 {
		return fmt.Errorf("%d initial TCP connections failed", connectionErrors)
	}
	return nil
}

func dial(ctx context.Context, cfg config) (*timedConn, error) {
	target, err := url.Parse(cfg.connectURL)
	if err != nil || (target.Scheme != "http" && target.Scheme != "https") || target.Host == "" {
		return nil, fmt.Errorf("invalid connect URL")
	}
	host := target.Host
	if !strings.Contains(host, ":") {
		host += map[bool]string{true: ":443", false: ":80"}[target.Scheme == "https"]
	}
	dialer := net.Dialer{Timeout: time.Duration(cfg.timeoutSeconds) * time.Second, KeepAlive: 30 * time.Second}
	connection, err := dialer.DialContext(ctx, "tcp", host)
	if err != nil {
		return nil, err
	}
	if target.Scheme == "https" {
		tlsConfig, tlsErr := verifiedTLSConfig(cfg, "http/1.1")
		if tlsErr != nil {
			_ = connection.Close()
			return nil, tlsErr
		}
		tlsConnection := tls.Client(connection, tlsConfig)
		if err := tlsConnection.HandshakeContext(ctx); err != nil {
			_ = connection.Close()
			return nil, err
		}
		if negotiated := tlsConnection.ConnectionState().NegotiatedProtocol; negotiated != "http/1.1" {
			_ = tlsConnection.Close()
			return nil, fmt.Errorf("expected ALPN http/1.1, got %q", negotiated)
		}
		connection = tlsConnection
	}
	return &timedConn{Conn: connection, protocol: map[bool]string{true: "http/1.1", false: "http/1.1-cleartext"}[target.Scheme == "https"], key: fmt.Sprintf("%p", connection)}, nil
}

func (w *worker) serve(jobs <-chan job) {
	for item := range jobs {
		assigned := time.Now()
		w.recorder.idle.Add(-1)
		w.recorder.busy.Add(1)
		trace := w.execute(item, assigned)
		w.recorder.busy.Add(-1)
		w.recorder.idle.Add(1)
		w.recorder.completed(trace)
	}
}

func (w *worker) execute(item job, assigned time.Time) requestTrace {
	trace := requestTrace{
		ForceTrace: w.config.traceAll,
		ID:         item.id, TraceID: amznTraceID(w.config, item), ConnectionID: w.id, DeviceID: deviceID(w.config, w.id), StreamSlot: 1, PayloadIndex: item.payloadIndex,
		ScheduledUnixNanoseconds: item.scheduled.UnixNano(), DispatchUnixNanoseconds: item.dispatched.UnixNano(),
		ConnectionAssignedUnixNanoseconds: assigned.UnixNano(),
		DispatchLatenessNanoseconds:       max(0, item.dispatched.Sub(item.scheduled).Nanoseconds()),
		AssignmentLatenessNanoseconds:     max(0, assigned.Sub(item.scheduled).Nanoseconds()),
		NewConnection:                     w.newConnection,
	}
	w.recorder.observeStreamConcurrency(1)
	w.newConnection = false
	if w.connection == nil {
		started := time.Now()
		connection, err := dial(context.Background(), w.config)
		if err != nil {
			trace.Error = "connect: " + err.Error()
			return trace
		}
		w.connection, w.reader, w.newConnection = connection, bufio.NewReaderSize(connection, 64*1024), true
		w.recorder.connected.Add(1)
		w.recorder.connection(time.Since(started))
		trace.NewConnection = true
	}
	trace.Protocol = "HTTP/1.1"
	trace.NegotiatedProtocol = w.connection.protocol
	trace.ConnectionKey = w.connection.key
	_ = w.connection.SetDeadline(time.Now().Add(time.Duration(w.config.timeoutSeconds) * time.Second))
	w.connection.firstRead.Store(0)
	header := requestHeader(w.config, item, trace.DeviceID, trace.TraceID)
	writeStarted := time.Now()
	trace.HeaderWriteUnixNanoseconds = writeStarted.UnixNano()
	if err := writeAll(w.connection, []byte(header)); err != nil {
		return w.fail(trace, "header write", err)
	}
	trace.BodyFirstWriteUnixNanoseconds = time.Now().UnixNano()
	if err := writeAll(w.connection, item.payload); err != nil {
		return w.fail(trace, "body write", err)
	}
	trace.BodyLastWriteUnixNanoseconds = time.Now().UnixNano()
	trace.RequestCompleteUnixNanoseconds = trace.BodyLastWriteUnixNanoseconds
	trace.WriteNanoseconds = time.Since(writeStarted).Nanoseconds()
	response, err := http.ReadResponse(w.reader, nil)
	if err != nil {
		return w.fail(trace, "response read", err)
	}
	trace.FirstResponseByteUnixNanoseconds = w.connection.firstRead.Load()
	if trace.FirstResponseByteUnixNanoseconds > 0 {
		trace.TTFBNanoseconds = trace.FirstResponseByteUnixNanoseconds - trace.RequestCompleteUnixNanoseconds
	}
	_, bodyErr := io.Copy(io.Discard, response.Body)
	closeErr := response.Body.Close()
	trace.ResponseCompleteUnixNanoseconds = time.Now().UnixNano()
	trace.TotalNanoseconds = trace.ResponseCompleteUnixNanoseconds - trace.DispatchUnixNanoseconds
	trace.Status = response.StatusCode
	if bodyErr != nil {
		return w.fail(trace, "response body", bodyErr)
	}
	if closeErr != nil {
		return w.fail(trace, "response close", closeErr)
	}
	return trace
}

func (w *worker) fail(trace requestTrace, phase string, err error) requestTrace {
	trace.Error = phase + ": " + err.Error()
	trace.ResponseCompleteUnixNanoseconds = time.Now().UnixNano()
	trace.TotalNanoseconds = trace.ResponseCompleteUnixNanoseconds - trace.DispatchUnixNanoseconds
	if w.connection != nil {
		_ = w.connection.Close()
		w.connection = nil
		w.recorder.closed.Add(1)
	}
	return trace
}

func requestHeader(cfg config, item job, device, traceID string) string {
	requestID := fmt.Sprintf("ref-%s-%s-%d", cfg.node, cfg.process, item.id)
	return fmt.Sprintf("POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nX-Request-Id: %s\r\nX-Amzn-Trace-Id: %s\r\nX-Loopad-Stage: %s\r\nX-Loopad-Repetition: %s\r\nX-Loopad-Target: reference\r\nX-Loopad-Instrumentation: on\r\nX-Loopad-Device-Id: %s\r\nConnection: keep-alive\r\n\r\n",
		cfg.path, cfg.hostHeader, len(item.payload), requestID, traceID, cfg.stage, cfg.repetition, device)
}

func amznTraceID(cfg config, item job) string {
	// Keep the X-Ray root stable across the driver, ALB access log, and
	// collector trace without exposing payload contents. Node and process are
	// part of the digest because each process starts request IDs at zero.
	digest := sha256.Sum256([]byte(fmt.Sprintf("%s/%s/%s/%s/%d/%d", cfg.stage, cfg.repetition, cfg.node, cfg.process, cfg.seed, item.id)))
	seconds := item.scheduled.Unix()
	if seconds < 0 {
		seconds = 0
	}
	return fmt.Sprintf("Root=1-%08x-%s", uint64(seconds)&0xffffffff, hex.EncodeToString(digest[:12]))
}

func deviceID(cfg config, connectionID int) string {
	return fmt.Sprintf("%s/%s/%06d", cfg.node, cfg.process, connectionID)
}

func verifiedTLSConfig(cfg config, nextProtocol string) (*tls.Config, error) {
	if cfg.caCertPath == "" || cfg.tlsServerName == "" {
		return nil, fmt.Errorf("verified TLS requires --ca-cert and --tls-server-name")
	}
	certificate, err := os.ReadFile(cfg.caCertPath)
	if err != nil {
		return nil, fmt.Errorf("read CA certificate: %w", err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(certificate) {
		return nil, fmt.Errorf("CA certificate does not contain a valid PEM certificate")
	}
	minimumVersion := uint16(tls.VersionTLS12)
	if nextProtocol == http3.NextProtoH3 {
		minimumVersion = tls.VersionTLS13
	}
	return &tls.Config{
		RootCAs: roots, ServerName: cfg.tlsServerName, MinVersion: minimumVersion, NextProtos: []string{nextProtocol},
	}, nil
}

func writeAll(writer io.Writer, content []byte) error {
	for len(content) > 0 {
		written, err := writer.Write(content)
		if err != nil {
			return err
		}
		if written == 0 {
			return io.ErrShortWrite
		}
		content = content[written:]
	}
	return nil
}

func (r *recorder) bin(timestamp time.Time) *secondBin {
	second := int(timestamp.Sub(r.start) / time.Second)
	if second < 0 {
		second = 0
	}
	bin := r.bins[second]
	if bin == nil {
		bin = &secondBin{Statuses: map[int]uint64{}}
		r.bins[second] = bin
	}
	return bin
}

func (r *recorder) scheduled(timestamp time.Time) {
	r.mu.Lock()
	bin := r.bin(timestamp)
	bin.Scheduled++
	bin.ActiveHigh = max(bin.ActiveHigh, r.active.Load())
	if bin.IdleLow == 0 {
		bin.IdleLow = r.idle.Load()
	} else {
		bin.IdleLow = min(bin.IdleLow, r.idle.Load())
	}
	bin.BusyHigh = max(bin.BusyHigh, r.busy.Load())
	r.mu.Unlock()
}

func (r *recorder) dispatched(item job, period time.Duration) {
	r.mu.Lock()
	defer r.mu.Unlock()
	bin := r.bin(item.dispatched)
	bin.Dispatched++
	lateness := max(0, item.dispatched.Sub(item.scheduled).Nanoseconds())
	bin.DispatchLateness = append(bin.DispatchLateness, lateness)
	if time.Duration(lateness) > period {
		bin.Catchup++
	}
}

func (r *recorder) connection(duration time.Duration) {
	r.mu.Lock()
	defer r.mu.Unlock()
	bin := r.bin(time.Now())
	bin.NewConnections++
	bin.Connect = append(bin.Connect, duration.Nanoseconds())
}

func (r *recorder) tcpOpened() {
	current := r.tcpActive.Add(1)
	for {
		high := r.tcpHigh.Load()
		if current <= high || r.tcpHigh.CompareAndSwap(high, current) {
			return
		}
	}
}

func (r *recorder) observeStreamConcurrency(current int64) {
	for {
		high := r.streamHigh.Load()
		if current <= high || r.streamHigh.CompareAndSwap(high, current) {
			return
		}
	}
}

func (r *recorder) completed(trace requestTrace) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.liveServiceCounts) == 0 {
		r.liveServiceCounts = make([]uint64, liveProgressMaxNanoseconds/liveProgressBucketWidthNanoseconds+1)
	}
	if r.protocols == nil {
		r.protocols = map[string]uint64{}
	}
	if r.physical == nil {
		r.physical = map[string]struct{}{}
	}
	if r.connectionOwners == nil {
		r.connectionOwners = map[string]string{}
	}
	if r.connectionUses == nil {
		r.connectionUses = map[string]uint64{}
	}
	if trace.Protocol != "" {
		r.protocols[trace.Protocol]++
	}
	if trace.ConnectionKey != "" {
		r.physical[trace.ConnectionKey] = struct{}{}
		r.connectionUses[trace.ConnectionKey]++
		if owner, exists := r.connectionOwners[trace.ConnectionKey]; exists && owner != trace.DeviceID {
			r.crossDeviceReuse++
		} else if !exists {
			r.connectionOwners[trace.ConnectionKey] = trace.DeviceID
		}
	}
	if strings.HasPrefix(trace.Error, "protocol mismatch:") {
		r.protocolErrors++
	}
	if trace.HiddenRetryCount > 0 {
		r.hiddenRetries.Add(uint64(trace.HiddenRetryCount))
	}
	assignedBin := r.bin(time.Unix(0, trace.ConnectionAssignedUnixNanoseconds))
	assignedBin.Assigned++
	assignedBin.AssignmentLateness = append(assignedBin.AssignmentLateness, trace.AssignmentLatenessNanoseconds)
	completionTime := trace.ResponseCompleteUnixNanoseconds
	if completionTime == 0 {
		completionTime = time.Now().UnixNano()
	}
	bin := r.bin(time.Unix(0, completionTime))
	bin.Completed++
	bin.Write = append(bin.Write, trace.WriteNanoseconds)
	bin.TTFB = append(bin.TTFB, trace.TTFBNanoseconds)
	bin.Total = append(bin.Total, trace.TotalNanoseconds)
	liveIndex := (max(0, trace.TotalNanoseconds) + liveProgressBucketWidthNanoseconds - 1) / liveProgressBucketWidthNanoseconds
	if liveIndex >= int64(len(r.liveServiceCounts)) {
		liveIndex = int64(len(r.liveServiceCounts) - 1)
		r.liveServiceOverflow++
	}
	r.liveServiceCounts[liveIndex]++
	bin.ScheduledTotal = append(bin.ScheduledTotal, max(0, completionTime-trace.ScheduledUnixNanoseconds))
	if !trace.NewConnection {
		bin.ReusedConnections++
	}
	if trace.Error != "" {
		bin.Errors++
	}
	if trace.Status != 0 {
		bin.Statuses[trace.Status]++
	}
	if sampled(trace) {
		encoded, _ := json.Marshal(trace)
		if r.traceBytes+uint64(len(encoded)+1) <= r.maxTraceBytes {
			r.traces = append(r.traces, trace)
			r.traceBytes += uint64(len(encoded) + 1)
		} else {
			r.traceDropped++
		}
	}
}

func sampled(trace requestTrace) bool {
	if trace.ForceTrace || trace.Error != "" || trace.TotalNanoseconds >= int64(150*time.Millisecond) {
		return true
	}
	hash := fnv.New64a()
	_, _ = hash.Write([]byte(strconv.FormatUint(trace.ID, 10)))
	return hash.Sum64()%100 < 5
}

func startLiveProgress(cfg config, record *recorder) func() {
	stop := make(chan struct{})
	done := make(chan struct{})
	write := func() {
		progress := record.liveProgress(cfg, time.Now())
		content, err := json.Marshal(progress)
		if err != nil {
			fmt.Fprintf(os.Stderr, "encode live progress: %v\n", err)
			return
		}
		temporary := filepath.Join(cfg.outputDir, ".live-progress.json.tmp")
		final := filepath.Join(cfg.outputDir, "live-progress.json")
		if err := os.WriteFile(temporary, append(content, '\n'), 0o600); err != nil {
			fmt.Fprintf(os.Stderr, "write live progress: %v\n", err)
			return
		}
		if err := os.Rename(temporary, final); err != nil {
			fmt.Fprintf(os.Stderr, "publish live progress: %v\n", err)
		}
	}
	go func() {
		defer close(done)
		write()
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				write()
			case <-stop:
				write()
				return
			}
		}
	}()
	return func() {
		close(stop)
		<-done
	}
}

func (r *recorder) liveProgress(cfg config, now time.Time) map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	elapsedSeconds := max(0, int(now.Sub(r.start)/time.Second))
	var scheduled, dispatched, completed, errors uint64
	statuses := map[int]uint64{}
	for _, bin := range r.bins {
		scheduled += bin.Scheduled
		dispatched += bin.Dispatched
		completed += bin.Completed
		errors += bin.Errors
		for status, count := range bin.Statuses {
			statuses[status] += count
		}
	}
	serviceCounts := append([]uint64(nil), r.liveServiceCounts...)
	serviceCount := uint64(0)
	for _, count := range serviceCounts {
		serviceCount += count
	}
	p95Nanoseconds := histogramQuantileUpper(serviceCounts, liveProgressBucketWidthNanoseconds, .95)
	var http5xx uint64
	for status, count := range statuses {
		if status >= 500 && status < 600 {
			http5xx += count
		}
	}
	scored := cfg.warmupSeconds == 0 || r.start.Unix() != cfg.startUnix
	return map[string]any{
		"schema_version": 1, "updated_at": now.UTC(), "phase_started_at": r.start.UTC(),
		"phase": map[bool]string{true: "measurement", false: "warmup"}[scored], "scored": scored,
		"planned_rps": cfg.rps, "target_physical_connections": cfg.connections, "elapsed_seconds": elapsedSeconds,
		"scheduled_requests": scheduled, "dispatched_requests": dispatched, "completed_requests": completed,
		"errors": errors, "statuses": statuses, "http_202": statuses[202], "http_429": statuses[429], "http_5xx": http5xx,
		"observed_physical_connections": len(r.physical), "initial_connection_establishment_count": r.initialConnected.Load(),
		"current_tcp_connections": r.tcpActive.Load(), "connection_establishment_count": r.connected.Load(),
		"service_histogram": map[string]any{
			"method": "fixed-width-upper-bound", "unit": "nanoseconds", "bucket_width_nanoseconds": liveProgressBucketWidthNanoseconds,
			"max_nanoseconds": liveProgressMaxNanoseconds, "counts": serviceCounts, "count": serviceCount,
			"overflow_count": r.liveServiceOverflow, "p95_upper_nanoseconds": p95Nanoseconds,
		},
		"trace_dropped": r.traceDropped, "protocol_errors": r.protocolErrors, "hidden_retries": r.hiddenRetries.Load(),
		"aborted": abortRequested.Load(),
	}
}

func writeOutput(cfg config, payloadSHA string, expected int, record *recorder) error {
	record.mu.Lock()
	defer record.mu.Unlock()
	seconds := make([]int, 0, len(record.bins))
	for second := range record.bins {
		seconds = append(seconds, second)
	}
	sort.Ints(seconds)
	perSecondFile, err := os.OpenFile(filepath.Join(cfg.outputDir, "per-second.ndjson"), os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	encoder := json.NewEncoder(perSecondFile)
	var totals secondBin
	totals.Statuses = map[int]uint64{}
	var cumulativeScheduled, cumulativeAssigned uint64
	for _, second := range seconds {
		bin := record.bins[second]
		cumulativeScheduled += bin.Scheduled
		cumulativeAssigned += bin.Assigned
		row := map[string]any{
			"second": second, "scheduled": bin.Scheduled, "dispatched": bin.Dispatched, "assigned": bin.Assigned,
			"completed": bin.Completed, "schedule_debt": max(0, int64(cumulativeScheduled)-int64(cumulativeAssigned)), "catchup_count": bin.Catchup,
			"new_connections": bin.NewConnections, "reused_connections": bin.ReusedConnections, "errors": bin.Errors, "statuses": bin.Statuses,
			"active_connections_high": bin.ActiveHigh, "idle_connections_low": bin.IdleLow, "busy_connections_high": bin.BusyHigh,
			"dispatch_lateness_ns": stats(bin.DispatchLateness), "assignment_lateness_ns": stats(bin.AssignmentLateness),
			"connect_ns": stats(bin.Connect), "write_ns": stats(bin.Write), "ttfb_ns": stats(bin.TTFB), "service_latency_ns": stats(bin.Total),
			"scheduled_latency_ns": stats(bin.ScheduledTotal), "total_ns": stats(bin.Total),
		}
		if err := encoder.Encode(row); err != nil {
			return err
		}
		mergeBin(&totals, bin)
	}
	if err := perSecondFile.Close(); err != nil {
		return err
	}
	traceFile, err := os.OpenFile(filepath.Join(cfg.outputDir, "traces.ndjson"), os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	traceEncoder := json.NewEncoder(traceFile)
	for _, trace := range record.traces {
		if err := traceEncoder.Encode(trace); err != nil {
			return err
		}
	}
	if err := traceFile.Close(); err != nil {
		return err
	}
	completed := totals.Completed
	expectedConnectionGenerations := http2ExpectedConnectionGenerations(cfg, expected)
	expectedNewConnections := uint64(cfg.connections * expectedConnectionGenerations)
	connectionContractValid := record.initialConnected.Load() == uint64(cfg.connections)
	if cfg.protocol == "h2" {
		// An ALB sends GOAWAY and closes a frontend HTTP/2 connection after it
		// serves more than 10,000 requests. The transport must replace those
		// connections, but an orderly draining generation may overlap only one
		// replacement generation and the total dial count must be deterministic.
		connectionContractValid = connectionContractValid && record.connected.Load() == expectedNewConnections &&
			record.tcpHigh.Load() <= int64(cfg.connections*2)
	} else {
		connectionContractValid = connectionContractValid && record.tcpHigh.Load() <= int64(cfg.connections)
	}
	connectionAccuracy := float64(record.initialConnected.Load()) / float64(cfg.connections)
	if connectionAccuracy < .99 || connectionAccuracy > 1.01 {
		connectionContractValid = false
	}
	var minimumRequestsPerConnection, maximumRequestsPerConnection uint64
	for _, count := range record.connectionUses {
		if minimumRequestsPerConnection == 0 || count < minimumRequestsPerConnection {
			minimumRequestsPerConnection = count
		}
		maximumRequestsPerConnection = max(maximumRequestsPerConnection, count)
	}
	serviceLatencyHistogram := fixedLatencyHistogram(totals.Total, cfg.timeoutSeconds)
	scheduledLatencyHistogram := fixedLatencyHistogram(totals.ScheduledTotal, cfg.timeoutSeconds)
	warmupValid := cfg.warmupSeconds == 0
	if cfg.warmupSeconds > 0 {
		warmupValid, _ = cfg.warmupSummary["valid"].(bool)
	}
	sharedWarmupPhysical := 0
	for key := range record.physical {
		if _, exists := cfg.warmupPhysical[key]; exists {
			sharedWarmupPhysical++
		}
	}
	connectionReuseAcrossReset := cfg.warmupSeconds > 0 && len(cfg.warmupPhysical) == cfg.connections &&
		len(record.physical) == cfg.connections && sharedWarmupPhysical == cfg.connections && totals.NewConnections == 0 &&
		record.connected.Load() == cfg.warmupConnected
	measurementValid := !cfg.aborted && totals.Scheduled >= uint64(math.Ceil(float64(expected)*0.99)) && totals.Dispatched >= uint64(math.Ceil(float64(expected)*0.99)) &&
		record.traceDropped == 0 && record.protocolErrors == 0 && record.hiddenRetries.Load() == 0 && record.crossDeviceReuse == 0 &&
		record.streamHigh.Load() <= 1 && connectionContractValid && serviceLatencyHistogram["overflow_count"].(uint64) == 0 &&
		scheduledLatencyHistogram["overflow_count"].(uint64) == 0
	if cfg.warmupSeconds > 0 {
		measurementValid = measurementValid && warmupValid && connectionReuseAcrossReset
	}
	summary := map[string]any{
		"schema_version": 4, "driver": "loopad-reference-" + cfg.protocol, "protocol": cfg.protocol, "node": cfg.node, "process": cfg.process,
		"started_at": time.Unix(cfg.startUnix, 0).UTC(), "measurement_started_at": time.Unix(cfg.startUnix, 0).UTC(), "ended_at": time.Now().UTC(),
		"rps": cfg.rps, "connections": cfg.connections, "duration_seconds": cfg.durationSeconds, "expected_requests": expected,
		"persistent_warmup_seconds": cfg.warmupSeconds, "connection_reuse_across_measurement_reset": connectionReuseAcrossReset,
		"warmup_summary": cfg.warmupSummary, "warmup_valid": warmupValid,
		"warmup_physical_connections": len(cfg.warmupPhysical), "measurement_physical_connections_shared_with_warmup": sharedWarmupPhysical,
		"warmup_connection_establishment_count": cfg.warmupConnected, "measurement_new_connections": totals.NewConnections,
		"streams_per_connection": cfg.streamsPerConnection, "maximum_stream_slots": cfg.connections * cfg.streamsPerConnection,
		"scheduled_requests": totals.Scheduled, "dispatched_requests": totals.Dispatched, "assigned_requests": totals.Assigned, "completed_requests": completed,
		"dispatched_requests_all": totals.Dispatched, "assigned_requests_all": totals.Assigned, "completed_requests_all": totals.Completed,
		"schedule_accuracy": float64(totals.Scheduled) / float64(expected), "dispatch_accuracy": float64(totals.Dispatched) / float64(expected),
		"achieved_rps": float64(completed) / float64(cfg.durationSeconds), "connection_establishment_count": record.initialConnected.Load(),
		"logical_devices": cfg.connections, "target_physical_connections": cfg.connections,
		"connection_establishment_accuracy": connectionAccuracy, "new_connections": record.connected.Load(), "closed_connections": record.closed.Load(),
		"alb_http2_requests_per_connection_limit": map[bool]int{true: 10_000, false: 0}[cfg.protocol == "h2"],
		"expected_connection_generations":         expectedConnectionGenerations, "expected_new_connections": expectedNewConnections,
		"observed_physical_connections": len(record.physical), "concurrent_physical_connections_high_water": record.tcpHigh.Load(), "observed_protocols": record.protocols, "protocol_errors": record.protocolErrors,
		"hidden_retries": record.hiddenRetries.Load(), "silent_drops": 0, "trace_count": len(record.traces), "trace_bytes": record.traceBytes,
		"cross_device_reuse_violations": record.crossDeviceReuse, "max_concurrent_streams_per_connection": record.streamHigh.Load(),
		"minimum_requests_per_observed_connection": minimumRequestsPerConnection, "maximum_requests_per_observed_connection": maximumRequestsPerConnection,
		"protocol_fallbacks": record.protocolErrors, "tls_verification": "required",
		"trace_dropped": record.traceDropped, "errors": totals.Errors, "statuses": totals.Statuses,
		"dispatch_lateness_ns": stats(totals.DispatchLateness), "assignment_lateness_ns": stats(totals.AssignmentLateness),
		"connect_ns": stats(totals.Connect), "write_ns": stats(totals.Write), "ttfb_ns": stats(totals.TTFB),
		"service_latency_ns": stats(totals.Total), "scheduled_latency_ns": stats(totals.ScheduledTotal), "total_ns": stats(totals.Total),
		"service_latency_histogram":   serviceLatencyHistogram,
		"scheduled_latency_histogram": scheduledLatencyHistogram,
		"payload_sha256":              payloadSHA, "seed": cfg.seed, "aborted": cfg.aborted,
		"collector_instrumentation": instrumentationMode(cfg),
		"valid":                     measurementValid,
	}
	content, _ := json.MarshalIndent(summary, "", "  ")
	return os.WriteFile(filepath.Join(cfg.outputDir, "reference-summary.json"), append(content, '\n'), 0o600)
}

func instrumentationMode(cfg config) string {
	if cfg.instrumentation == "" {
		return "on"
	}
	return cfg.instrumentation
}

func http2ExpectedConnectionGenerations(cfg config, expected int) int {
	if cfg.protocol != "h2" {
		return 1
	}
	totalExpected := expected + cfg.rps*cfg.warmupSeconds
	return max(1, (totalExpected+cfg.connections*10_000-1)/(cfg.connections*10_000))
}

func mergeBin(target, source *secondBin) {
	target.Scheduled += source.Scheduled
	target.Dispatched += source.Dispatched
	target.Assigned += source.Assigned
	target.Completed += source.Completed
	target.Catchup += source.Catchup
	target.NewConnections += source.NewConnections
	target.ReusedConnections += source.ReusedConnections
	target.Errors += source.Errors
	target.DispatchLateness = append(target.DispatchLateness, source.DispatchLateness...)
	target.AssignmentLateness = append(target.AssignmentLateness, source.AssignmentLateness...)
	target.Connect = append(target.Connect, source.Connect...)
	target.Write = append(target.Write, source.Write...)
	target.TTFB = append(target.TTFB, source.TTFB...)
	target.Total = append(target.Total, source.Total...)
	target.ScheduledTotal = append(target.ScheduledTotal, source.ScheduledTotal...)
	for status, count := range source.Statuses {
		target.Statuses[status] += count
	}
}

func stats(values []int64) map[string]int64 {
	sort.Slice(values, func(i, j int) bool { return values[i] < values[j] })
	return map[string]int64{"count": int64(len(values)), "p50": quantile(values, .5), "p95": quantile(values, .95), "p99": quantile(values, .99), "max": quantile(values, 1)}
}

func fixedLatencyHistogram(values []int64, timeoutSeconds int) map[string]any {
	const bucketWidthNanoseconds int64 = int64(time.Millisecond)
	maxNanoseconds := int64(time.Duration(timeoutSeconds+1) * time.Second)
	counts := make([]uint64, maxNanoseconds/bucketWidthNanoseconds+1)
	var overflow uint64
	for _, value := range values {
		value = max(0, value)
		index := (value + bucketWidthNanoseconds - 1) / bucketWidthNanoseconds
		if index >= int64(len(counts)) {
			index = int64(len(counts) - 1)
			overflow++
		}
		counts[index]++
	}
	return map[string]any{
		"method": "fixed-width-upper-bound", "unit": "nanoseconds", "bucket_width_nanoseconds": bucketWidthNanoseconds,
		"max_nanoseconds": maxNanoseconds, "counts": counts, "count": len(values), "overflow_count": overflow,
		"p50_upper_nanoseconds": histogramQuantileUpper(counts, bucketWidthNanoseconds, .50),
		"p95_upper_nanoseconds": histogramQuantileUpper(counts, bucketWidthNanoseconds, .95),
		"p99_upper_nanoseconds": histogramQuantileUpper(counts, bucketWidthNanoseconds, .99),
	}
}

func histogramQuantileUpper(counts []uint64, bucketWidthNanoseconds int64, fraction float64) int64 {
	var count uint64
	for _, value := range counts {
		count += value
	}
	if count == 0 {
		return 0
	}
	target := uint64(math.Ceil(float64(count) * fraction))
	var cumulative uint64
	for index, value := range counts {
		cumulative += value
		if cumulative >= target {
			return int64(index) * bucketWidthNanoseconds
		}
	}
	return int64(len(counts)-1) * bucketWidthNanoseconds
}

func quantile(values []int64, fraction float64) int64 {
	if len(values) == 0 {
		return 0
	}
	return values[min(len(values)-1, max(0, int(math.Ceil(float64(len(values))*fraction))-1))]
}

func readPayloads(path string) ([][]byte, string, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, "", err
	}
	digest := sha256.Sum256(content)
	lines := strings.Split(strings.TrimSpace(string(content)), "\n")
	payloads := make([][]byte, 0, len(lines))
	for _, line := range lines {
		if !json.Valid([]byte(line)) {
			return nil, "", fmt.Errorf("payload pool contains invalid JSON")
		}
		payloads = append(payloads, []byte(line))
	}
	if len(payloads) == 0 {
		return nil, "", errors.New("payload pool is empty")
	}
	return payloads, hex.EncodeToString(digest[:]), nil
}

func parseFlags() config {
	cfg := config{instrumentation: "on"}
	flag.StringVar(&cfg.connectURL, "connect-url", "", "HTTP URL used for the TCP destination")
	flag.StringVar(&cfg.hostHeader, "host-header", "", "HTTP Host header")
	flag.StringVar(&cfg.path, "path", "/__collector_control", "request path")
	flag.StringVar(&cfg.payloadPath, "payload-pool", "", "NDJSON payload pool")
	flag.StringVar(&cfg.outputDir, "output-dir", "", "output directory")
	flag.StringVar(&cfg.stage, "stage", "stage", "stage label")
	flag.StringVar(&cfg.repetition, "repetition", "1", "repetition")
	flag.StringVar(&cfg.node, "node", "node-01", "node")
	flag.StringVar(&cfg.process, "process", "process-01", "process")
	flag.StringVar(&cfg.protocol, "protocol", "h1", "wire protocol: h1, h2, or h3")
	flag.BoolVar(&cfg.traceAll, "trace-all", false, "record every request trace for correctness probes")
	flag.StringVar(&cfg.caCertPath, "ca-cert", "", "PEM CA certificate for verified TLS")
	flag.StringVar(&cfg.tlsServerName, "tls-server-name", "", "verified TLS server name and SNI")
	flag.IntVar(&cfg.rps, "rps", 0, "offered requests per second")
	flag.IntVar(&cfg.connections, "connections", 0, "persistent connections")
	flag.IntVar(&cfg.durationSeconds, "duration-seconds", 0, "duration")
	flag.IntVar(&cfg.warmupSeconds, "warmup-seconds", 0, "unscored persistent warm-up duration")
	flag.IntVar(&cfg.timeoutSeconds, "timeout-seconds", 10, "timeout")
	flag.IntVar(&cfg.streamsPerConnection, "streams-per-connection", 1, "concurrent HTTP/2 streams per physical connection")
	flag.Int64Var(&cfg.startUnix, "start-unix", 0, "start epoch")
	flag.Int64Var(&cfg.seed, "seed", 1, "deterministic seed")
	flag.Parse()
	if cfg.connectURL == "" || cfg.hostHeader == "" || cfg.payloadPath == "" || cfg.outputDir == "" || cfg.rps < 1 || cfg.connections < 1 || cfg.durationSeconds < 1 || cfg.warmupSeconds < 0 || cfg.timeoutSeconds < 1 || cfg.startUnix < 1 {
		fmt.Fprintln(os.Stderr, "invalid required driver flags")
		flag.Usage()
		os.Exit(2)
	}
	if !strings.HasPrefix(cfg.path, "/") || strings.ContainsAny(cfg.hostHeader, "\r\n") {
		fmt.Fprintln(os.Stderr, "invalid path or Host header")
		os.Exit(2)
	}
	if cfg.protocol != "h1" && cfg.protocol != "h2" && cfg.protocol != "h3" {
		fmt.Fprintln(os.Stderr, "protocol must be h1, h2, or h3")
		os.Exit(2)
	}
	if cfg.protocol == "h1" && cfg.streamsPerConnection != 1 {
		fmt.Fprintln(os.Stderr, "h1 requires streams-per-connection=1")
		os.Exit(2)
	}
	if (cfg.protocol == "h2" || cfg.protocol == "h3") && cfg.streamsPerConnection != 1 {
		fmt.Fprintln(os.Stderr, "independent h2/h3 devices require streams-per-connection=1")
		os.Exit(2)
	}
	target, targetErr := url.Parse(cfg.connectURL)
	if targetErr == nil && target.Scheme == "https" && (cfg.caCertPath == "" || cfg.tlsServerName == "") {
		fmt.Fprintln(os.Stderr, "https requires --ca-cert and --tls-server-name")
		os.Exit(2)
	}
	return cfg
}

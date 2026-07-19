package com.loopad.performance.phase4;

import static com.loopad.performance.phase4.ConsumerModels.BatchContext;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicLong;
import software.amazon.kinesis.retrieval.KinesisClientRecord;

final class MemoryGateMain {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final long REQUIRED_MEMORY_LIMIT_BYTES = 2L * 1024 * 1024 * 1024;
    private static final double MAX_PEAK_RATIO = 0.70;
    private static final int SHARDS_PER_TASK = RuntimeConfig.MAX_LEASES_PER_WORKER;
    private static final int WARMUP_WAVES = 2;
    private static final int WAVES = 6;
    private static final long SIMULATED_CLICKHOUSE_MILLIS = 100;
    private static final long REQUIRED_FLEET_RPS = 50_000;
    private static final int PINNED_MAX_PAYLOAD_BYTES = 1_518;

    private MemoryGateMain() {}

    static void run() throws Exception {
        long memoryLimit = readLong("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes");
        if (memoryLimit != REQUIRED_MEMORY_LIMIT_BYTES) {
            throw new IllegalStateException("Memory gate requires an exact 2 GiB cgroup limit.");
        }
        Instant now = Instant.now();
        List<BatchContext> batches = buildBatches(now);
        AtomicLong insertedBytes = new AtomicLong();
        BatchProcessor processor = new BatchProcessor(
                new EventTransformer(),
                (table, ndjson, timeout) -> {
                    byte[] retainedByHttpLayer = ndjson.clone();
                    insertedBytes.addAndGet(retainedByHttpLayer.length);
                    Thread.sleep(SIMULATED_CLICKHOUSE_MILLIS);
                    if (retainedByHttpLayer.length == 0) throw new IllegalStateException("EmptyInsert");
                },
                (batch, category, attempts) -> { throw new IllegalStateException("UnexpectedArchive"); },
                (name, count, timestamp) -> {},
                (event, shard, records, attempts, category) -> {},
                Thread::sleep,
                RuntimeConfig.MAX_CONCURRENT_BATCHES,
                () -> now,
                System::nanoTime,
                () -> 0.0);

        ExecutorService workers = Executors.newFixedThreadPool(SHARDS_PER_TASK, Thread.ofPlatform()
                .name("memory-gate-shard-", 0)
                .factory());
        long processed;
        long started;
        try {
            processWaves(workers, processor, batches, WARMUP_WAVES);
            insertedBytes.set(0);
            started = System.nanoTime();
            processed = processWaves(workers, processor, batches, WAVES);
        } finally {
            workers.shutdownNow();
        }
        long elapsedNanos = System.nanoTime() - started;
        long peak = readLong("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes");
        long current = readLong("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes");
        long heapUsed = Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory();
        double taskRps = processed / (elapsedNanos / 1_000_000_000.0);
        double fleetEquivalentRps = taskRps * 2.0;
        double peakRatio = (double) peak / memoryLimit;
        boolean passed = peakRatio < MAX_PEAK_RATIO && fleetEquivalentRps >= REQUIRED_FLEET_RPS;

        ObjectNode result = MAPPER.createObjectNode();
        result.put("schemaVersion", 1);
        result.put("status", passed ? "passed" : "failed");
        result.put("runtime", "native-java-kcl-3.4.3");
        result.put("shardsPerTask", SHARDS_PER_TASK);
        result.put("maxRecords", RuntimeConfig.MAX_RECORDS);
        result.put("maxPendingProcessRecordsInput", RuntimeConfig.MAX_PENDING_PROCESS_RECORDS_INPUT);
        result.put("maxConcurrentBatches", RuntimeConfig.MAX_CONCURRENT_BATCHES);
        result.put("warmupWaves", WARMUP_WAVES);
        result.put("waves", WAVES);
        result.put("processedRecords", processed);
        result.put("fixturePayloadBytes", batches.getFirst().records().getFirst().data().remaining());
        result.put("simulatedClickHouseMillis", SIMULATED_CLICKHOUSE_MILLIS);
        result.put("taskRecordsPerSecond", Math.round(taskRps));
        result.put("twoTaskFleetEquivalentRecordsPerSecond", Math.round(fleetEquivalentRps));
        result.put("requiredFleetRecordsPerSecond", REQUIRED_FLEET_RPS);
        result.put("memoryLimitBytes", memoryLimit);
        result.put("peakCgroupBytes", peak);
        result.put("peakCgroupPercent", peakRatio * 100.0);
        result.put("requiredPeakBelowPercent", MAX_PEAK_RATIO * 100.0);
        result.put("currentCgroupBytes", current);
        result.put("heapUsedBytes", heapUsed);
        result.put("insertedNdjsonBytes", insertedBytes.get());
        System.out.println(MAPPER.writerWithDefaultPrettyPrinter().writeValueAsString(result));
        if (!passed) throw new IllegalStateException("Native Java KCL memory/throughput gate failed.");
    }

    private static long processWaves(
            ExecutorService workers,
            BatchProcessor processor,
            List<BatchContext> batches,
            int waveCount) throws Exception {
        long processed = 0;
        for (int wave = 0; wave < waveCount; wave += 1) {
            List<Callable<Integer>> calls = batches.stream()
                    .<Callable<Integer>>map(batch -> () -> {
                        processor.process(batch);
                        return batch.records().size();
                    })
                    .toList();
            for (Future<Integer> result : workers.invokeAll(calls)) processed += result.get();
        }
        return processed;
    }

    private static List<BatchContext> buildBatches(Instant now) {
        List<BatchContext> batches = new ArrayList<>(SHARDS_PER_TASK);
        AtomicLong sequence = new AtomicLong(1);
        for (int shard = 0; shard < SHARDS_PER_TASK; shard += 1) {
            List<KinesisClientRecord> records = new ArrayList<>(RuntimeConfig.MAX_RECORDS);
            for (int record = 0; record < RuntimeConfig.MAX_RECORDS; record += 1) {
                long id = sequence.getAndIncrement();
                byte[] payload = payload(id, now);
                records.add(KinesisClientRecord.builder()
                        .sequenceNumber(Long.toString(id))
                        .partitionKey("partition-" + (id % 4_096))
                        .approximateArrivalTimestamp(now)
                        .data(ByteBuffer.wrap(payload))
                        .build());
            }
            batches.add(new BatchContext(
                    "arn:aws:kinesis:ap-northeast-2:000000000000:stream/phase4-memory-gate",
                    "shardId-" + String.format("%012d", shard),
                    0L,
                    records));
        }
        return List.copyOf(batches);
    }

    private static byte[] payload(long id, Instant now) {
        String timestamp = DateTimeFormatter.ISO_INSTANT.format(now.atOffset(ZoneOffset.UTC));
        String prefix = "{\"project_id\":\"memory-gate\",\"write_key\":\"perf_public_write_key\","
                + "\"schema_version\":\"hotel_rec_promo.v1\",\"event_id\":\"evt_" + id + "\","
                + "\"event_name\":\"hotel_recommendation_impression\",\"event_time\":\"" + timestamp + "\","
                + "\"source\":\"java-memory-gate\",\"user_id\":\"user_" + id + "\",\"session_id\":null,"
                + "\"properties_json\":\"{\\\"padding\\\":\\\"";
        String suffix = "\\\"}\","
                + "\"producer_sent_at\":\"" + timestamp + "\",\"run_id\":\"run_local_java_memory_gate\"}";
        int paddingBytes = PINNED_MAX_PAYLOAD_BYTES
                - prefix.getBytes(StandardCharsets.UTF_8).length
                - suffix.getBytes(StandardCharsets.UTF_8).length;
        if (paddingBytes < 0) throw new IllegalStateException("MemoryGateFixtureTooLarge");
        byte[] payload = (prefix + "x".repeat(paddingBytes) + suffix).getBytes(StandardCharsets.UTF_8);
        if (payload.length != PINNED_MAX_PAYLOAD_BYTES) {
            throw new IllegalStateException("MemoryGateFixtureSizeMismatch");
        }
        return payload;
    }

    private static long readLong(String v2, String v1) throws Exception {
        Path selected = Files.exists(Path.of(v2)) ? Path.of(v2) : Path.of(v1);
        String value = Files.readString(selected).trim();
        if ("max".equals(value)) throw new IllegalStateException("Cgroup memory is unlimited.");
        return Long.parseLong(value);
    }
}

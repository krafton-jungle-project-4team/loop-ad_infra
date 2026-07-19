package com.loopad.performance.phase4;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.HashMap;
import java.util.Map;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import software.amazon.kinesis.retrieval.KinesisClientRecord;

final class EmfTelemetry implements BatchProcessor.MetricEmitter, BatchProcessor.SummaryLogger, AutoCloseable {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final long[] VISIBILITY_BUCKETS_MS = {
        100, 250, 500, 1_000, 2_000, 5_000, 10_000, 30_000, 60_000
    };
    private final String namespace;
    private final String runId;
    private final String metadataUri;
    private final HttpClient httpClient;
    private final ScheduledExecutorService sampler;
    private volatile String taskArn;

    EmfTelemetry(String namespace, String runId, String metadataUri, HttpClient httpClient) {
        this.namespace = namespace;
        this.runId = runId;
        this.metadataUri = metadataUri;
        this.httpClient = httpClient;
        this.sampler = Executors.newSingleThreadScheduledExecutor(Thread.ofPlatform()
                .name("host-memory-telemetry")
                .daemon(true)
                .factory());
    }

    void startHostMemorySampling() {
        sampler.scheduleWithFixedDelay(this::sampleHostMemorySafely, 0, 60, TimeUnit.SECONDS);
    }

    @Override
    public void emit(String name, long count, Instant timestamp) {
        ObjectNode root = metricRoot(timestamp);
        ArrayNode dimensions = MAPPER.createArrayNode();
        dimensions.add(MAPPER.createArrayNode().add("RunId"));
        root.withObject("_aws").withArray("CloudWatchMetrics").add(metricDefinition(name, "Count", dimensions));
        root.put("RunId", runId);
        root.put(name, count);
        write(root);
    }

    @Override
    public void log(String event, String shardId, int inputRecords, int attempts, String errorCategory) {
        ObjectNode root = MAPPER.createObjectNode();
        root.put("runId", runId);
        root.put("event", event);
        if (shardId != null && !shardId.isEmpty()) root.put("shardId", shardId);
        root.put("inputRecords", inputRecords);
        root.put("attempts", attempts);
        if (errorCategory != null && !errorCategory.isEmpty()) root.put("errorCategory", errorCategory);
        write(root);
    }

    @Override
    public void logVisibility(String shardId, List<KinesisClientRecord> records, Instant completedAt) {
        long[] counts = new long[VISIBILITY_BUCKETS_MS.length + 1];
        long observed = 0;
        for (KinesisClientRecord record : records) {
            Instant arrivedAt = record.approximateArrivalTimestamp();
            if (arrivedAt == null) continue;
            long latency = Math.max(0, Duration.between(arrivedAt, completedAt).toMillis());
            int bucket = VISIBILITY_BUCKETS_MS.length;
            for (int index = 0; index < VISIBILITY_BUCKETS_MS.length; index += 1) {
                if (latency <= VISIBILITY_BUCKETS_MS[index]) {
                    bucket = index;
                    break;
                }
            }
            counts[bucket] += 1;
            observed += 1;
        }
        ObjectNode root = MAPPER.createObjectNode();
        root.put("runId", runId);
        root.put("event", "phase7_visibility_histogram");
        root.put("visibilityBasis", "kinesis_approximate_arrival_to_clickhouse_insert_completion");
        if (shardId != null && !shardId.isEmpty()) root.put("shardId", shardId);
        root.put("observedRecords", observed);
        for (int index = 0; index < VISIBILITY_BUCKETS_MS.length; index += 1) {
            root.put("latencyLe" + VISIBILITY_BUCKETS_MS[index] + "Ms", counts[index]);
        }
        root.put("latencyGt60000Ms", counts[VISIBILITY_BUCKETS_MS.length]);
        write(root);
    }

    private void sampleHostMemorySafely() {
        try {
            Map<String, Long> memory = parseMeminfo(Files.readString(Path.of("/proc/meminfo")));
            long total = memory.getOrDefault("MemTotal", 0L);
            long available = memory.getOrDefault("MemAvailable", -1L);
            if (total <= 0 || available < 0 || available > total) throw new IOException("InvalidMeminfo");
            String resolvedTaskArn = taskArn == null ? fetchTaskArn() : taskArn;
            taskArn = resolvedTaskArn;
            double utilization = (double) (total - available) * 100.0 / total;

            ObjectNode root = metricRoot(Instant.now());
            ArrayNode dimensions = MAPPER.createArrayNode();
            dimensions.add(MAPPER.createArrayNode().add("RunId").add("TaskArn"));
            root.withObject("_aws").withArray("CloudWatchMetrics")
                    .add(metricDefinition("HostMemoryUtilization", "Percent", dimensions));
            root.put("RunId", runId);
            root.put("TaskArn", resolvedTaskArn);
            root.put("HostMemoryUtilization", utilization);
            write(root);
        } catch (Exception ignored) {
            taskArn = null;
            log("phase4_host_memory_telemetry_error", "", 0, 0, "");
        }
    }

    private String fetchTaskArn() throws Exception {
        if (!metadataUri.matches("http://169\\.254\\.170\\.2/v4/[A-Za-z0-9-]+")) {
            throw new IllegalArgumentException("HostMemoryTelemetryError");
        }
        HttpRequest request = HttpRequest.newBuilder(URI.create(metadataUri + "/task"))
                .timeout(Duration.ofSeconds(5)).GET().build();
        HttpResponse<byte[]> response = httpClient.send(request, HttpResponse.BodyHandlers.ofByteArray());
        if (response.statusCode() != 200) throw new IOException("MetadataHttpError");
        String arn = MAPPER.readTree(response.body()).path("TaskARN").asText("");
        if (!arn.matches("arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task/.+")) {
            throw new IOException("MetadataDocumentError");
        }
        return arn;
    }

    private ObjectNode metricRoot(Instant timestamp) {
        ObjectNode root = MAPPER.createObjectNode();
        ObjectNode aws = root.putObject("_aws");
        aws.put("Timestamp", timestamp.toEpochMilli());
        aws.putArray("CloudWatchMetrics");
        return root;
    }

    private ObjectNode metricDefinition(String name, String unit, ArrayNode dimensions) {
        ObjectNode definition = MAPPER.createObjectNode();
        definition.put("Namespace", namespace);
        definition.set("Dimensions", dimensions);
        definition.putArray("Metrics").addObject().put("Name", name).put("Unit", unit);
        return definition;
    }

    private static Map<String, Long> parseMeminfo(String meminfo) {
        Map<String, Long> values = new HashMap<>();
        for (String line : meminfo.lines().toList()) {
            String[] fields = line.trim().split("\\s+");
            if (fields.length == 3 && fields[0].endsWith(":") && "kB".equals(fields[2])) {
                try {
                    values.put(fields[0].substring(0, fields[0].length() - 1), Long.parseLong(fields[1]));
                } catch (NumberFormatException ignored) {
                    // Invalid fields are rejected when required values are read.
                }
            }
        }
        return values;
    }

    private static synchronized void write(ObjectNode value) {
        try {
            System.err.println(MAPPER.writeValueAsString(value));
        } catch (IOException ignored) {
            System.err.println("{\"event\":\"phase4_telemetry_serialization_error\"}");
        }
    }

    @Override
    public void close() {
        sampler.shutdownNow();
    }
}

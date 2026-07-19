package com.loopad.performance.phase4;

import static com.loopad.performance.phase4.ConsumerModels.BatchContext;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.nio.ByteBuffer;
import java.time.Instant;
import java.util.Base64;
import java.util.concurrent.atomic.AtomicLong;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.s3.model.ServerSideEncryption;
import software.amazon.kinesis.retrieval.KinesisClientRecord;

final class S3FailureArchiver implements BatchProcessor.FailureArchiver {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final AtomicLong KEY_SEQUENCE = new AtomicLong();
    private final S3Client s3;
    private final String bucket;
    private final String prefix;

    S3FailureArchiver(S3Client s3, String bucket, String prefix) {
        this.s3 = s3;
        this.bucket = bucket;
        this.prefix = prefix;
    }

    @Override
    public void archive(BatchContext batch, String errorCategory, int attempts) throws Exception {
        Instant now = Instant.now();
        ObjectNode archive = MAPPER.createObjectNode();
        archive.put("schemaVersion", "loopad.phase4.failure.v1");
        archive.put("archivedAt", now.toString());
        archive.put("streamArn", batch.streamArn());
        archive.put("shardId", batch.shardId());
        if (batch.millisBehindLatest() == null) archive.putNull("millisBehindLatest");
        else archive.put("millisBehindLatest", batch.millisBehindLatest());
        archive.put("errorCategory", errorCategory);
        archive.put("attempts", attempts);
        ArrayNode records = archive.putArray("records");
        for (KinesisClientRecord record : batch.records()) {
            ObjectNode node = records.addObject();
            node.put("action", "record");
            node.put("data", Base64.getEncoder().encodeToString(bytes(record.data())));
            node.put("partitionKey", record.partitionKey() == null ? "" : record.partitionKey());
            node.put("sequenceNumber", record.sequenceNumber());
            if (record.approximateArrivalTimestamp() == null) node.putNull("approximateArrivalTimestamp");
            else node.put("approximateArrivalTimestamp", record.approximateArrivalTimestamp().toEpochMilli());
            node.put("subSequenceNumber", record.subSequenceNumber());
        }
        byte[] body = MAPPER.writeValueAsBytes(archive);
        String first = batch.records().isEmpty() ? "empty" : batch.records().getFirst().sequenceNumber();
        String last = batch.records().isEmpty() ? "empty" : batch.records().getLast().sequenceNumber();
        String key = prefix + now.toEpochMilli() + "-" + KEY_SEQUENCE.incrementAndGet() + "-"
                + safe(batch.shardId()) + "-" + first + "-" + last + ".json";
        s3.putObject(PutObjectRequest.builder()
                        .bucket(bucket)
                        .key(key)
                        .contentType("application/json")
                        .serverSideEncryption(ServerSideEncryption.AES256)
                        .build(),
                RequestBody.fromBytes(body));
    }

    private static byte[] bytes(ByteBuffer source) {
        ByteBuffer duplicate = source.asReadOnlyBuffer();
        byte[] bytes = new byte[duplicate.remaining()];
        duplicate.get(bytes);
        return bytes;
    }

    private static String safe(String value) {
        String sanitized = value.replaceAll("[^A-Za-z0-9_.-]", "_");
        return sanitized.substring(0, Math.min(128, sanitized.length()));
    }
}

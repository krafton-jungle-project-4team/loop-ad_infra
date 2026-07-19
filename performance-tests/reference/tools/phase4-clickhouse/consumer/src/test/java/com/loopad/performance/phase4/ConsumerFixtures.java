package com.loopad.performance.phase4;

import static com.loopad.performance.phase4.ConsumerModels.BatchContext;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import software.amazon.kinesis.retrieval.KinesisClientRecord;

final class ConsumerFixtures {
    static final Instant NOW = Instant.parse("2026-07-16T12:00:00Z");
    static final String STREAM_ARN = "arn:aws:kinesis:ap-northeast-2:123456789012:stream/phase4";
    static final String SHARD_ID = "shardId-000000000000";

    private ConsumerFixtures() {}

    static String payload(String eventId, String eventTime) {
        return "{\"project_id\":\"project-1\",\"write_key\":\"secret-write-key\","
                + "\"schema_version\":\"hotel_rec_promo.v1\",\"event_id\":\"" + eventId + "\","
                + "\"event_name\":\"promotion_viewed\",\"event_time\":\"" + eventTime + "\","
                + "\"source\":\"java-test\",\"user_id\":\"user-1\",\"session_id\":null,"
                + "\"properties_json\":\" { \\\"unchanged\\\" : true } \","
                + "\"producer_sent_at\":\"2026-07-16T11:59:59.100Z\","
                + "\"run_id\":\"run_20260716_120000_phase4_clickhouse_ecs\"}";
    }

    static KinesisClientRecord record(String payload, String sequence) {
        return KinesisClientRecord.builder()
                .sequenceNumber(sequence)
                .partitionKey("partition-" + sequence)
                .approximateArrivalTimestamp(NOW)
                .data(ByteBuffer.wrap(payload.getBytes(StandardCharsets.UTF_8)))
                .build();
    }

    static BatchContext batch(List<KinesisClientRecord> records) {
        return new BatchContext(STREAM_ARN, SHARD_ID, 0L, records);
    }
}

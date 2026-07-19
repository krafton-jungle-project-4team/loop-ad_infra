package com.loopad.performance.phase4;

import static com.loopad.performance.phase4.ConsumerModels.BatchContext;
import static com.loopad.performance.phase4.ConsumerModels.BatchPlan;

import com.fasterxml.jackson.core.StreamReadConstraints;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.time.DateTimeException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.Base64;
import java.util.regex.Pattern;
import software.amazon.kinesis.retrieval.KinesisClientRecord;

final class EventTransformer {
    private static final String SUPPORTED_SCHEMA_VERSION = "hotel_rec_promo.v1";
    private static final int LATE_EVENT_DAYS = 7;
    private static final int ERROR_MESSAGE_MAX_LENGTH = 256;
    private static final Pattern SEQUENCE_NUMBER = Pattern.compile("[0-9]+");
    private static final DateTimeFormatter CLICKHOUSE_TIMESTAMP =
            DateTimeFormatter.ofPattern("uuuu-MM-dd HH:mm:ss.SSS").withZone(ZoneOffset.UTC);

    private final ObjectMapper mapper;

    EventTransformer() {
        mapper = new ObjectMapper();
        mapper.getFactory().setStreamReadConstraints(StreamReadConstraints.builder()
                .maxNestingDepth(100)
                .maxStringLength(1_048_576)
                .maxNumberLength(256)
                .build());
    }

    BatchPlan plan(BatchContext batch, Instant receivedAt) {
        int initialSize = Math.max(1_024, Math.min(4 * 1_024 * 1_024, batch.records().size() * 2_048));
        ByteArrayOutputStream events = new ByteArrayOutputStream(initialSize);
        ByteArrayOutputStream rawEvents = new ByteArrayOutputStream(Math.max(1_024, initialSize / 16));
        int eventRows = 0;
        int rawRows = 0;
        int lateRows = 0;

        for (KinesisClientRecord record : batch.records()) {
            try {
                ObjectNode eventRow = transformValid(batch, record, record.data(), receivedAt);
                if (eventRow == null) {
                    lateRows += 1;
                } else {
                    append(events, eventRow);
                    eventRows += 1;
                }
            } catch (RecordTransformException error) {
                append(rawEvents, rawRow(batch, record, receivedAt, error));
                rawRows += 1;
            }
        }
        return new BatchPlan(events.toByteArray(), rawEvents.toByteArray(), eventRows, rawRows, lateRows);
    }

    private ObjectNode transformValid(
            BatchContext batch,
            KinesisClientRecord record,
            ByteBuffer payloadBytes,
            Instant receivedAt) {
        JsonNode payload;
        try {
            payload = readTree(payloadBytes);
        } catch (IOException error) {
            throw new RecordTransformException("invalid_json", "JSON parsing failed.");
        }
        if (payload == null || !payload.isObject()) {
            throw new RecordTransformException("invalid_envelope", "Payload must be a JSON object.");
        }

        String schemaVersion = requiredString(payload, "schema_version", true);
        if (!SUPPORTED_SCHEMA_VERSION.equals(schemaVersion)) {
            throw new RecordTransformException("unsupported_schema_version", "The schema version is not supported.");
        }
        Instant eventTime = timestamp(requiredString(payload, "event_time", true), "invalid_event_time");
        Instant boundary = receivedAt.atZone(ZoneOffset.UTC).toLocalDate()
                .minusDays(LATE_EVENT_DAYS)
                .atStartOfDay(ZoneOffset.UTC)
                .toInstant();
        if (eventTime.isBefore(boundary)) return null;

        String sequenceNumber = sequenceNumber(record);
        ObjectNode row = mapper.createObjectNode();
        row.put("project_id", requiredString(payload, "project_id", true));
        row.put("write_key", requiredString(payload, "write_key", true));
        row.put("schema_version", schemaVersion);
        row.put("event_id", requiredString(payload, "event_id", true));
        row.put("event_name", requiredString(payload, "event_name", true));
        row.put("event_time", CLICKHOUSE_TIMESTAMP.format(eventTime));
        row.put("source", requiredString(payload, "source", true));
        putOptionalString(row, payload, "user_id");
        putOptionalString(row, payload, "session_id");
        row.put("properties_json", requiredString(payload, "properties_json", false));
        putOptionalTimestamp(row, payload, "producer_sent_at");
        putOptionalString(row, payload, "run_id");
        row.put("kinesis_shard_id", batch.shardId());
        row.put("kinesis_sequence_number", sequenceNumber);
        return row;
    }

    private ObjectNode rawRow(
            BatchContext batch,
            KinesisClientRecord record,
            Instant receivedAt,
            RecordTransformException error) {
        byte[] payload = bytes(record.data());
        ObjectNode row = mapper.createObjectNode();
        row.put("stream_arn", batch.streamArn());
        row.put("shard_id", batch.shardId());
        row.put("sequence_number", sequenceNumber(record));
        row.put("partition_key", record.partitionKey() == null ? "" : record.partitionKey());
        Instant arrival = record.approximateArrivalTimestamp() == null
                ? receivedAt
                : record.approximateArrivalTimestamp();
        row.put("approximate_arrival_at", CLICKHOUSE_TIMESTAMP.format(arrival));
        row.put("raw_payload_base64", Base64.getEncoder().encodeToString(payload));
        row.put("error_code", error.code());
        row.put("error_message", truncate(error.getMessage(), ERROR_MESSAGE_MAX_LENGTH));
        row.put("lambda_received_at", CLICKHOUSE_TIMESTAMP.format(receivedAt));
        String runId = extractRunId(payload);
        if (runId == null) row.putNull("run_id");
        else row.put("run_id", runId);
        return row;
    }

    private String extractRunId(byte[] payload) {
        try {
            JsonNode parsed = mapper.readTree(payload);
            JsonNode value = parsed == null ? null : parsed.get("run_id");
            return value != null && value.isTextual() ? value.textValue() : null;
        } catch (IOException ignored) {
            return null;
        }
    }

    private JsonNode readTree(ByteBuffer source) throws IOException {
        ByteBuffer duplicate = source.duplicate();
        if (duplicate.hasArray()) {
            return mapper.readTree(
                    duplicate.array(),
                    duplicate.arrayOffset() + duplicate.position(),
                    duplicate.remaining());
        }
        return mapper.readTree(bytes(duplicate));
    }

    private void append(ByteArrayOutputStream output, ObjectNode row) {
        try {
            mapper.writeValue(output, row);
            output.write('\n');
        } catch (IOException error) {
            throw new IllegalStateException("NdjsonSerializationError", error);
        }
    }

    private static String requiredString(JsonNode payload, String field, boolean nonEmpty) {
        JsonNode value = payload.get(field);
        if (value == null || !value.isTextual() || (nonEmpty && value.textValue().isEmpty())) {
            throw new RecordTransformException("invalid_required_field", "Required field is invalid.");
        }
        return value.textValue();
    }

    private static void putOptionalString(ObjectNode row, JsonNode payload, String field) {
        JsonNode value = payload.get(field);
        if (value == null || value.isNull()) {
            row.putNull(field);
        } else if (value.isTextual()) {
            row.put(field, value.textValue());
        } else {
            throw new RecordTransformException("invalid_optional_field", "Optional field is invalid.");
        }
    }

    private static void putOptionalTimestamp(ObjectNode row, JsonNode payload, String field) {
        JsonNode value = payload.get(field);
        if (value == null || value.isNull()) {
            row.putNull(field);
        } else if (value.isTextual()) {
            row.put(field, CLICKHOUSE_TIMESTAMP.format(timestamp(value.textValue(), "invalid_producer_sent_at")));
        } else {
            throw new RecordTransformException("invalid_producer_sent_at", "producer_sent_at is invalid.");
        }
    }

    private static Instant timestamp(String value, String errorCode) {
        if (!value.matches(".*(?:Z|[+-][0-9]{2}:[0-9]{2})$")) {
            throw new RecordTransformException(errorCode, "Timestamp must include a UTC offset.");
        }
        try {
            return OffsetDateTime.parse(value).toInstant();
        } catch (DateTimeException error) {
            throw new RecordTransformException(errorCode, "Timestamp parsing failed.");
        }
    }

    private static String sequenceNumber(KinesisClientRecord record) {
        String value = record.sequenceNumber();
        if (value == null || !SEQUENCE_NUMBER.matcher(value).matches()) {
            throw new IllegalArgumentException("InvalidSequenceNumber");
        }
        BigInteger parsed = new BigInteger(value);
        if (parsed.signum() < 0 || parsed.bitLength() > 256) {
            throw new IllegalArgumentException("InvalidSequenceNumber");
        }
        return value;
    }

    private static byte[] bytes(ByteBuffer source) {
        ByteBuffer duplicate = source.duplicate();
        byte[] value = new byte[duplicate.remaining()];
        duplicate.get(value);
        return value;
    }

    private static String truncate(String value, int maxLength) {
        return value.length() <= maxLength ? value : value.substring(0, maxLength);
    }

    static final class RecordTransformException extends RuntimeException {
        private final String code;

        RecordTransformException(String code, String message) {
            super(message);
            this.code = code;
        }

        String code() {
            return code;
        }
    }
}

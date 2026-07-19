package com.loopad.performance.phase4;

import static com.loopad.performance.phase4.ConsumerFixtures.NOW;
import static com.loopad.performance.phase4.ConsumerFixtures.batch;
import static com.loopad.performance.phase4.ConsumerFixtures.payload;
import static com.loopad.performance.phase4.ConsumerFixtures.record;
import static org.junit.jupiter.api.Assertions.assertEquals;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.charset.StandardCharsets;
import java.util.List;
import org.junit.jupiter.api.Test;

class EventTransformerTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void preservesPropertiesRoutesInvalidAndDropsLate() throws Exception {
        var plan = new EventTransformer().plan(batch(List.of(
                record(payload("valid", "2026-07-16T11:59:59Z"), "10"),
                record("{", "11"),
                record(payload("late", "2026-07-08T23:59:59Z"), "12"))), NOW);

        assertEquals(1, plan.eventRows());
        assertEquals(1, plan.rawEventRows());
        assertEquals(1, plan.lateEventCount());
        JsonNode event = first(plan.eventsNdjson());
        assertEquals("valid", event.path("event_id").asText());
        assertEquals(" { \"unchanged\" : true } ", event.path("properties_json").asText());
        assertEquals("shardId-000000000000", event.path("kinesis_shard_id").asText());
        assertEquals("10", event.path("kinesis_sequence_number").asText());
        JsonNode raw = first(plan.rawEventsNdjson());
        assertEquals("invalid_json", raw.path("error_code").asText());
        assertEquals("ew==", raw.path("raw_payload_base64").asText());
    }

    private static JsonNode first(byte[] ndjson) throws Exception {
        return MAPPER.readTree(new String(ndjson, StandardCharsets.UTF_8).trim());
    }
}

package com.loopad.performance.phase4;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.net.URI;
import java.net.URLDecoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import org.junit.jupiter.api.Test;

class ClickHouseWriterTest {
    @Test
    void buildsFixedAsyncInsertShapeWithoutNetwork() {
        byte[] ndjson = "{\"event_id\":\"one\"}\n".getBytes(StandardCharsets.UTF_8);
        ClickHouseWriter writer = new ClickHouseWriter(
                HttpClient.newHttpClient(),
                URI.create("http://127.0.0.1:18123"),
                "loopad",
                new ClickHouseWriter.Credentials("ingest", "password"),
                false);
        HttpRequest request = writer.request("events", ndjson, Duration.ofSeconds(2));
        String query = URLDecoder.decode(request.uri().getRawQuery(), StandardCharsets.UTF_8);

        assertEquals(ndjson.length, request.bodyPublisher().orElseThrow().contentLength());
        assertEquals("ingest", request.headers().firstValue("X-ClickHouse-User").orElseThrow());
        assertTrue(query.contains("INSERT INTO loopad.events"));
        assertTrue(query.contains("async_insert=1"));
        assertTrue(query.contains("wait_for_async_insert=1"));
        assertTrue(query.contains("async_insert_deduplicate=0"));
    }

    @Test
    void rejectsPublicOrTlsClickHouseEndpoints() {
        assertThrows(IllegalArgumentException.class,
                () -> ClickHouseWriter.requirePrivateHttp(URI.create("https://10.0.0.1:8123"), false));
        assertThrows(IllegalArgumentException.class,
                () -> ClickHouseWriter.requirePrivateHttp(URI.create("http://8.8.8.8:8123"), false));
    }
}

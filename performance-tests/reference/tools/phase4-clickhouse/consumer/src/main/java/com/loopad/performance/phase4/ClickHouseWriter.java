package com.loopad.performance.phase4;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;
import java.util.Objects;

final class ClickHouseWriter implements BatchProcessor.RowWriter {
    record Credentials(String username, String password) {
        Credentials {
            if (username == null || username.isEmpty() || password == null) {
                throw new IllegalArgumentException("SecretConfigurationError");
            }
        }
    }

    private static final Map<String, String> INSERTS = Map.of(
            "events", "INSERT INTO %s.events (project_id,write_key,schema_version,event_id,event_name,event_time,source,user_id,session_id,properties_json,producer_sent_at,run_id,kinesis_shard_id,kinesis_sequence_number) FORMAT JSONEachRow",
            "raw_events", "INSERT INTO %s.raw_events (stream_arn,shard_id,sequence_number,partition_key,approximate_arrival_at,raw_payload_base64,error_code,error_message,lambda_received_at,run_id) FORMAT JSONEachRow");
    private static final String SETTINGS = String.join("&",
            "async_insert=1",
            "wait_for_async_insert=1",
            "async_insert_max_data_size=16777216",
            "async_insert_use_adaptive_busy_timeout=1",
            "async_insert_busy_timeout_min_ms=50",
            "async_insert_busy_timeout_max_ms=300",
            "async_insert_deduplicate=0",
            "input_format_json_read_numbers_as_strings=1");

    private final HttpClient client;
    private final URI baseUrl;
    private final String database;
    private final Credentials credentials;

    ClickHouseWriter(HttpClient client, URI baseUrl, String database, Credentials credentials, boolean allowLocalDns) {
        this.client = Objects.requireNonNull(client);
        this.baseUrl = Objects.requireNonNull(baseUrl);
        this.database = Objects.requireNonNull(database);
        this.credentials = Objects.requireNonNull(credentials);
        requirePrivateHttp(baseUrl, allowLocalDns);
        if (!database.matches("[A-Za-z_][A-Za-z0-9_]*")) {
            throw new IllegalArgumentException("RuntimeConfigurationError");
        }
    }

    @Override
    public void insert(String table, byte[] ndjson, Duration timeout) throws Exception {
        HttpRequest request = request(table, ndjson, timeout);
        HttpResponse<Void> response = client.send(request, HttpResponse.BodyHandlers.discarding());
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new ClickHouseInsertException(response.statusCode());
        }
    }

    HttpRequest request(String table, byte[] ndjson, Duration timeout) {
        String insert = INSERTS.get(table);
        if (insert == null) throw new IllegalArgumentException("UnsupportedClickHouseTable");
        String query = String.format(insert, database);
        String separator = baseUrl.toString().contains("?") ? "&" : "?";
        URI target = URI.create(baseUrl + separator + "query="
                + URLEncoder.encode(query, StandardCharsets.UTF_8) + "&" + SETTINGS);
        return HttpRequest.newBuilder(target)
                .timeout(timeout)
                .header("Content-Type", "application/x-ndjson; charset=utf-8")
                .header("X-ClickHouse-User", credentials.username())
                .header("X-ClickHouse-Key", credentials.password())
                .POST(HttpRequest.BodyPublishers.ofByteArray(ndjson))
                .build();
    }

    static void requirePrivateHttp(URI uri, boolean allowLocalDns) {
        String host = uri.getHost();
        if (!"http".equals(uri.getScheme()) || host == null || uri.getUserInfo() != null || uri.getFragment() != null) {
            throw new IllegalArgumentException("ClickHouseEndpointMustBePrivateHttp");
        }
        if (allowLocalDns && "clickhouse".equals(host)) return;
        try {
            InetAddress address = InetAddress.getByName(host);
            if (!(address instanceof Inet4Address) || !isPrivateIpv4(address.getAddress())) {
                throw new IllegalArgumentException("ClickHouseEndpointMustBePrivateHttp");
            }
        } catch (Exception error) {
            if (error instanceof IllegalArgumentException invalid) throw invalid;
            throw new IllegalArgumentException("ClickHouseEndpointMustBePrivateHttp");
        }
    }

    private static boolean isPrivateIpv4(byte[] bytes) {
        int first = Byte.toUnsignedInt(bytes[0]);
        int second = Byte.toUnsignedInt(bytes[1]);
        return first == 10
                || first == 127
                || (first == 172 && second >= 16 && second <= 31)
                || (first == 192 && second == 168);
    }

    static final class ClickHouseInsertException extends Exception {
        private final int statusCode;

        ClickHouseInsertException(int statusCode) {
            super("ClickHouse insert failed.");
            this.statusCode = statusCode;
        }

        int statusCode() {
            return statusCode;
        }
    }
}

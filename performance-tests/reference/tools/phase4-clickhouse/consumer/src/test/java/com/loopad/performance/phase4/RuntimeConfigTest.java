package com.loopad.performance.phase4;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.net.URI;
import java.util.HashMap;
import java.util.Map;
import org.junit.jupiter.api.Test;

class RuntimeConfigTest {
    @Test
    void productionModeRejectsAnyEndpointOverride() {
        Map<String, String> sdkOverride = validEnvironment();
        sdkOverride.put("AWS_ENDPOINT_URL_KINESIS", "http://localstack:4566");
        assertThrows(IllegalArgumentException.class, () -> RuntimeConfig.fromEnvironment(sdkOverride));

        Map<String, String> phase7Override = validEnvironment();
        phase7Override.put("PHASE7_KINESIS_ENDPOINT_URL", "http://localstack:4566");
        assertThrows(IllegalArgumentException.class, () -> RuntimeConfig.fromEnvironment(phase7Override));
    }

    @Test
    void localModeRequiresAllExplicitLoopbackEndpoints() {
        Map<String, String> environment = validEnvironment();
        environment.put("PHASE7_LOCAL_MODE", "true");
        assertThrows(IllegalArgumentException.class, () -> RuntimeConfig.fromEnvironment(environment));

        addLocalEndpoints(environment, "http://localstack:4566");
        RuntimeConfig config = RuntimeConfig.fromEnvironment(environment);
        assertTrue(config.localMode());
        assertEquals(URI.create("http://localstack:4566"), config.awsEndpoints().s3());
    }

    @Test
    void localModeRejectsExternalOrImplicitSdkEndpoints() {
        Map<String, String> externalEndpoint = validEnvironment();
        externalEndpoint.put("PHASE7_LOCAL_MODE", "true");
        addLocalEndpoints(externalEndpoint, "http://localstack:4566");
        externalEndpoint.put("PHASE7_S3_ENDPOINT_URL", "http://example.com:4566");
        assertThrows(IllegalArgumentException.class, () -> RuntimeConfig.fromEnvironment(externalEndpoint));

        Map<String, String> implicitOverride = validEnvironment();
        implicitOverride.put("PHASE7_LOCAL_MODE", "true");
        addLocalEndpoints(implicitOverride, "http://127.0.0.1:4566");
        implicitOverride.put("AWS_ENDPOINT_URL", "http://127.0.0.1:4566");
        assertThrows(IllegalArgumentException.class, () -> RuntimeConfig.fromEnvironment(implicitOverride));
    }

    @Test
    void productionModeRemainsDefault() {
        RuntimeConfig config = RuntimeConfig.fromEnvironment(validEnvironment());
        assertFalse(config.localMode());
    }

    private static void addLocalEndpoints(Map<String, String> environment, String endpoint) {
        environment.put("PHASE7_KINESIS_ENDPOINT_URL", endpoint);
        environment.put("PHASE7_DYNAMODB_ENDPOINT_URL", endpoint);
        environment.put("PHASE7_CLOUDWATCH_ENDPOINT_URL", endpoint);
        environment.put("PHASE7_SECRETSMANAGER_ENDPOINT_URL", endpoint);
        environment.put("PHASE7_S3_ENDPOINT_URL", endpoint);
        environment.put("CLICKHOUSE_HTTP_URL", "http://clickhouse:8123");
    }

    private static Map<String, String> validEnvironment() {
        return new HashMap<>(Map.ofEntries(
                Map.entry("AWS_REGION", "ap-northeast-2"),
                Map.entry("RUN_ID", "run_phase7_local"),
                Map.entry("METRIC_NAMESPACE", "LoopAd/Phase7"),
                Map.entry("KINESIS_STREAM_NAME", "phase7-local"),
                Map.entry("KINESIS_STREAM_ARN", "arn:aws:kinesis:ap-northeast-2:000000000000:stream/phase7-local"),
                Map.entry("KCL_APPLICATION_NAME", "phase7-local"),
                Map.entry("KCL_LEASE_TABLE_NAME", "phase7-local-leases"),
                Map.entry("KCL_WORKER_METRICS_TABLE_NAME", "phase7-local-workers"),
                Map.entry("KCL_COORDINATOR_STATE_TABLE_NAME", "phase7-local-coordinator"),
                Map.entry("CLICKHOUSE_DATABASE", "loopad"),
                Map.entry("CLICKHOUSE_HTTP_URL", "http://10.45.0.10:8123"),
                Map.entry("CLICKHOUSE_SECRET_ARN", "phase7-local-clickhouse"),
                Map.entry("FAILURE_BUCKET", "phase7-local-failures"),
                Map.entry("FAILURE_PREFIX", "failures/run_phase7_local/")));
    }
}

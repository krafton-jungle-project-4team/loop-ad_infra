package com.loopad.performance.phase4;

import java.net.URI;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.regex.Pattern;

record RuntimeConfig(
        String region,
        String runId,
        String metricNamespace,
        String streamName,
        String streamArn,
        String applicationName,
        String leaseTableName,
        String workerMetricsTableName,
        String coordinatorStateTableName,
        String clickHouseDatabase,
        URI clickHouseUrl,
        String clickHouseSecretArn,
        String failureBucket,
        String failurePrefix,
        String ecsMetadataUri,
        boolean localMode,
        AwsClientEndpoints awsEndpoints) {

    static final int MAX_RECORDS = 1_000;
    static final int MAX_PENDING_PROCESS_RECORDS_INPUT = 0;
    static final int MAX_LEASES_PER_WORKER = 60;
    static final int MAX_CONCURRENT_BATCHES = 10;
    static final long POLLING_IDLE_MILLIS = 200;
    static final long GRACEFUL_LEASE_HANDOFF_MILLIS = 120_000;

    private static final Pattern REGION = Pattern.compile("[a-z][a-z]-[a-z]+-[0-9]");
    private static final Pattern SIMPLE_NAME = Pattern.compile("[A-Za-z0-9_.-]+");
    private static final Pattern STREAM_ARN = Pattern.compile(
            "arn:aws:kinesis:[a-z0-9-]+:[0-9]{12}:stream/[A-Za-z0-9_.-]+");
    private static final Pattern IDENTIFIER = Pattern.compile("[A-Za-z_][A-Za-z0-9_]*");

    static RuntimeConfig fromEnvironment(Map<String, String> environment) {
        Objects.requireNonNull(environment, "environment");
        String region = required(environment, "AWS_REGION");
        String streamName = required(environment, "KINESIS_STREAM_NAME");
        String streamArn = required(environment, "KINESIS_STREAM_ARN");
        String applicationName = required(environment, "KCL_APPLICATION_NAME");
        String leaseTable = required(environment, "KCL_LEASE_TABLE_NAME");
        String workerMetricsTable = required(environment, "KCL_WORKER_METRICS_TABLE_NAME");
        String coordinatorStateTable = required(environment, "KCL_COORDINATOR_STATE_TABLE_NAME");
        String database = required(environment, "CLICKHOUSE_DATABASE");
        URI clickHouseUrl = URI.create(required(environment, "CLICKHOUSE_HTTP_URL"));
        String failurePrefix = required(environment, "FAILURE_PREFIX");
        boolean localMode = parseLocalMode(environment);
        AwsClientEndpoints awsEndpoints = AwsClientEndpoints.fromEnvironment(environment, localMode);

        if (!REGION.matcher(region).matches()
                || !SIMPLE_NAME.matcher(streamName).matches()
                || !STREAM_ARN.matcher(streamArn).matches()
                || !SIMPLE_NAME.matcher(applicationName).matches()
                || !SIMPLE_NAME.matcher(leaseTable).matches()
                || !SIMPLE_NAME.matcher(workerMetricsTable).matches()
                || !SIMPLE_NAME.matcher(coordinatorStateTable).matches()
                || !IDENTIFIER.matcher(database).matches()
                || !failurePrefix.matches("[A-Za-z0-9][A-Za-z0-9._/-]*/")
                || failurePrefix.startsWith("/")
                || failurePrefix.contains("..")) {
            throw new IllegalArgumentException("RuntimeConfigurationError");
        }
        ClickHouseWriter.requirePrivateHttp(clickHouseUrl, localMode);

        return new RuntimeConfig(
                region,
                required(environment, "RUN_ID"),
                required(environment, "METRIC_NAMESPACE"),
                streamName,
                streamArn,
                applicationName,
                leaseTable,
                workerMetricsTable,
                coordinatorStateTable,
                database,
                clickHouseUrl,
                required(environment, "CLICKHOUSE_SECRET_ARN"),
                required(environment, "FAILURE_BUCKET"),
                failurePrefix,
                environment.getOrDefault("ECS_CONTAINER_METADATA_URI_V4", ""),
                localMode,
                awsEndpoints);
    }

    private static boolean parseLocalMode(Map<String, String> environment) {
        String value = environment.getOrDefault("PHASE7_LOCAL_MODE", "false");
        if ("true".equals(value)) return true;
        if ("false".equals(value)) return false;
        throw new IllegalArgumentException("RuntimeConfigurationError");
    }

    private static String required(Map<String, String> environment, String name) {
        String value = environment.get(name);
        if (value == null || value.isEmpty() || !value.equals(value.trim())) {
            throw new IllegalArgumentException("RuntimeConfigurationError");
        }
        return value;
    }
}

record AwsClientEndpoints(
        URI kinesis,
        URI dynamodb,
        URI cloudWatch,
        URI secretsManager,
        URI s3) {

    private static final List<String> SDK_ENDPOINT_ENVIRONMENT = List.of(
            "AWS_ENDPOINT_URL",
            "AWS_ENDPOINT_URL_KINESIS",
            "AWS_ENDPOINT_URL_DYNAMODB",
            "AWS_ENDPOINT_URL_CLOUDWATCH",
            "AWS_ENDPOINT_URL_SECRETS_MANAGER",
            "AWS_ENDPOINT_URL_SECRETSMANAGER",
            "AWS_ENDPOINT_URL_S3");

    private static final List<String> PHASE7_ENDPOINT_ENVIRONMENT = List.of(
            "PHASE7_KINESIS_ENDPOINT_URL",
            "PHASE7_DYNAMODB_ENDPOINT_URL",
            "PHASE7_CLOUDWATCH_ENDPOINT_URL",
            "PHASE7_SECRETSMANAGER_ENDPOINT_URL",
            "PHASE7_S3_ENDPOINT_URL");

    static AwsClientEndpoints fromEnvironment(Map<String, String> environment, boolean localMode) {
        rejectPresent(environment, SDK_ENDPOINT_ENVIRONMENT);
        if (!localMode) {
            rejectPresent(environment, PHASE7_ENDPOINT_ENVIRONMENT);
            return new AwsClientEndpoints(null, null, null, null, null);
        }
        return new AwsClientEndpoints(
                requireLocalEndpoint(environment, "PHASE7_KINESIS_ENDPOINT_URL"),
                requireLocalEndpoint(environment, "PHASE7_DYNAMODB_ENDPOINT_URL"),
                requireLocalEndpoint(environment, "PHASE7_CLOUDWATCH_ENDPOINT_URL"),
                requireLocalEndpoint(environment, "PHASE7_SECRETSMANAGER_ENDPOINT_URL"),
                requireLocalEndpoint(environment, "PHASE7_S3_ENDPOINT_URL"));
    }

    private static void rejectPresent(Map<String, String> environment, List<String> names) {
        for (String name : names) {
            if (environment.containsKey(name)) {
                throw new IllegalArgumentException("AwsEndpointOverrideRejected");
            }
        }
    }

    private static URI requireLocalEndpoint(Map<String, String> environment, String name) {
        String value = environment.get(name);
        if (value == null || value.isEmpty() || !value.equals(value.trim())) {
            throw new IllegalArgumentException("LocalAwsEndpointRequired");
        }
        URI endpoint;
        try {
            endpoint = URI.create(value);
        } catch (IllegalArgumentException error) {
            throw new IllegalArgumentException("LocalAwsEndpointInvalid");
        }
        String host = endpoint.getHost();
        String path = endpoint.getPath();
        boolean allowedHost = "localstack".equals(host)
                || "localhost".equals(host)
                || "127.0.0.1".equals(host)
                || "::1".equals(host);
        if (!"http".equals(endpoint.getScheme())
                || !allowedHost
                || endpoint.getUserInfo() != null
                || endpoint.getQuery() != null
                || endpoint.getFragment() != null
                || (path != null && !path.isEmpty())) {
            throw new IllegalArgumentException("LocalAwsEndpointInvalid");
        }
        return endpoint;
    }
}

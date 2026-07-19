package com.loopad.performance.phase4;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.http.HttpClient;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.http.urlconnection.UrlConnectionHttpClient;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.cloudwatch.CloudWatchAsyncClient;
import software.amazon.awssdk.services.dynamodb.DynamoDbAsyncClient;
import software.amazon.awssdk.services.kinesis.KinesisAsyncClient;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.secretsmanager.SecretsManagerClient;
import software.amazon.awssdk.services.secretsmanager.model.GetSecretValueRequest;
import software.amazon.kinesis.coordinator.Scheduler;
import software.amazon.kinesis.processor.ShardRecordProcessorFactory;

public final class ConsumerMain {
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private ConsumerMain() {}

    public static void main(String[] args) throws Exception {
        String command = args.length == 0 ? "run" : args[0];
        if ("memory-gate".equals(command)) {
            MemoryGateMain.run();
            return;
        }
        if (!"run".equals(command)) throw new IllegalArgumentException("Unsupported consumer command.");
        runProduction();
    }

    private static void runProduction() throws Exception {
        RuntimeConfig config = RuntimeConfig.fromEnvironment(System.getenv());
        Region region = Region.of(config.region());
        DefaultCredentialsProvider credentials = DefaultCredentialsProvider.create();
        HttpClient clickHouseHttp = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .version(HttpClient.Version.HTTP_1_1)
                .build();

        try (KinesisAsyncClient kinesis = buildKinesis(config, region, credentials);
                DynamoDbAsyncClient dynamo = buildDynamoDb(config, region, credentials);
                CloudWatchAsyncClient cloudWatch = buildCloudWatch(config, region, credentials);
                SecretsManagerClient secrets = buildSecretsManager(config, region, credentials);
                S3Client s3 = buildS3(config, region, credentials);
                EmfTelemetry telemetry = new EmfTelemetry(
                        config.metricNamespace(), config.runId(), config.ecsMetadataUri(), clickHouseHttp)) {
            ClickHouseWriter.Credentials clickHouseCredentials = loadClickHouseCredentials(
                    secrets, config.clickHouseSecretArn());
            BatchProcessor processor = new BatchProcessor(
                    new EventTransformer(),
                    new ClickHouseWriter(clickHouseHttp, config.clickHouseUrl(), config.clickHouseDatabase(),
                            clickHouseCredentials, config.localMode()),
                    new S3FailureArchiver(s3, config.failureBucket(), config.failurePrefix()),
                    telemetry,
                    telemetry,
                    Thread::sleep,
                    RuntimeConfig.MAX_CONCURRENT_BATCHES,
                    Instant::now,
                    System::nanoTime,
                    Math::random);
            ShardRecordProcessorFactory factory = () -> new LoopAdRecordProcessor(
                    config.streamArn(), processor, telemetry, telemetry, error -> Runtime.getRuntime().halt(1));
            String workerId = System.getenv().getOrDefault("HOSTNAME", "unknown") + ":" + UUID.randomUUID();
            Scheduler scheduler = KclSchedulerFactory.create(
                    config, workerId, kinesis, dynamo, cloudWatch, factory);
            Runtime.getRuntime().addShutdownHook(Thread.ofPlatform().name("kcl-graceful-shutdown").unstarted(() -> {
                try {
                    scheduler.startGracefulShutdown().get(115, TimeUnit.SECONDS);
                } catch (Exception error) {
                    telemetry.log("phase4_graceful_shutdown_error", "", 0, 0,
                            error.getClass().getSimpleName());
                }
            }));
            telemetry.startHostMemorySampling();
            Files.writeString(Path.of("/tmp/loopad-phase4-ready"), "java-kcl-3.4.3\n");
            telemetry.log("phase4_native_java_kcl_ready", "", 0, 0, "");
            scheduler.run();
        }
    }

    private static KinesisAsyncClient buildKinesis(
            RuntimeConfig config, Region region, DefaultCredentialsProvider credentials) {
        var builder = KinesisAsyncClient.builder().region(region).credentialsProvider(credentials);
        if (config.localMode()) builder.endpointOverride(config.awsEndpoints().kinesis());
        return builder.build();
    }

    private static DynamoDbAsyncClient buildDynamoDb(
            RuntimeConfig config, Region region, DefaultCredentialsProvider credentials) {
        var builder = DynamoDbAsyncClient.builder().region(region).credentialsProvider(credentials);
        if (config.localMode()) builder.endpointOverride(config.awsEndpoints().dynamodb());
        return builder.build();
    }

    private static CloudWatchAsyncClient buildCloudWatch(
            RuntimeConfig config, Region region, DefaultCredentialsProvider credentials) {
        var builder = CloudWatchAsyncClient.builder().region(region).credentialsProvider(credentials);
        if (config.localMode()) builder.endpointOverride(config.awsEndpoints().cloudWatch());
        return builder.build();
    }

    private static SecretsManagerClient buildSecretsManager(
            RuntimeConfig config, Region region, DefaultCredentialsProvider credentials) {
        var builder = SecretsManagerClient.builder()
                .region(region)
                .credentialsProvider(credentials)
                .httpClientBuilder(UrlConnectionHttpClient.builder());
        if (config.localMode()) builder.endpointOverride(config.awsEndpoints().secretsManager());
        return builder.build();
    }

    private static S3Client buildS3(
            RuntimeConfig config, Region region, DefaultCredentialsProvider credentials) {
        var builder = S3Client.builder()
                .region(region)
                .credentialsProvider(credentials)
                .forcePathStyle(config.localMode())
                .httpClientBuilder(UrlConnectionHttpClient.builder());
        if (config.localMode()) builder.endpointOverride(config.awsEndpoints().s3());
        return builder.build();
    }

    static ClickHouseWriter.Credentials loadClickHouseCredentials(SecretsManagerClient secrets, String arn) {
        String secret = secrets.getSecretValue(GetSecretValueRequest.builder().secretId(arn).build()).secretString();
        try {
            JsonNode parsed = MAPPER.readTree(secret);
            String username = parsed.path("username").asText("");
            String password = parsed.path("password").asText("");
            if (username.isEmpty() || password.isEmpty()) throw new IllegalArgumentException("SecretConfigurationError");
            return new ClickHouseWriter.Credentials(username, password);
        } catch (Exception error) {
            throw new IllegalArgumentException("SecretConfigurationError");
        }
    }
}

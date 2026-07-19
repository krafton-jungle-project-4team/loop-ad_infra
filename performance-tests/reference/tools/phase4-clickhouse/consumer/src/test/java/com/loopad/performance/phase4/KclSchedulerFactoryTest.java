package com.loopad.performance.phase4;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.net.URI;
import java.lang.reflect.Proxy;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.services.cloudwatch.CloudWatchAsyncClient;
import software.amazon.awssdk.services.dynamodb.DynamoDbAsyncClient;
import software.amazon.awssdk.services.kinesis.KinesisAsyncClient;
import software.amazon.kinesis.common.InitialPositionInStream;
import software.amazon.kinesis.metrics.MetricsLevel;
import software.amazon.kinesis.processor.ShardRecordProcessorFactory;
import software.amazon.kinesis.retrieval.polling.PollingConfig;

class KclSchedulerFactoryTest {
    @Test
    void pinsMemoryBoundPollingLeaseAndGracefulSettings() {
        RuntimeConfig runtime = new RuntimeConfig(
                "ap-northeast-2", "run_test", "LoopAd/Phase4", "stream", ConsumerFixtures.STREAM_ARN,
                "application", "leases", "workers", "coordinator", "loopad",
                URI.create("http://10.45.0.10:8123"), "secret", "bucket", "failures/run_test/", "",
                false, new AwsClientEndpoints(null, null, null, null, null));
        var config = KclSchedulerFactory.configuration(
                runtime,
                "worker",
                proxy(KinesisAsyncClient.class),
                proxy(DynamoDbAsyncClient.class),
                proxy(CloudWatchAsyncClient.class),
                () -> null);

        assertEquals(60, config.lease().maxLeasesForWorker());
        assertTrue(config.lease().gracefulLeaseHandoffConfig().isGracefulLeaseHandoffEnabled());
        assertEquals(120_000, config.lease().gracefulLeaseHandoffConfig().gracefulLeaseHandoffTimeoutMillis());
        assertEquals("workers", config.lease().workerUtilizationAwareAssignmentConfig()
                .workerMetricsTableConfig().tableName());
        assertEquals("coordinator", config.coordinator().coordinatorStateTableConfig().tableName());
        assertEquals(InitialPositionInStream.LATEST,
                config.lease().initialPositionInStream().getInitialPositionInStream());
        assertEquals(InitialPositionInStream.LATEST,
                config.retrieval().streamTracker().streamConfigList().get(0)
                        .initialPositionInStreamExtended().getInitialPositionInStream());
        assertEquals(MetricsLevel.DETAILED, config.metrics().metricsLevel());
        assertFalse(config.processor().callProcessRecordsEvenForEmptyRecordList());
        PollingConfig polling = (PollingConfig) config.retrieval().retrievalSpecificConfig();
        assertEquals(1_000, polling.maxRecords());
        assertEquals(0, polling.maxPendingProcessRecordsInput());
        assertEquals(200, polling.idleTimeBetweenReadsInMillis());
    }

    @Test
    void localModeReadsStartupRecordsAndDisablesCloudWatchPublishing() {
        RuntimeConfig runtime = new RuntimeConfig(
                "ap-northeast-2", "run_test", "LoopAd/Phase7Local", "stream", ConsumerFixtures.STREAM_ARN,
                "application", "leases", "workers", "coordinator", "loopad",
                URI.create("http://clickhouse:8123"), "secret", "bucket", "failures/run_test/", "",
                true, new AwsClientEndpoints(
                        URI.create("http://localstack:4566"),
                        URI.create("http://localstack:4566"),
                        URI.create("http://localstack:4566"),
                        URI.create("http://localstack:4566"),
                        URI.create("http://localstack:4566")));
        var config = KclSchedulerFactory.configuration(
                runtime,
                "worker",
                proxy(KinesisAsyncClient.class),
                proxy(DynamoDbAsyncClient.class),
                proxy(CloudWatchAsyncClient.class),
                () -> null);

        assertEquals(InitialPositionInStream.TRIM_HORIZON,
                config.lease().initialPositionInStream().getInitialPositionInStream());
        assertEquals(InitialPositionInStream.TRIM_HORIZON,
                config.retrieval().streamTracker().streamConfigList().get(0)
                        .initialPositionInStreamExtended().getInitialPositionInStream());
        assertEquals(MetricsLevel.NONE, config.metrics().metricsLevel());
        assertTrue(config.metrics().metricsEnabledDimensions().isEmpty());
    }

    private static <T> T proxy(Class<T> type) {
        return type.cast(Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[] {type},
                (instance, method, arguments) -> {
                    if ("toString".equals(method.getName())) return type.getSimpleName();
                    if ("close".equals(method.getName())) return null;
                    throw new UnsupportedOperationException(method.getName());
                }));
    }
}

package com.loopad.performance.phase4;

import java.util.Set;
import software.amazon.awssdk.services.cloudwatch.CloudWatchAsyncClient;
import software.amazon.awssdk.services.dynamodb.DynamoDbAsyncClient;
import software.amazon.awssdk.services.dynamodb.model.BillingMode;
import software.amazon.awssdk.services.kinesis.KinesisAsyncClient;
import software.amazon.kinesis.common.ConfigsBuilder;
import software.amazon.kinesis.common.InitialPositionInStream;
import software.amazon.kinesis.common.InitialPositionInStreamExtended;
import software.amazon.kinesis.coordinator.CoordinatorConfig;
import software.amazon.kinesis.coordinator.Scheduler;
import software.amazon.kinesis.leases.LeaseManagementConfig;
import software.amazon.kinesis.leases.LeaseManagementConfig.GracefulLeaseHandoffConfig;
import software.amazon.kinesis.lifecycle.LifecycleConfig;
import software.amazon.kinesis.metrics.MetricsConfig;
import software.amazon.kinesis.metrics.MetricsLevel;
import software.amazon.kinesis.processor.ProcessorConfig;
import software.amazon.kinesis.processor.ShardRecordProcessorFactory;
import software.amazon.kinesis.processor.SingleStreamTracker;
import software.amazon.kinesis.retrieval.RetrievalConfig;
import software.amazon.kinesis.retrieval.polling.PollingConfig;

final class KclSchedulerFactory {
    record Configuration(
            LeaseManagementConfig lease,
            CoordinatorConfig coordinator,
            LifecycleConfig lifecycle,
            MetricsConfig metrics,
            ProcessorConfig processor,
            RetrievalConfig retrieval) {}

    private KclSchedulerFactory() {}

    static Configuration configuration(
            RuntimeConfig runtime,
            String workerId,
            KinesisAsyncClient kinesis,
            DynamoDbAsyncClient dynamo,
            CloudWatchAsyncClient cloudWatch,
            ShardRecordProcessorFactory recordProcessors) {
        InitialPositionInStreamExtended initialPosition = InitialPositionInStreamExtended.newInitialPosition(
                runtime.localMode()
                        ? InitialPositionInStream.TRIM_HORIZON
                        : InitialPositionInStream.LATEST);
        ConfigsBuilder builder = new ConfigsBuilder(
                new SingleStreamTracker(runtime.streamName(), initialPosition),
                runtime.applicationName(),
                kinesis,
                dynamo,
                cloudWatch,
                workerId,
                recordProcessors)
                .tableName(runtime.leaseTableName());

        LeaseManagementConfig lease = builder.leaseManagementConfig()
                .maxLeasesForWorker(RuntimeConfig.MAX_LEASES_PER_WORKER)
                .cleanupLeasesUponShardCompletion(true)
                .initialPositionInStream(initialPosition)
                .billingMode(BillingMode.PAY_PER_REQUEST)
                .gracefulLeaseHandoffConfig(GracefulLeaseHandoffConfig.builder()
                        .isGracefulLeaseHandoffEnabled(true)
                        .gracefulLeaseHandoffTimeoutMillis(RuntimeConfig.GRACEFUL_LEASE_HANDOFF_MILLIS)
                        .build());
        lease.workerUtilizationAwareAssignmentConfig().workerMetricsTableConfig()
                .tableName(runtime.workerMetricsTableName())
                .billingMode(BillingMode.PAY_PER_REQUEST);

        CoordinatorConfig coordinator = builder.coordinatorConfig()
                .clientVersionConfig(CoordinatorConfig.ClientVersionConfig.CLIENT_VERSION_CONFIG_3X);
        coordinator.coordinatorStateTableConfig()
                .tableName(runtime.coordinatorStateTableName())
                .billingMode(BillingMode.PAY_PER_REQUEST);

        LifecycleConfig lifecycle = builder.lifecycleConfig().taskBackoffTimeMillis(500);
        MetricsConfig metrics = builder.metricsConfig()
                .metricsBufferTimeMillis(10_000)
                .metricsMaxQueueSize(10_000);
        if (runtime.localMode()) {
            metrics.metricsLevel(MetricsLevel.NONE).metricsEnabledDimensions(Set.of());
        } else {
            metrics.metricsLevel(MetricsLevel.DETAILED)
                    .metricsEnabledDimensions(Set.of("Operation", "ShardId", "WorkerIdentifier"));
        }
        ProcessorConfig processor = builder.processorConfig()
                .callProcessRecordsEvenForEmptyRecordList(false);
        RetrievalConfig retrieval = builder.retrievalConfig()
                .initialPositionInStreamExtended(initialPosition)
                .retrievalSpecificConfig(new PollingConfig(runtime.streamName(), kinesis)
                        .maxRecords(RuntimeConfig.MAX_RECORDS)
                        .maxPendingProcessRecordsInput(RuntimeConfig.MAX_PENDING_PROCESS_RECORDS_INPUT)
                        .idleTimeBetweenReadsInMillis(RuntimeConfig.POLLING_IDLE_MILLIS));
        return new Configuration(lease, coordinator, lifecycle, metrics, processor, retrieval);
    }

    static Scheduler create(
            RuntimeConfig runtime,
            String workerId,
            KinesisAsyncClient kinesis,
            DynamoDbAsyncClient dynamo,
            CloudWatchAsyncClient cloudWatch,
            ShardRecordProcessorFactory recordProcessors) {
        ConfigsBuilder defaults = new ConfigsBuilder(
                runtime.streamName(), runtime.applicationName(), kinesis, dynamo, cloudWatch, workerId, recordProcessors)
                .tableName(runtime.leaseTableName());
        Configuration configured = configuration(
                runtime, workerId, kinesis, dynamo, cloudWatch, recordProcessors);
        return new Scheduler(
                defaults.checkpointConfig(),
                configured.coordinator(),
                configured.lease(),
                configured.lifecycle(),
                configured.metrics(),
                configured.processor(),
                configured.retrieval());
    }
}

package com.loopad.performance.phase4;

import static com.loopad.performance.phase4.ConsumerFixtures.NOW;
import static com.loopad.performance.phase4.ConsumerFixtures.SHARD_ID;
import static com.loopad.performance.phase4.ConsumerFixtures.STREAM_ARN;
import static com.loopad.performance.phase4.ConsumerFixtures.payload;
import static com.loopad.performance.phase4.ConsumerFixtures.record;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import software.amazon.kinesis.lifecycle.events.InitializationInput;
import software.amazon.kinesis.lifecycle.events.LeaseLostInput;
import software.amazon.kinesis.lifecycle.events.ProcessRecordsInput;
import software.amazon.kinesis.lifecycle.events.ShardEndedInput;
import software.amazon.kinesis.lifecycle.events.ShutdownRequestedInput;
import software.amazon.kinesis.processor.RecordProcessorCheckpointer;

class LoopAdRecordProcessorTest {
    @Test
    void checkpointsOnlyAfterProcessingAndSupportsGracefulCallbacks() throws Exception {
        List<List<Object>> checkpoints = new ArrayList<>();
        RecordProcessorCheckpointer checkpointer = (RecordProcessorCheckpointer) Proxy.newProxyInstance(
                RecordProcessorCheckpointer.class.getClassLoader(),
                new Class<?>[] {RecordProcessorCheckpointer.class},
                (instance, method, arguments) -> {
                    if ("checkpoint".equals(method.getName())) {
                        checkpoints.add(arguments == null ? List.of() : List.of(arguments));
                        return null;
                    }
                    throw new UnsupportedOperationException(method.getName());
                });
        BatchProcessor batchProcessor = new BatchProcessor(
                new EventTransformer(),
                (table, body, timeout) -> {},
                (batch, category, attempts) -> {},
                (name, count, timestamp) -> {},
                (event, shard, records, attempts, category) -> {},
                ignored -> {},
                RuntimeConfig.MAX_CONCURRENT_BATCHES,
                () -> NOW,
                () -> 0L,
                () -> 0.0);
        LoopAdRecordProcessor processor = new LoopAdRecordProcessor(
                STREAM_ARN,
                batchProcessor,
                (name, count, timestamp) -> {},
                (event, shard, records, attempts, category) -> {},
                error -> { throw new AssertionError(error); });
        processor.initialize(InitializationInput.builder().shardId(SHARD_ID).build());
        processor.processRecords(ProcessRecordsInput.builder()
                .records(List.of(record(payload("valid", "2026-07-16T11:59:59Z"), "10")))
                .checkpointer(checkpointer)
                .millisBehindLatest(0L)
                .build());
        org.junit.jupiter.api.Assertions.assertEquals(List.of(List.of("10")), checkpoints);

        processor.shutdownRequested(ShutdownRequestedInput.builder().checkpointer(checkpointer).build());
        org.junit.jupiter.api.Assertions.assertEquals(List.of(List.of("10"), List.of("10")), checkpoints);
        processor.leaseLost(null);

        processor.shardEnded(ShardEndedInput.builder().checkpointer(checkpointer).build());
        org.junit.jupiter.api.Assertions.assertEquals(
                List.of(List.of("10"), List.of("10"), List.of()), checkpoints);
    }
}

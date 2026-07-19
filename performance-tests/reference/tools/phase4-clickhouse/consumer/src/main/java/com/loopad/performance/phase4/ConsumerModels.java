package com.loopad.performance.phase4;

import java.util.List;
import software.amazon.kinesis.retrieval.KinesisClientRecord;

final class ConsumerModels {
    private ConsumerModels() {}

    record BatchContext(
            String streamArn,
            String shardId,
            Long millisBehindLatest,
            List<KinesisClientRecord> records) {
        BatchContext {
            records = List.copyOf(records);
        }
    }

    record BatchPlan(
            byte[] eventsNdjson,
            byte[] rawEventsNdjson,
            int eventRows,
            int rawEventRows,
            int lateEventCount) {}

    record Checkpoint(String sequenceNumber, long subSequenceNumber, boolean aggregated) {}

    record BatchResult(
            Checkpoint checkpoint,
            int eventRows,
            int rawEventRows,
            int lateEventCount,
            boolean terminalFailure) {}
}

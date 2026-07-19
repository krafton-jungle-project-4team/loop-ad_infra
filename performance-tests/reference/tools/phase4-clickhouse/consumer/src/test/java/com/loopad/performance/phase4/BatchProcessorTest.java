package com.loopad.performance.phase4;

import static com.loopad.performance.phase4.ConsumerFixtures.NOW;
import static com.loopad.performance.phase4.ConsumerFixtures.batch;
import static com.loopad.performance.phase4.ConsumerFixtures.payload;
import static com.loopad.performance.phase4.ConsumerFixtures.record;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

class BatchProcessorTest {
    @Test
    void retriesThenReturnsCheckpointAfterInsert() {
        AtomicInteger writes = new AtomicInteger();
        AtomicInteger visibilityRecords = new AtomicInteger();
        List<Long> sleeps = new ArrayList<>();
        List<String> summaries = new ArrayList<>();
        BatchProcessor processor = processor(
                (table, body, timeout) -> {
                    if (writes.incrementAndGet() == 1) throw new IllegalStateException("sensitive detail");
                },
                (input, category, attempts) -> {},
                sleeps::add,
                new BatchProcessor.SummaryLogger() {
                    @Override
                    public void log(String event, String shard, int records, int attempts, String category) {
                        summaries.add(event);
                    }

                    @Override
                    public void logVisibility(String shard, List<software.amazon.kinesis.retrieval.KinesisClientRecord> records,
                            java.time.Instant completedAt) {
                        visibilityRecords.addAndGet(records.size());
                    }
                });

        var result = processor.process(batch(List.of(record(
                payload("valid", "2026-07-16T11:59:59Z"), "10"))));

        assertEquals(2, writes.get());
        assertEquals(List.of(200L), sleeps);
        assertEquals("10", result.checkpoint().sequenceNumber());
        assertEquals(1, visibilityRecords.get());
        assertTrue(summaries.contains("phase4_batch_success"));
        assertTrue(summaries.stream().noneMatch(value -> value.contains("sensitive")));
    }

    @Test
    void archivesBeforeTerminalCheckpointAndBlocksWhenArchiveFails() {
        List<String> order = new ArrayList<>();
        BatchProcessor processor = processor(
                (table, body, timeout) -> { throw new IllegalStateException("unavailable"); },
                (input, category, attempts) -> order.add("archive"),
                ignored -> {},
                (event, shard, records, attempts, category) -> order.add(event));
        var result = processor.process(batch(List.of(record(
                payload("terminal", "2026-07-16T11:59:59Z"), "99"))));
        assertTrue(result.terminalFailure());
        assertEquals(5, order.stream().filter("phase4_batch_retry"::equals).count());
        assertTrue(order.indexOf("archive") < order.indexOf("phase4_terminal_failure"));

        BatchProcessor archiveFailure = processor(
                (table, body, timeout) -> { throw new IllegalStateException("unavailable"); },
                (input, category, attempts) -> { throw new IllegalStateException("archive unavailable"); },
                ignored -> {},
                (event, shard, records, attempts, category) -> {});
        assertThrows(BatchProcessor.TerminalArchiveException.class, () -> archiveFailure.process(batch(List.of(
                record(payload("blocked", "2026-07-16T11:59:59Z"), "100")))));
    }

    private static BatchProcessor processor(
            BatchProcessor.RowWriter writer,
            BatchProcessor.FailureArchiver archiver,
            BatchProcessor.Sleeper sleeper,
            BatchProcessor.SummaryLogger summaries) {
        return new BatchProcessor(
                new EventTransformer(), writer, archiver, (name, count, timestamp) -> {}, summaries,
                sleeper, RuntimeConfig.MAX_CONCURRENT_BATCHES, () -> NOW, () -> 0L, () -> 0.0);
    }
}

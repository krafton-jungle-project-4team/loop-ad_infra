package com.loopad.performance.phase4;

import static com.loopad.performance.phase4.ConsumerModels.BatchContext;
import static com.loopad.performance.phase4.ConsumerModels.BatchPlan;
import static com.loopad.performance.phase4.ConsumerModels.BatchResult;
import static com.loopad.performance.phase4.ConsumerModels.Checkpoint;

import java.time.Duration;
import java.time.Instant;
import java.util.Objects;
import java.util.concurrent.Semaphore;
import java.util.function.DoubleSupplier;
import java.util.function.LongSupplier;
import java.util.function.Supplier;
import software.amazon.kinesis.retrieval.KinesisClientRecord;

final class BatchProcessor {
    static final int MAX_INSERT_ATTEMPTS = 5;
    static final Duration INSERT_RETRY_DEADLINE = Duration.ofSeconds(60);
    static final Duration CLICKHOUSE_REQUEST_TIMEOUT = Duration.ofSeconds(20);

    interface RowWriter {
        void insert(String table, byte[] ndjson, Duration timeout) throws Exception;
    }

    interface FailureArchiver {
        void archive(BatchContext batch, String errorCategory, int attempts) throws Exception;
    }

    interface MetricEmitter {
        void emit(String name, long count, Instant timestamp);
    }

    interface SummaryLogger {
        void log(String event, String shardId, int inputRecords, int attempts, String errorCategory);

        default void logVisibility(
                String shardId,
                java.util.List<KinesisClientRecord> records,
                Instant completedAt) {
            // Optional for local gates that only assert lifecycle summaries.
        }
    }

    interface Sleeper {
        void sleep(long milliseconds) throws InterruptedException;
    }

    private final EventTransformer transformer;
    private final RowWriter writer;
    private final FailureArchiver archiver;
    private final MetricEmitter metrics;
    private final SummaryLogger summaries;
    private final Sleeper sleeper;
    private final Semaphore concurrency;
    private final Supplier<Instant> wallClock;
    private final LongSupplier monotonicNanos;
    private final DoubleSupplier random;

    BatchProcessor(
            EventTransformer transformer,
            RowWriter writer,
            FailureArchiver archiver,
            MetricEmitter metrics,
            SummaryLogger summaries,
            Sleeper sleeper,
            int maxConcurrentBatches,
            Supplier<Instant> wallClock,
            LongSupplier monotonicNanos,
            DoubleSupplier random) {
        this.transformer = Objects.requireNonNull(transformer);
        this.writer = Objects.requireNonNull(writer);
        this.archiver = Objects.requireNonNull(archiver);
        this.metrics = Objects.requireNonNull(metrics);
        this.summaries = Objects.requireNonNull(summaries);
        this.sleeper = Objects.requireNonNull(sleeper);
        if (maxConcurrentBatches < 1) throw new IllegalArgumentException("maxConcurrentBatches");
        this.concurrency = new Semaphore(maxConcurrentBatches, true);
        this.wallClock = Objects.requireNonNull(wallClock);
        this.monotonicNanos = Objects.requireNonNull(monotonicNanos);
        this.random = Objects.requireNonNull(random);
    }

    BatchResult process(BatchContext batch) {
        if (batch.records().isEmpty()) return new BatchResult(null, 0, 0, 0, false);
        boolean acquired = false;
        try {
            concurrency.acquire();
            acquired = true;
            return processBounded(batch);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new FatalProcessingException("InterruptedProcessing", error);
        } finally {
            if (acquired) concurrency.release();
        }
    }

    int availablePermits() {
        return concurrency.availablePermits();
    }

    private BatchResult processBounded(BatchContext batch) {
        Instant receivedAt = wallClock.get();
        BatchPlan plan = transformer.plan(batch, receivedAt);
        long startedAt = monotonicNanos.getAsLong();
        String finalError = "UnknownError";
        int attempts = 0;

        for (int attempt = 1; attempt <= MAX_INSERT_ATTEMPTS; attempt += 1) {
            attempts = attempt;
            Duration elapsed = elapsedSince(startedAt);
            Duration remaining = INSERT_RETRY_DEADLINE.minus(elapsed);
            if (remaining.isNegative() || remaining.isZero()) break;
            Duration timeout = remaining.compareTo(CLICKHOUSE_REQUEST_TIMEOUT) < 0
                    ? remaining
                    : CLICKHOUSE_REQUEST_TIMEOUT;
            try {
                if (plan.eventsNdjson().length > 0) writer.insert("events", plan.eventsNdjson(), timeout);
                if (plan.rawEventsNdjson().length > 0) writer.insert("raw_events", plan.rawEventsNdjson(), timeout);
                if (plan.lateEventCount() > 0) {
                    metrics.emit("LateEventDropped", plan.lateEventCount(), receivedAt);
                }
                summaries.logVisibility(batch.shardId(), batch.records(), wallClock.get());
                summaries.log("phase4_batch_success", batch.shardId(), batch.records().size(), attempt, "");
                return result(batch, plan, false);
            } catch (Exception error) {
                finalError = safeCategory(error);
                summaries.log("phase4_batch_retry", batch.shardId(), batch.records().size(), attempt, finalError);
            }

            if (attempt == MAX_INSERT_ATTEMPTS) break;
            long delay = retryDelayMillis(attempt, random.getAsDouble());
            Duration afterAttempt = elapsedSince(startedAt);
            if (INSERT_RETRY_DEADLINE.minus(afterAttempt).compareTo(Duration.ofMillis(delay)) <= 0) break;
            try {
                sleeper.sleep(delay);
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new FatalProcessingException("InterruptedProcessing", error);
            }
        }

        try {
            archiver.archive(batch, finalError, attempts);
        } catch (Exception error) {
            summaries.log("phase4_terminal_archive_error", batch.shardId(), batch.records().size(), attempts, "ArchiveError");
            throw new TerminalArchiveException(error);
        }
        metrics.emit("TerminalFailure", 1, wallClock.get());
        summaries.log("phase4_terminal_failure", batch.shardId(), batch.records().size(), attempts, finalError);
        return result(batch, plan, true);
    }

    private static BatchResult result(BatchContext batch, BatchPlan plan, boolean terminalFailure) {
        KinesisClientRecord last = batch.records().getLast();
        Checkpoint checkpoint = new Checkpoint(last.sequenceNumber(), last.subSequenceNumber(), last.aggregated());
        return new BatchResult(
                checkpoint,
                plan.eventRows(),
                plan.rawEventRows(),
                plan.lateEventCount(),
                terminalFailure);
    }

    private Duration elapsedSince(long startedAt) {
        return Duration.ofNanos(Math.max(0, monotonicNanos.getAsLong() - startedAt));
    }

    private static long retryDelayMillis(int attempt, double random) {
        long exponential = Math.min(5_000, 200L << Math.max(0, attempt - 1));
        double bounded = Math.max(0.0, Math.min(1.0, random));
        return exponential + (long) Math.floor(exponential * bounded * 0.25);
    }

    private static String safeCategory(Exception error) {
        String simple = error.getClass().getSimpleName();
        if (simple.matches("[A-Za-z][A-Za-z0-9]*")) return simple;
        return "UnknownError";
    }

    static final class TerminalArchiveException extends RuntimeException {
        TerminalArchiveException(Throwable cause) {
            super("Terminal failure archive write failed.", cause);
        }
    }

    static final class FatalProcessingException extends RuntimeException {
        FatalProcessingException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}

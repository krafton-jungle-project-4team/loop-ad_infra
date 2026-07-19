package com.loopad.performance.phase4;

import static com.loopad.performance.phase4.ConsumerModels.BatchContext;
import static com.loopad.performance.phase4.ConsumerModels.BatchResult;
import static com.loopad.performance.phase4.ConsumerModels.Checkpoint;

import java.util.concurrent.TimeUnit;
import software.amazon.kinesis.exceptions.ShutdownException;
import software.amazon.kinesis.lifecycle.events.InitializationInput;
import software.amazon.kinesis.lifecycle.events.LeaseLostInput;
import software.amazon.kinesis.lifecycle.events.ProcessRecordsInput;
import software.amazon.kinesis.lifecycle.events.ShardEndedInput;
import software.amazon.kinesis.lifecycle.events.ShutdownRequestedInput;
import software.amazon.kinesis.processor.RecordProcessorCheckpointer;
import software.amazon.kinesis.processor.ShardRecordProcessor;

final class LoopAdRecordProcessor implements ShardRecordProcessor {
    private static final int CHECKPOINT_ATTEMPTS = 5;

    @FunctionalInterface
    interface FatalHandler {
        void fatal(Throwable error);
    }

    private final String streamArn;
    private final BatchProcessor processor;
    private final BatchProcessor.MetricEmitter metrics;
    private final BatchProcessor.SummaryLogger summaries;
    private final FatalHandler fatalHandler;
    private String shardId;
    private Checkpoint lastSuccessful;

    LoopAdRecordProcessor(
            String streamArn,
            BatchProcessor processor,
            BatchProcessor.MetricEmitter metrics,
            BatchProcessor.SummaryLogger summaries,
            FatalHandler fatalHandler) {
        this.streamArn = streamArn;
        this.processor = processor;
        this.metrics = metrics;
        this.summaries = summaries;
        this.fatalHandler = fatalHandler;
    }

    @Override
    public void initialize(InitializationInput input) {
        shardId = input.shardId();
        summaries.log("phase4_shard_initialized", shardId, 0, 0, "");
    }

    @Override
    public void processRecords(ProcessRecordsInput input) {
        if (input.records().isEmpty()) return;
        try {
            BatchResult result = processor.process(new BatchContext(
                    streamArn, shardId, input.millisBehindLatest(), input.records()));
            if (result.checkpoint() != null && checkpoint(input.checkpointer(), result.checkpoint(), false)) {
                lastSuccessful = result.checkpoint();
            }
        } catch (Throwable error) {
            summaries.log("phase4_fatal_processing_error", shardId, input.records().size(), 0,
                    safeCategory(error));
            fatalHandler.fatal(error);
            throw new BatchProcessor.FatalProcessingException("FatalProcessingError", error);
        }
    }

    @Override
    public void leaseLost(LeaseLostInput input) {
        summaries.log("phase4_lease_lost", shardId, 0, 0, "");
    }

    @Override
    public void shardEnded(ShardEndedInput input) {
        try {
            checkpointAtShardEnd(input.checkpointer());
        } catch (Throwable error) {
            summaries.log("phase4_shard_end_checkpoint_error", shardId, 0, CHECKPOINT_ATTEMPTS,
                    safeCategory(error));
            fatalHandler.fatal(error);
            throw new BatchProcessor.FatalProcessingException("ShardEndCheckpointError", error);
        }
    }

    @Override
    public void shutdownRequested(ShutdownRequestedInput input) {
        if (lastSuccessful == null) return;
        try {
            checkpoint(input.checkpointer(), lastSuccessful, true);
        } catch (Throwable error) {
            summaries.log("phase4_shutdown_checkpoint_error", shardId, 0, CHECKPOINT_ATTEMPTS,
                    safeCategory(error));
            fatalHandler.fatal(error);
            throw new BatchProcessor.FatalProcessingException("ShutdownCheckpointError", error);
        }
    }

    private boolean checkpoint(RecordProcessorCheckpointer checkpointer, Checkpoint checkpoint, boolean shutdown) {
        for (int attempt = 1; attempt <= CHECKPOINT_ATTEMPTS; attempt += 1) {
            try {
                if (checkpoint.aggregated()) {
                    checkpointer.checkpoint(checkpoint.sequenceNumber(), checkpoint.subSequenceNumber());
                } else {
                    checkpointer.checkpoint(checkpoint.sequenceNumber());
                }
                summaries.log(shutdown ? "phase4_shutdown_checkpoint" : "phase4_batch_checkpoint",
                        shardId, 0, attempt, "");
                return true;
            } catch (ShutdownException leaseAlreadyLost) {
                metrics.emit("CheckpointError", 1, java.time.Instant.now());
                summaries.log("phase4_checkpoint_skipped_after_lease_loss", shardId, 0, attempt,
                        "ShutdownException");
                return false;
            } catch (Exception error) {
                metrics.emit("CheckpointError", 1, java.time.Instant.now());
                if (attempt == CHECKPOINT_ATTEMPTS) throw new CheckpointFailureException(error);
                sleepBeforeCheckpointRetry(attempt);
            }
        }
        throw new IllegalStateException("UnreachableCheckpointState");
    }

    private void checkpointAtShardEnd(RecordProcessorCheckpointer checkpointer) {
        for (int attempt = 1; attempt <= CHECKPOINT_ATTEMPTS; attempt += 1) {
            try {
                checkpointer.checkpoint();
                summaries.log("phase4_shard_end_checkpoint", shardId, 0, attempt, "");
                return;
            } catch (Exception error) {
                metrics.emit("CheckpointError", 1, java.time.Instant.now());
                if (attempt == CHECKPOINT_ATTEMPTS) throw new CheckpointFailureException(error);
                sleepBeforeCheckpointRetry(attempt);
            }
        }
    }

    private static void sleepBeforeCheckpointRetry(int attempt) {
        try {
            TimeUnit.MILLISECONDS.sleep(Math.min(2_000, 100L << (attempt - 1)));
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new CheckpointFailureException(error);
        }
    }

    private static String safeCategory(Throwable error) {
        String name = error.getClass().getSimpleName();
        return name.matches("[A-Za-z][A-Za-z0-9]*") ? name : "UnknownError";
    }

    static final class CheckpointFailureException extends RuntimeException {
        CheckpointFailureException(Throwable cause) {
            super("KCL checkpoint failed.", cause);
        }
    }
}

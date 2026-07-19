CREATE DATABASE IF NOT EXISTS loopad;

CREATE TABLE IF NOT EXISTS loopad.events
(
    project_id String,
    write_key String,
    schema_version LowCardinality(String),
    event_id String,
    event_name LowCardinality(String),
    event_time DateTime64(3, 'UTC'),
    event_date Date MATERIALIZED toDate(event_time),
    source LowCardinality(String),
    user_id Nullable(String),
    session_id Nullable(String),
    properties_json String,
    producer_sent_at Nullable(DateTime64(3, 'UTC')),
    run_id Nullable(String),
    kinesis_shard_id LowCardinality(String),
    kinesis_sequence_number UInt256,
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY event_date
ORDER BY (project_id, event_id);

CREATE TABLE IF NOT EXISTS loopad.raw_events
(
    stream_arn String,
    shard_id LowCardinality(String),
    sequence_number UInt256,
    partition_key String,
    approximate_arrival_at DateTime64(3, 'UTC'),
    raw_payload_base64 String,
    error_code LowCardinality(String),
    error_message String,
    lambda_received_at DateTime64(3, 'UTC'),
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3),
    ingested_date Date MATERIALIZED toDate(ingested_at),
    run_id Nullable(String)
)
ENGINE = MergeTree
PARTITION BY ingested_date
ORDER BY (ingested_at, shard_id, sequence_number);

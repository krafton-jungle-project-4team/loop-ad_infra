#!/bin/sh
set -eu

INIT_COMPLETE_PATH=${PHASE7_INIT_COMPLETE_PATH:-/tmp/phase7-init-complete}

if ! awslocal kinesis describe-stream-summary --stream-name phase7-local-events >/dev/null 2>&1; then
  awslocal kinesis create-stream --stream-name phase7-local-events --shard-count 4 || \
    awslocal kinesis describe-stream-summary --stream-name phase7-local-events >/dev/null
fi
awslocal kinesis wait stream-exists --stream-name phase7-local-events

if ! awslocal secretsmanager describe-secret --secret-id phase7-local-clickhouse >/dev/null 2>&1; then
  awslocal secretsmanager create-secret \
    --name phase7-local-clickhouse \
    --secret-string '{"username":"loopad_local","password":"local-only-not-a-secret"}' || \
    awslocal secretsmanager describe-secret --secret-id phase7-local-clickhouse >/dev/null
fi

for bucket in phase7-local-failures phase7-local-archive; do
  if ! awslocal s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    awslocal s3api create-bucket \
      --bucket "$bucket" \
      --create-bucket-configuration LocationConstraint=ap-northeast-2 || \
      awslocal s3api head-bucket --bucket "$bucket" >/dev/null
  fi
done
touch "$INIT_COMPLETE_PATH"

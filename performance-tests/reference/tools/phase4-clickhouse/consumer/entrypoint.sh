#!/bin/sh
set -eu
umask 077

if ! (cd /opt/loopad && sha256sum --check --strict consumer.jar.sha256 >/dev/null); then
    echo 'Java consumer artifact integrity check failed.' >&2
    exit 1
fi

case "${1:-run}" in
    memory-gate) ;;
    run)
        required_environment='AWS_REGION RUN_ID METRIC_NAMESPACE KINESIS_STREAM_NAME KINESIS_STREAM_ARN KCL_APPLICATION_NAME KCL_LEASE_TABLE_NAME KCL_WORKER_METRICS_TABLE_NAME KCL_COORDINATOR_STATE_TABLE_NAME CLICKHOUSE_DATABASE CLICKHOUSE_HTTP_URL CLICKHOUSE_SECRET_ARN FAILURE_BUCKET FAILURE_PREFIX'
        for name in ${required_environment}; do
            value="$(printenv "${name}" 2>/dev/null || true)"
            if [ -z "${value}" ]; then
                echo "Missing required runtime configuration: ${name}" >&2
                exit 1
            fi
            case "${value}" in
                *'
'*|*''*|*'='*)
                    echo "Invalid runtime configuration: ${name}" >&2
                    exit 1
                    ;;
            esac
        done
        ;;
    *)
        echo 'Unsupported consumer command.' >&2
        exit 1
        ;;
esac

exec java \
    -XX:+UseG1GC \
    -XX:InitialRAMPercentage=20.0 \
    -XX:MaxRAMPercentage=65.0 \
    -XX:MaxDirectMemorySize=256m \
    -Xss256k \
    -XX:+ExitOnOutOfMemoryError \
    -Djava.io.tmpdir=/tmp \
    -Dorg.slf4j.simpleLogger.defaultLogLevel=info \
    -jar /opt/loopad/consumer.jar "$@"

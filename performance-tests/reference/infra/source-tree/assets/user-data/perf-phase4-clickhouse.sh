#!/bin/bash
set -euo pipefail
umask 077

: "${AWS_REGION:?}"
: "${CLICKHOUSE_ALLOWED_CIDR:?}"
: "${CLICKHOUSE_CREDENTIALS_SECRET_ARN:?}"
: "${CLICKHOUSE_HTTP_PORT:?}"
: "${CLICKHOUSE_IMAGE:?}"
: "${CLICKHOUSE_SCHEMA_PATH:?}"

dnf update -y
dnf install -y awscli docker
systemctl enable --now docker

IMDS_TOKEN="$(curl -fsS --connect-timeout 2 --max-time 5 -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token)"
CLICKHOUSE_BIND_ADDRESS="$(curl -fsS --connect-timeout 2 --max-time 5 \
    -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" \
    http://169.254.169.254/latest/meta-data/local-ipv4)"
unset IMDS_TOKEN

SECRET_JSON="$(
    aws secretsmanager get-secret-value \
        --region "${AWS_REGION}" \
        --secret-id "${CLICKHOUSE_CREDENTIALS_SECRET_ARN}" \
        --query SecretString \
        --output text
)"
CLICKHOUSE_USER="$(
    printf '%s' "${SECRET_JSON}" |
        python3 -c 'import json, sys; print(json.load(sys.stdin)["username"])'
)"
CLICKHOUSE_PASSWORD="$(
    printf '%s' "${SECRET_JSON}" |
        python3 -c 'import json, sys; print(json.load(sys.stdin)["password"])'
)"
unset SECRET_JSON

if [[ ! "${CLICKHOUSE_USER}" =~ ^[A-Za-z_][A-Za-z0-9_.-]*$ ]]; then
    echo "ClickHouse username is invalid." >&2
    exit 1
fi
if [[ ! "${CLICKHOUSE_BIND_ADDRESS}" =~ ^10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
    echo "ClickHouse bind address must be a private 10/8 IPv4 address." >&2
    exit 1
fi

CLICKHOUSE_PASSWORD_SHA256="$(printf '%s' "${CLICKHOUSE_PASSWORD}" | sha256sum | awk '{ print $1 }')"
unset CLICKHOUSE_PASSWORD

mkdir -p /var/lib/clickhouse
mkdir -p /opt/loop-ad/clickhouse/users.d
mkdir -p /opt/loop-ad/clickhouse/config.d

CLICKHOUSE_USER_CONFIG=/opt/loop-ad/clickhouse/users.d/loopad-ingest.xml
cat > "${CLICKHOUSE_USER_CONFIG}" <<EOF
<clickhouse>
  <users>
    <default>
      <networks replace="replace">
        <ip>127.0.0.1</ip>
        <ip>::1</ip>
      </networks>
    </default>
    <${CLICKHOUSE_USER}>
      <password_sha256_hex>${CLICKHOUSE_PASSWORD_SHA256}</password_sha256_hex>
      <networks>
        <ip>${CLICKHOUSE_ALLOWED_CIDR}</ip>
      </networks>
      <profile>default</profile>
      <quota>default</quota>
    </${CLICKHOUSE_USER}>
  </users>
</clickhouse>
EOF
unset CLICKHOUSE_PASSWORD_SHA256
# The server process runs as the container's clickhouse user. The file contains
# only a SHA-256 password hash, so make the read-only bind mount readable there.
chmod 644 "${CLICKHOUSE_USER_CONFIG}"

CLICKHOUSE_ASYNC_LOG_CONFIG=/opt/loop-ad/clickhouse/config.d/asynchronous-insert-log.xml
cat > "${CLICKHOUSE_ASYNC_LOG_CONFIG}" <<'EOF'
<clickhouse>
  <asynchronous_insert_log>
    <database>system</database>
    <table>asynchronous_insert_log</table>
    <flush_interval_milliseconds>1000</flush_interval_milliseconds>
  </asynchronous_insert_log>
</clickhouse>
EOF
chmod 644 "${CLICKHOUSE_ASYNC_LOG_CONFIG}"

if docker ps -a --format '{{.Names}}' | grep -qx 'phase4-clickhouse'; then
    docker rm -f phase4-clickhouse
fi

docker run -d \
    --restart unless-stopped \
    --name phase4-clickhouse \
    --log-opt max-size=100m \
    --log-opt max-file=3 \
    -p "${CLICKHOUSE_BIND_ADDRESS}:${CLICKHOUSE_HTTP_PORT}:8123" \
    -v /var/lib/clickhouse:/var/lib/clickhouse \
    -v "${CLICKHOUSE_USER_CONFIG}:/etc/clickhouse-server/users.d/loopad-ingest.xml:ro" \
    -v "${CLICKHOUSE_ASYNC_LOG_CONFIG}:/etc/clickhouse-server/config.d/asynchronous-insert-log.xml:ro" \
    "${CLICKHOUSE_IMAGE}"

for _ in {1..90}; do
    if docker exec phase4-clickhouse clickhouse-client --query 'SELECT 1' >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

if ! docker exec phase4-clickhouse clickhouse-client --query 'SELECT 1' >/dev/null 2>&1; then
    docker logs --tail 120 phase4-clickhouse >&2
    exit 1
fi

docker exec -i phase4-clickhouse clickhouse-client --multiquery < "${CLICKHOUSE_SCHEMA_PATH}"
docker exec phase4-clickhouse clickhouse-client --query \
    "SELECT throwIf(count() != 2) FROM system.tables WHERE database = 'loopad' AND name IN ('events', 'raw_events')"

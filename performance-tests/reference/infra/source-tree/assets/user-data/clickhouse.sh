#!/bin/bash
set -euo pipefail

: "${CLICKHOUSE_DATABASE:?}"
: "${CLICKHOUSE_CREDENTIALS_SECRET_NAME:?}"
: "${CLICKHOUSE_HTTP_PORT:?}"
: "${CLICKHOUSE_IMAGE:?}"
: "${AWS_REGION:?}"

# 패키지와 Docker는 인스턴스 재생성 시마다 최신 AL2023 기준으로 준비합니다.
# ClickHouse 자체 버전은 CDK env로 받은 이미지 태그가 고정하므로 OS 업데이트와 분리됩니다.
dnf update -y
dnf install -y awscli docker
systemctl enable --now docker

secret_json_field() {
    local secret_name="$1"
    local field_name="$2"

    aws secretsmanager get-secret-value \
        --region "${AWS_REGION}" \
        --secret-id "${secret_name}" \
        --query SecretString \
        --output text |
        python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "${field_name}"
}

CLICKHOUSE_USER="$(secret_json_field "${CLICKHOUSE_CREDENTIALS_SECRET_NAME}" username)"
CLICKHOUSE_PASSWORD="$(secret_json_field "${CLICKHOUSE_CREDENTIALS_SECRET_NAME}" password)"

# 데이터 디렉터리는 호스트 EBS에 두어 컨테이너 재시작과 이미지 교체 뒤에도 유지합니다.
# EBS deleteOnTermination은 CDK에서 관리하므로 stack destroy 시 dev 데이터는 함께 제거됩니다.
mkdir -p /var/lib/clickhouse
mkdir -p /opt/loop-ad/clickhouse/users.d

if [[ ! "${CLICKHOUSE_DATABASE}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "CLICKHOUSE_DATABASE must be a simple SQL identifier." >&2
    exit 1
fi

if [[ ! "${CLICKHOUSE_USER}" =~ ^[A-Za-z_][A-Za-z0-9_.-]*$ ]]; then
    echo "CLICKHOUSE_USER must be safe as a ClickHouse XML user element." >&2
    exit 1
fi

CLICKHOUSE_PASSWORD_SHA256="$(printf '%s' "${CLICKHOUSE_PASSWORD}" | sha256sum | awk '{ print $1 }')"
CLICKHOUSE_USER_CONFIG=/opt/loop-ad/clickhouse/users.d/loopad-user.xml

# The Docker entrypoint writes CLICKHOUSE_PASSWORD into XML without escaping every
# password shape ClickHouse accepts. Generate the user config ourselves and store
# only the hash so credentials containing XML metacharacters still boot cleanly.
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
        <ip>::/0</ip>
      </networks>
      <profile>default</profile>
      <quota>default</quota>
      <access_management>1</access_management>
      <named_collection_control>1</named_collection_control>
    </${CLICKHOUSE_USER}>
  </users>
</clickhouse>
EOF
chmod 644 "${CLICKHOUSE_USER_CONFIG}"

# user-data가 재실행되거나 인스턴스가 교체 중 재시도되어도 같은 이름의 컨테이너 충돌을 피합니다.
if docker ps -a --format '{{.Names}}' | grep -qx 'clickhouse-server'; then
    docker rm -f clickhouse-server
fi

# HTTP 포트만 외부로 열고 credentials는 위에서 만든 users.d config로만 주입합니다.
docker run -d \
    --restart unless-stopped \
    --name clickhouse-server \
    -p "${CLICKHOUSE_HTTP_PORT}:8123" \
    -v /var/lib/clickhouse:/var/lib/clickhouse \
    -v "${CLICKHOUSE_USER_CONFIG}:/etc/clickhouse-server/users.d/loopad-user.xml:ro" \
    "${CLICKHOUSE_IMAGE}"

for _ in {1..60}; do
    if docker exec clickhouse-server clickhouse-client --query 'SELECT 1' >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

if ! docker exec clickhouse-server clickhouse-client --query 'SELECT 1' >/dev/null 2>&1; then
    docker logs --tail 120 clickhouse-server >&2
    exit 1
fi

docker exec clickhouse-server clickhouse-client --query "CREATE DATABASE IF NOT EXISTS \`${CLICKHOUSE_DATABASE}\`"
docker exec clickhouse-server clickhouse-client \
    --user "${CLICKHOUSE_USER}" \
    --password "${CLICKHOUSE_PASSWORD}" \
    --query "SELECT 1" >/dev/null

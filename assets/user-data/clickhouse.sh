#!/bin/bash
set -euo pipefail

: "${CLICKHOUSE_DATABASE:?}"
: "${CLICKHOUSE_HTTP_PORT:?}"
: "${CLICKHOUSE_IMAGE:?}"
: "${CLICKHOUSE_PASSWORD:?}"
: "${CLICKHOUSE_USER:?}"

# 패키지와 Docker는 인스턴스 재생성 시마다 최신 AL2023 기준으로 준비합니다.
# ClickHouse 자체 버전은 CDK env로 받은 이미지 태그가 고정하므로 OS 업데이트와 분리됩니다.
dnf update -y
dnf install -y docker
systemctl enable --now docker

# 데이터 디렉터리는 호스트 EBS에 두어 컨테이너 재시작과 이미지 교체 뒤에도 유지합니다.
# EBS deleteOnTermination은 CDK에서 관리하므로 stack destroy 시 dev 데이터는 함께 제거됩니다.
mkdir -p /var/lib/clickhouse

# user-data가 재실행되거나 인스턴스가 교체 중 재시도되어도 같은 이름의 컨테이너 충돌을 피합니다.
if docker ps -a --format '{{.Names}}' | grep -qx 'clickhouse-server'; then
    docker rm -f clickhouse-server
fi

# HTTP 포트만 외부로 열고 database/user/password는 CDK가 전달한 Secrets Manager dynamic reference 결과를 사용합니다.
docker run -d \
    --restart unless-stopped \
    --name clickhouse-server \
    -p "${CLICKHOUSE_HTTP_PORT}:8123" \
    -v /var/lib/clickhouse:/var/lib/clickhouse \
    -e CLICKHOUSE_DB="${CLICKHOUSE_DATABASE}" \
    -e CLICKHOUSE_USER="${CLICKHOUSE_USER}" \
    -e CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD}" \
    "${CLICKHOUSE_IMAGE}"

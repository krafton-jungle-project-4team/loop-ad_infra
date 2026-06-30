#!/bin/bash
set -euo pipefail

: "${APP_USER_SECRET_NAME:?}"
: "${AWS_REGION:?}"
: "${BROKER_USER_SECRET_NAME:?}"
: "${EVENT_TOPIC_NAME:?}"
: "${KAFKA_HEAP_OPTS:?}"
: "${KAFKA_SASL_MECHANISM:?}"
: "${KAFKA_SCALA_VERSION:?}"
: "${KAFKA_SCRAM_PORT:?}"
: "${KAFKA_SECURITY_PROTOCOL:?}"
: "${KAFKA_VERSION:?}"

# Kafka는 JVM만 필요하므로 headless Corretto와 압축 도구만 설치합니다.
# broker binary 버전은 CDK env로 고정해 인스턴스 재생성 시에도 같은 Kafka 버전이 올라가게 합니다.
dnf update -y
dnf install -y awscli java-17-amazon-corretto-headless tar gzip

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

APP_USERNAME="$(secret_json_field "${APP_USER_SECRET_NAME}" username)"
APP_PASSWORD="$(secret_json_field "${APP_USER_SECRET_NAME}" password)"
BROKER_USERNAME="$(secret_json_field "${BROKER_USER_SECRET_NAME}" username)"
BROKER_PASSWORD="$(secret_json_field "${BROKER_USER_SECRET_NAME}" password)"

# Kafka 프로세스를 전용 system user로 실행해 broker 파일 권한과 서비스 권한을 분리합니다.
useradd --system --home-dir /opt/kafka --shell /sbin/nologin kafka || true
mkdir -p /opt/kafka /var/lib/kafka /var/log/kafka

# dev 단일 broker라 archive.apache.org에서 지정 버전을 내려받아 로컬에 펼칩니다.
# 관리형 MSK 대신 EC2를 쓰는 대신, 설치 절차는 이 스크립트에 고정해 재현성을 확보합니다.
curl -fL "https://archive.apache.org/dist/kafka/${KAFKA_VERSION}/kafka_${KAFKA_SCALA_VERSION}-${KAFKA_VERSION}.tgz" -o /tmp/kafka.tgz
tar -xzf /tmp/kafka.tgz --strip-components=1 -C /opt/kafka

# public subnet 구조라 클라이언트가 접근할 advertised listener에는 인스턴스 public DNS가 필요합니다.
# IMDSv2 토큰을 사용해 메타데이터 접근을 명시적으로 제한합니다.
TOKEN=$(curl -s -X PUT -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" http://169.254.169.254/latest/api/token)
PUBLIC_DNS=$(curl -s -H "X-aws-ec2-metadata-token: ${TOKEN}" http://169.254.169.254/latest/meta-data/public-hostname)

# KRaft 단일 노드 설정입니다.
# replication factor와 ISR을 1로 두어 dev 비용을 낮추고, topic 자동 생성은 꺼서 infra 계약에 없는 topic 생성을 막습니다.
cat > /opt/kafka/config/kraft/server.properties <<EOF
process.roles=broker,controller
node.id=1
controller.quorum.voters=1@localhost:9093
listeners=${KAFKA_SECURITY_PROTOCOL}://0.0.0.0:${KAFKA_SCRAM_PORT},CONTROLLER://localhost:9093
advertised.listeners=${KAFKA_SECURITY_PROTOCOL}://${PUBLIC_DNS}:${KAFKA_SCRAM_PORT}
listener.security.protocol.map=${KAFKA_SECURITY_PROTOCOL}:${KAFKA_SECURITY_PROTOCOL},CONTROLLER:PLAINTEXT
inter.broker.listener.name=${KAFKA_SECURITY_PROTOCOL}
controller.listener.names=CONTROLLER
sasl.enabled.mechanisms=${KAFKA_SASL_MECHANISM}
sasl.mechanism.inter.broker.protocol=${KAFKA_SASL_MECHANISM}
log.dirs=/var/lib/kafka
num.partitions=1
default.replication.factor=1
min.insync.replicas=1
offsets.topic.replication.factor=1
transaction.state.log.replication.factor=1
transaction.state.log.min.isr=1
group.initial.rebalance.delay.ms=0
auto.create.topics.enable=false
log.retention.hours=168
EOF

# broker 내부 인증 정보는 broker user secret에서 오며 KafkaServer JAAS에만 사용합니다.
cat > /opt/kafka/config/kafka_server_jaas.conf <<EOF
KafkaServer {
  org.apache.kafka.common.security.scram.ScramLoginModule required username="${BROKER_USERNAME}" password="${BROKER_PASSWORD}";
};
EOF

# topic 생성 같은 로컬 관리 명령도 SCRAM 인증을 거치도록 같은 broker user 설정을 사용합니다.
cat > /opt/kafka/config/client.properties <<EOF
security.protocol=${KAFKA_SECURITY_PROTOCOL}
sasl.mechanism=${KAFKA_SASL_MECHANISM}
sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="${BROKER_USERNAME}" password="${BROKER_PASSWORD}";
EOF

chmod 600 /opt/kafka/config/kafka_server_jaas.conf /opt/kafka/config/client.properties
chown -R kafka:kafka /opt/kafka /var/lib/kafka /var/log/kafka

# storage format 시점에 broker user와 app user를 모두 SCRAM credential로 등록합니다.
# app 컨테이너에는 app user만 주입해 broker 관리 credential이 런타임 task로 넘어가지 않게 합니다.
CLUSTER_ID=$(/opt/kafka/bin/kafka-storage.sh random-uuid)
runuser -u kafka -- /opt/kafka/bin/kafka-storage.sh format \
    -t "${CLUSTER_ID}" \
    -c /opt/kafka/config/kraft/server.properties \
    --ignore-formatted \
    --add-scram "${KAFKA_SASL_MECHANISM}=[name=${BROKER_USERNAME},password=${BROKER_PASSWORD}]" \
    --add-scram "${KAFKA_SASL_MECHANISM}=[name=${APP_USERNAME},password=${APP_PASSWORD}]"

# systemd로 broker를 관리해 인스턴스 재부팅 뒤에도 자동 복구되게 합니다.
# heap option은 CDK 설정으로 넘겨 EC2 크기 변경 시 함께 조정할 수 있습니다.
cat > /etc/systemd/system/kafka.service <<EOF
[Unit]
Description=Loop Ad dev Kafka broker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kafka
Group=kafka
Environment="KAFKA_HEAP_OPTS=${KAFKA_HEAP_OPTS}"
Environment="KAFKA_OPTS=-Djava.security.auth.login.config=/opt/kafka/config/kafka_server_jaas.conf"
ExecStart=/opt/kafka/bin/kafka-server-start.sh /opt/kafka/config/kraft/server.properties
ExecStop=/opt/kafka/bin/kafka-server-stop.sh
Restart=on-failure
RestartSec=10
LimitNOFILE=100000

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now kafka
sleep 20

# infra config에서 정의한 raw event topic 하나만 생성합니다.
# 앱이 topic 이름을 env로 받기 때문에 코드와 broker 초기화 계약이 같은 값으로 유지됩니다.
runuser -u kafka -- /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server "localhost:${KAFKA_SCRAM_PORT}" \
    --command-config /opt/kafka/config/client.properties \
    --create \
    --if-not-exists \
    --topic "${EVENT_TOPIC_NAME}" \
    --partitions 1 \
    --replication-factor 1

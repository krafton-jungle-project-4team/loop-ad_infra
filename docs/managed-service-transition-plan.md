# 관리형 서비스 전환 계획

현재 개발 구조는 월 $200-$300 비용 목표를 우선해 ClickHouse와 Kafka를 단일 EC2로 운영하고, Aurora와 Valkey는 관리형 서버리스 구성을 사용한다. 이 문서는 향후 ClickHouse, Kafka/MSK, cache, DB를 더 높은 운영 안정성의 관리형 구성으로 전환할 때 앱 코드를 크게 다시 쓰지 않기 위한 계약과 검증 절차를 정의한다.

## 반드시 유지할 계약

관리형 전환의 핵심 기준은 앱 코드를 갈아엎지 않고 일부 construct, 설정, 계약 변경만으로 전환 가능한지다.

- 엔드포인트 계약은 SSM parameter 이름을 유지하거나 좁은 이름 변경 계획을 둔다.
- ECS service 환경 변수 이름은 유지한다.
- 보안 그룹 경계는 `serverSecurityGroup` -> `dataStorageSecurityGroup` 신뢰 모델을 유지하거나 service별 SG로 더 좁히는 변경만 허용한다.
- 상태 저장 서비스는 `LoopAdDevDataStack` 또는 후속 data stack에 남기고 런타임 스택이 구현 타입을 알지 않게 한다.
- 관리형 전환 전후로 앱 repository는 AWS resource type, CloudFormation logical ID, CDK construct ID를 참조하지 않는다.
- `cdk diff`는 사용자 승인 전 실행하지 않으며, 배포 전 diff에서 상태 저장 리소스 교체가 보이면 전환을 중단한다.

## 공통 검증 게이트

모든 관리형 전환 후보는 아래 게이트를 통과해야 한다.

1. 성능 테스트
   - 최소 1시간의 지속 부하 테스트와 15분의 burst 테스트를 별도로 실행한다.
   - p50, p95, p99 지연 시간과 오류율을 기록한다.
   - ECS task CPU/memory, NAT data processing, LB target health, CloudWatch Logs 오류율을 함께 기록한다.

2. 비용 검증
   - AWS Pricing Calculator 또는 Price List API로 월 730시간 기준 추정치를 만든다.
   - 예상 개발 사용량, burst 사용량, 최악의 sustained 사용량을 분리한다.
   - 관리형 전환 후 전체 개발 월 비용이 $1200 이하인지 확인한다.
   - 실제 PoC 배포 후에는 Cost Explorer로 최소 7일 actual/forecast를 확인한다.

3. 롤백
   - 기존 EC2/self-managed endpoint SSM parameter 값을 복구하는 절차가 있어야 한다.
   - data copy가 필요한 경우 read-only window와 cutback 절차를 둔다.
   - 롤백 중 앱 환경 변수 이름은 바꾸지 않는다.

4. 마이그레이션 위험
   - schema compatibility, data loss window, replay 가능성, dual-write 필요 여부를 기록한다.
   - destructive migration은 개발 환경에서도 snapshot/export 후에만 허용한다.

5. CDK 변경 범위
   - 변경 대상 stack, construct, SSM parameter, security group rule, IAM grant를 명시한다.
   - logical ID rename과 resource replacement 위험을 별도로 기록한다.

## Kafka에서 MSK로 전환

현재 구성:

- `LoopAdDevDataStack`의 EC2 `t4g.small` 단일 Kafka broker
- SSM: `/loop-ad/dev/kafka/bootstrap-brokers`
- ECS env: `LOOPAD_KAFKA_BOOTSTRAP_BROKERS`

전환 후보:

- Amazon MSK Provisioned 또는 MSK Serverless
- 동일한 SSM parameter와 ECS 환경 변수 이름
- `dataStorageSecurityGroup`이 `serverSecurityGroup`에서 들어오는 broker 트래픽을 허용하는 모델

성능 테스트:

- Event Collector의 producer 처리량
- downstream event consumer의 lag와 recovery
- `ingest.dev.loop-ad.org` 기준 burst ingest test
- `loop-ad.events.raw` topic retention과 replay test

월 $1200 이하 검증:

- broker-hours, storage, partition count, data transfer, CloudWatch metrics/logging 비용을 추정한다.
- MSK Serverless와 허용 가능한 최소 provisioned cluster를 비교한다.
- client 또는 관리 호출이 VPC 밖으로 나가는 경우 NAT 비용 영향을 포함한다.

롤백:

- MSK가 7일 관찰을 통과할 때까지 data 설정 뒤에 EC2 Kafka stack code path를 남긴다.
- `/loop-ad/dev/kafka/bootstrap-brokers`를 EC2 private DNS broker string으로 복구한다.
- client가 자동 재연결하지 못하는 경우에만 Kafka client를 가진 ECS 서비스를 재시작한다.

마이그레이션 위험:

- Topic metadata, offsets, retention은 단일 노드 KRaft에서 MSK로 깔끔하게 이전되지 않을 수 있다.
- 가능하면 source event fixture에서 consumer replay를 사용한다.
- offset continuity가 필요하면 cutover 전에 MirrorMaker 2 또는 app-level replay를 테스트한다.

CDK 변경 범위:

- `KafkaInstance` construct block을 MSK construct/config branch로 교체한다.
- SSM parameter logical contract를 보존한다.
- 보안 그룹 포트를 EC2 Kafka plaintext에서 선택한 MSK listener에 맞게 갱신한다.
- 런타임 ECS 리소스는 이동하지 않는다.

## ClickHouse에서 관리형 분석 저장소로 전환

현재 구성:

- `LoopAdDevDataStack`의 EC2 `t4g.small` ClickHouse container
- SSM: `/loop-ad/dev/clickhouse/endpoint`
- ECS env: `LOOPAD_CLICKHOUSE_URL`, `LOOPAD_CLICKHOUSE_USERNAME`

전환 후보:

- private connectivity를 사용할 수 있다면 ClickHouse Cloud, 아니면 workload test 후 선택한 다른 관리형 분석 저장소
- protocol compatibility가 유지되는 경우 동일한 앱 환경 변수 이름

성능 테스트:

- Dashboard query p95/p99 지연 시간
- ClickHouse write/backfill 처리량
- aggregated context data backfill test
- idle 이후 cold-start query behavior

월 $1200 이하 검증:

- compute size, storage, backup, data transfer, private connectivity 비용을 추정한다.
- analytics store는 scale-to-zero가 되지 않을 수 있으므로 최소 상시 비용을 포함한다.
- 예상 개발 data retention이 storage assumption을 넘지 않는지 확인한다.

롤백:

- cutover 전에 EC2 ClickHouse EBS snapshot/export를 남긴다.
- `/loop-ad/dev/clickhouse/endpoint`를 EC2 private DNS URL로 복구한다.
- test window 중 write가 발생했다면 schema bootstrap과 aggregation data replay를 다시 실행한다.

마이그레이션 위험:

- SQL dialect와 engine setting은 관리형 provider마다 다를 수 있다.
- Materialized view, compression, TTL setting에 schema 변경이 필요할 수 있다.
- Data export/import가 rollback window보다 오래 걸릴 수 있다.

CDK 변경 범위:

- `ClickHouseInstance` construct block을 교체하거나 data endpoint provider config를 도입한다.
- protocol이 바뀌지 않는 한 `LOOPAD_CLICKHOUSE_URL`을 보존한다.
- generated assets와 런타임 스택은 변경하지 않는다.

## Valkey에서 대체 Cache로 전환

현재 구성:

- ElastiCache Serverless for Valkey
- SSM: `/loop-ad/dev/redis/endpoint`
- ECS env: `LOOPAD_REDIS_URL`

전환 후보:

- Valkey Serverless 유지, provisioned ElastiCache replication group 전환, 또는 다른 Redis-compatible managed cache

성능 테스트:

- Cache hit/miss 지연 시간
- Fargate task의 TLS connection churn
- burst traffic에서 ECPU 또는 node CPU
- provisioned replication group을 선택한 경우 failover behavior

월 $1200 이하 검증:

- serverless storage/ECPU와 provisioned node-hours, reserved capacity option을 비교한다.
- multi-AZ가 활성화되는 경우 cross-AZ data transfer를 포함한다.

롤백:

- `/loop-ad/dev/redis/endpoint`를 이전 `rediss://` endpoint로 복구한다.
- cache data는 rebuild 가능하므로 app semantics가 바뀌지 않는 한 durable rollback은 필요 없다.

마이그레이션 위험:

- TLS requirements, AUTH setting, eviction policy, cluster mode compatibility가 다를 수 있다.
- cutover 중 cache flush가 발생하면 DB load spike가 생길 수 있다.

CDK 변경 범위:

- `LOOPAD_REDIS_URL`과 SSM parameter를 안정적으로 유지한다.
- `ValkeyServerlessCache`를 선택한 cache construct/config branch로 교체한다.
- service-specific SG를 도입할 때 Redis TLS 포트로 SG port를 좁힌다.

## Aurora PostgreSQL 확장 또는 교체

현재 구성:

- Aurora PostgreSQL Serverless v2 min 0 ACU, max 2 ACU, 10 minute auto-pause
- SSM: `/loop-ad/dev/aurora/endpoint`
- ECS 환경 변수: `LOOPAD_AURORA_HOST`, `LOOPAD_AURORA_PORT`, `LOOPAD_AURORA_DATABASE`, secret username/password

전환 후보:

- Aurora Serverless v2 max ACU 증가, provisioned Aurora 전환, 또는 read replica 도입

성능 테스트:

- 대표 dashboard/ad traffic에서 API read/write 지연 시간
- migration runtime과 lock duration
- auto-pause resume 이후 connection pool behavior
- snapshot 기반 backup/restore drill

월 $1200 이하 검증:

- ACU-hours 또는 instance-hours, storage, I/O, backup, Performance Insights 사용 시 해당 비용을 추정한다.
- idle-heavy 개발 profile과 sustained test profile을 비교한다.

롤백:

- validation이 끝날 때까지 snapshot restore를 사용하거나 이전 cluster를 유지한다.
- replacement cluster를 사용하는 경우 `/loop-ad/dev/aurora/endpoint`와 secret ARN을 복구한다.
- ECS service를 redirect하기 전에 schema compatibility check를 실행한다.

마이그레이션 위험:

- Engine version, extension, parameter group, migration locking은 앱에 영향을 줄 수 있다.
- Secret rotation 또는 username 변경은 secret contract가 바뀌는 경우 ECS secret injection을 깨뜨릴 수 있다.

CDK 변경 범위:

- DB는 `LoopAdDevDataStack`에 유지한다.
- ECS 환경 변수 이름과 SSM endpoint parameter를 보존한다.
- secret이 바뀌는 경우 runtime이 소비하는 data stack output/prop만 갱신한다.

## 점수 영향

이 계획을 만족하지 못하는 전환은 CDK 유지보수성 점수를 낮춘다. 특히 SSM/env 계약을 바꾸거나 런타임 스택을 크게 다시 쓰거나 앱 코드가 broker/cache/database 구현 타입을 직접 알게 되면 관리형 전환 가능성이 낮은 것으로 평가한다.

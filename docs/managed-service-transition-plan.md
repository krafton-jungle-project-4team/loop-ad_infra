# Managed Service Transition Plan

현재 dev 구조는 월 $200-$300 비용 목표를 우선해 ClickHouse와 Kafka를 단일 EC2로 운영하고, Aurora와 Valkey는 관리형 serverless를 사용한다. 이 문서는 향후 ClickHouse, Kafka/MSK, cache, DB를 더 높은 운영 안정성의 관리형 구성으로 전환할 때 앱 코드를 크게 다시 쓰지 않기 위한 contract와 검증 절차를 정의한다.

## Non-Negotiable Contracts

관리형 전환의 핵심 기준은 앱 코드를 갈아엎지 않고 일부 construct, config, contract 변경만으로 전환 가능한지다.

- Endpoint contract는 SSM parameter 이름을 유지하거나 좁은 rename plan을 둔다.
- ECS service env 이름은 유지한다.
- Security group boundary는 `serverSecurityGroup` -> `dataStorageSecurityGroup` trust 모델을 유지하거나 service별 SG로 좁히는 변경만 허용한다.
- Stateful service는 `LoopAdDevDataStack` 또는 후속 data stack에 남기고 runtime stack이 구현 타입을 알지 않게 한다.
- 관리형 전환 전후로 앱 repository는 AWS resource type, CloudFormation logical ID, CDK construct ID를 참조하지 않는다.
- `cdk diff`는 사용자 승인 전 실행하지 않으며, 배포 전 diff에서 stateful replacement가 보이면 전환을 중단한다.

## Shared Verification Gates

모든 관리형 전환 후보는 아래 gate를 통과해야 한다.

1. Performance test
   - 최소 1시간 sustained test와 15분 burst test를 별도로 실행한다.
   - p50, p95, p99 latency와 error rate를 기록한다.
   - ECS task CPU/memory, NAT data processing, LB target health, CloudWatch Logs error rate를 같이 기록한다.

2. Cost verification
   - AWS Pricing Calculator 또는 Price List API로 월 730시간 기준 estimate를 만든다.
   - expected dev usage, burst usage, worst sustained usage를 분리한다.
   - managed 전환 후 전체 dev 월 비용이 $1200 이하인지 확인한다.
   - 실제 PoC 배포 후에는 Cost Explorer로 최소 7일 actual/forecast를 확인한다.

3. Rollback
   - 기존 EC2/self-managed endpoint SSM parameter 값을 복구하는 절차가 있어야 한다.
   - data copy가 필요한 경우 read-only window와 cutback 절차를 둔다.
   - rollback 중 앱 env 이름은 바꾸지 않는다.

4. Migration risk
   - schema compatibility, data loss window, replay 가능성, dual-write 필요 여부를 기록한다.
   - destructive migration은 dev에서도 snapshot/export 후에만 허용한다.

5. CDK scope
   - 변경 대상 stack, construct, SSM parameter, security group rule, IAM grant를 명시한다.
   - logical ID rename과 resource replacement 위험을 별도로 기록한다.

## Kafka to MSK

Current:

- EC2 `t4g.small` single Kafka broker in `LoopAdDevDataStack`
- SSM: `/loop-ad/dev/kafka/bootstrap-brokers`
- ECS env: `LOOPAD_KAFKA_BOOTSTRAP_BROKERS`

Target option:

- Amazon MSK Provisioned 또는 MSK Serverless
- Same SSM parameter and ECS env name
- `dataStorageSecurityGroup` allows broker traffic from `serverSecurityGroup`

Performance tests:

- Producer throughput from Event Collector
- Consumer lag and recovery for Ad Context Projector
- Burst ingest test on `ingest.dev.loop-ad.org`
- Topic retention and replay test for `loop-ad.events.raw`

Monthly $1200 verification:

- Estimate broker-hours, storage, partition count, data transfer, and CloudWatch metrics/logging.
- Compare MSK Serverless vs smallest acceptable provisioned cluster.
- Include NAT impact if clients or management calls leave the VPC.

Rollback:

- Keep EC2 Kafka stack code path available behind data config until MSK has passed 7-day observation.
- Restore `/loop-ad/dev/kafka/bootstrap-brokers` to the EC2 private DNS broker string.
- Restart Event Collector and Projector only if clients do not reconnect automatically.

Migration risks:

- Topic metadata, offsets, and retention may not migrate cleanly from single-node KRaft to MSK.
- Use consumer replay from source event fixtures where possible.
- If offset continuity is required, test MirrorMaker 2 or app-level replay before cutover.

CDK scope:

- Replace `KafkaInstance` construct block with an MSK construct/config branch.
- Preserve SSM parameter logical contract.
- Update security group ports from EC2 Kafka plaintext to the selected MSK listener.
- Do not move runtime ECS resources.

## ClickHouse to Managed Analytics Store

Current:

- EC2 `t4g.small` ClickHouse container in `LoopAdDevDataStack`
- SSM: `/loop-ad/dev/clickhouse/endpoint`
- ECS env: `LOOPAD_CLICKHOUSE_URL`, `LOOPAD_CLICKHOUSE_USERNAME`

Target option:

- ClickHouse Cloud via private connectivity if available, or another managed analytics store selected after workload testing.
- Same app env names where protocol compatibility is retained.

Performance tests:

- Dashboard query p95/p99 latency
- Projector write throughput
- Backfill test for aggregated context data
- Cold-start query behavior after idle periods

Monthly $1200 verification:

- Estimate compute size, storage, backup, data transfer, and private connectivity charges.
- Include minimum always-on cost because analytics stores may not scale to zero.
- Verify projected dev data retention does not exceed storage assumptions.

Rollback:

- Keep EC2 ClickHouse EBS snapshot/export before cutover.
- Restore `/loop-ad/dev/clickhouse/endpoint` to the EC2 private DNS URL.
- Re-run schema bootstrap and replay aggregation data if writes occurred during the test window.

Migration risks:

- SQL dialect and engine settings can differ across managed providers.
- Materialized views, compression, and TTL settings may require schema changes.
- Data export/import may be slower than the rollback window allows.

CDK scope:

- Replace `ClickHouseInstance` construct block or introduce a data endpoint provider config.
- Preserve `LOOPAD_CLICKHOUSE_URL` unless the protocol changes.
- Keep generated assets and runtime stack unchanged.

## Valkey to Alternate Cache

Current:

- ElastiCache Serverless for Valkey
- SSM: `/loop-ad/dev/redis/endpoint`
- ECS env: `LOOPAD_REDIS_URL`

Target option:

- Continue Valkey Serverless, switch to provisioned ElastiCache replication group, or use another Redis-compatible managed cache.

Performance tests:

- Cache hit/miss latency
- TLS connection churn from Fargate tasks
- ECPU or node CPU under burst traffic
- Failover behavior if provisioned replication group is selected

Monthly $1200 verification:

- Compare serverless storage/ECPU against provisioned node-hours and reserved capacity options.
- Include cross-AZ data transfer if multi-AZ is enabled.

Rollback:

- Restore `/loop-ad/dev/redis/endpoint` to the previous `rediss://` endpoint.
- Cache data is rebuildable; no durable rollback is required unless app semantics change.

Migration risks:

- TLS requirements, AUTH settings, eviction policy, and cluster mode compatibility may differ.
- Cache flush during cutover may cause DB load spikes.

CDK scope:

- Keep `LOOPAD_REDIS_URL` and SSM parameter stable.
- Replace `ValkeyServerlessCache` with the selected cache construct/config branch.
- Narrow SG ports to Redis TLS when service-specific SGs are introduced.

## Aurora PostgreSQL Scaling or Replacement

Current:

- Aurora PostgreSQL Serverless v2 min 0 ACU, max 2 ACU, 10 minute auto-pause
- SSM: `/loop-ad/dev/aurora/endpoint`
- ECS env: `LOOPAD_AURORA_HOST`, `LOOPAD_AURORA_PORT`, `LOOPAD_AURORA_DATABASE`, secret username/password

Target option:

- Increase Aurora Serverless v2 max ACU, switch to provisioned Aurora, or introduce read replicas.

Performance tests:

- API read/write latency under representative dashboard and ad traffic
- Migration runtime and lock duration
- Connection pool behavior after auto-pause resume
- Backup/restore drill from snapshot

Monthly $1200 verification:

- Estimate ACU-hours or instance-hours, storage, I/O, backup, and Performance Insights if enabled.
- Compare idle-heavy dev profile against sustained test profile.

Rollback:

- Use snapshot restore or keep the previous cluster until validation completes.
- Restore `/loop-ad/dev/aurora/endpoint` and secret ARN if a replacement cluster is used.
- Run schema compatibility checks before redirecting ECS services.

Migration risks:

- Engine version, extensions, parameter groups, and migration locking can affect apps.
- Secret rotation or username changes can break ECS secret injection if the secret contract changes.

CDK scope:

- Keep DB in `LoopAdDevDataStack`.
- Preserve ECS env names and SSM endpoint parameter.
- If the secret changes, update only the data stack output/prop consumed by runtime.

## Score Impact

이 계획을 만족하지 못하는 전환은 CDK 유지보수성 점수를 낮춘다. 특히 SSM/env contract를 바꾸거나 runtime stack을 크게 다시 쓰거나 앱 코드가 broker/cache/database 구현 타입을 직접 알게 되면 관리형 전환 가능성이 낮은 것으로 평가한다.

# Infra Improvement Log

이 문서는 AWS Core `aws-cdk`, `aws-billing-and-cost-management` 관점과 AWS 공식 문서 기준으로 loop-ad CDK 구조를 평가하고 개선한 기록이다.

## Evaluation Criteria

참조 기준:

- AWS CDK Developer Guide: Best practices for developing and deploying cloud infrastructure with the AWS CDK
- AWS CDK Developer Guide: Test AWS CDK applications
- AWS Prescriptive Guidance: Best practices for using the AWS CDK in TypeScript to create IaC projects
- AWS Well-Architected Framework: Cost Optimization Pillar
- AWS CloudFormation Template Reference: `AWS::Budgets::Budget`

점수는 평균으로 통과시키지 않고 각 항목을 독립적으로 평가한다.

## Baseline Assessment

기준선 브랜치: `codex/use-ec2-kafka-dev`

검증:

- `npm run build`: pass
- `npm test`: pass, 4 suites / 19 tests

| 항목 | 점수 | 판단 |
|---|---:|---|
| 비용 적합성 | 82 | NAT 1개, S3 Gateway Endpoint, Aurora Serverless v2 auto-pause, Valkey cap, EC2 ClickHouse/Kafka로 dev 비용 방향은 좋다. 다만 월 $200-$300 목표를 검증하는 deterministic cost model이 없고 budget guardrail이 CDK에 없다. |
| 보안/안전성 | 88 | public ingress는 443으로 제한하고 S3/OAC/SSL/secret 주입은 양호하다. 다만 internal SG가 broad all-traffic이고 Kafka/ClickHouse 관리 포트 및 plaintext Kafka의 dev-only 위험이 문서와 테스트에 더 명확히 고정되어야 한다. |
| 운영 안정성 | 86 | stack 분리, ECS circuit breaker, health check, log retention은 있다. 하지만 budget/cost alert, managed 전환 검증 절차, EC2 data node 복구/롤백 절차가 문서화되지 않았다. |
| CDK 모범사례/유지보수성 | 84 | L2 우선, env/context 검증, stack boundary는 좋다. 그러나 `src/loop-ad-stack.ts`가 1,061줄 단일 파일이고 stateful logical ID 안정성 테스트가 부족하다. 일부 physical name은 contract 목적이 있으나 lifecycle 문서와 blast radius 설명이 더 필요하다. |
| 테스트/문서화 | 84 | Jest fine-grained assertions와 workflow 테스트가 있다. 비용 계산, managed service 전환 계획, stateful logical ID guard, cycle-by-cycle improvement log가 없다. |

Baseline blockers:

- 실제 AWS 배포/Cost Explorer/Price List API 호출 없이 운영 비용을 확정 검증할 수 없다.
- `cdk diff`는 사용자 명시 승인 전 금지되어 있으므로 logical ID 변경 가능성은 unit test와 synth로만 검증한다.
- AWS Budgets, alarms, managed 전환은 CDK 코드와 문서로 준비할 수 있지만 실제 알림 동작은 배포 후 별도 검증이 필요하다.

Initial priority:

1. 비용 적합성: deterministic dev cost model과 budget guardrail을 먼저 추가한다.
2. 테스트/문서화: managed 전환 가능성 및 검증 계획을 명시한다.
3. CDK 모범사례/유지보수성: logical ID guard와 파일 분리를 진행하되 resource logical ID 변경을 피한다.

## Cycle 1 - Cost Guardrail and Test Rebuild

목적:

- 월 $200-$300 dev 운영 목표를 검증할 deterministic cost model을 추가한다.
- 월 $300 budget guardrail을 CDK로 합성 가능하게 만든다.
- 사용자의 요청에 따라 기존 Jest 테스트를 전부 삭제하고 fine-grained assertion 기반으로 다시 작성한다.

변경 파일:

- `src/loop-ad-stack.ts`
- `bin/loop-ad_aws_cdk.ts`
- `package.json`
- `scripts/refuse-deploy.mjs`
- `scripts/estimate-dev-monthly-cost.mjs`
- `test/infra-contract.test.ts`
- `README.md`
- `docs/requirements.md`
- `docs/cost-model.md`
- `docs/infra-improvement-log.md`

검증:

- `npm run build`: pass
- `npm test`: pass, 1 suite / 10 tests
- `node scripts/estimate-dev-monthly-cost.mjs --json`: pass, estimated monthly total $241.13 against $300 budget
- `CDK_DEFAULT_ACCOUNT=123456789012 LOOP_AD_BUDGET_ALERT_EMAIL=alerts@example.test npm run synth:dev-cost-guardrails`: pass

점수 변화:

| 항목 | 이전 | 이후 | 판단 |
|---|---:|---:|---|
| 비용 적합성 | 82 | 91 | 명시 가정 + 스크립트 기반 월 비용 모델, $300 budget, actual/forecasted 알림 contract가 추가되었다. 실제 단가와 알림 동작은 배포 전 Pricing Calculator/Price List API 및 배포 후 Cost Explorer/Budgets로 검증해야 한다. |
| 보안/안전성 | 88 | 89 | secret/env fallback, OIDC workflow, deploy-free infra check, L1 예외 정책을 새 테스트로 다시 고정했다. broad internal SG는 아직 남은 리스크다. |
| 운영 안정성 | 86 | 88 | budget incident 감지 경로와 cost review follow-up이 생겼다. EC2 ClickHouse/Kafka 복구 절차와 관리형 전환 rollback 계획은 아직 부족하다. |
| CDK 모범사례/유지보수성 | 84 | 87 | cost guardrail stack을 lifecycle별로 분리하고 테스트를 재구성했다. 단일 대형 `loop-ad-stack.ts`와 stateful logical ID guard 부족은 계속 남아 있다. |
| 테스트/문서화 | 84 | 90 | 기존 테스트를 모두 제거하고 CDK/resource/safety/cost contract 중심의 새 테스트로 교체했다. managed 전환 문서는 아직 별도 보강이 필요하다. |

관리형 전환 가능성 평가:

- Kafka/MSK, ClickHouse, Valkey, Aurora의 endpoint contract는 SSM parameter와 ECS env로 노출되어 있어 앱 코드가 AWS resource type에 직접 의존하지 않는 방향은 유지된다.
- 이번 cycle은 비용 guardrail 중심이라 managed 전환 절차 자체는 아직 상세화하지 않았다.

남은 리스크:

- 실제 AWS 배포 없이 budget email confirmation과 Cost Explorer actual 비용은 검증할 수 없다.
- sustained load에서 Aurora ACU와 ECS task가 장기간 상한에 머물면 $300 budget을 초과할 수 있으므로 budget alert를 incident로 취급해야 한다.
- 관리형 전환의 성능 테스트, 월 $1200 이하 검증, rollback, 데이터 마이그레이션 위험, CDK 변경 범위 문서가 필요하다.

## Cycle 2 - Managed Transition Plan and Logical ID Guard

목적:

- ClickHouse, Kafka/MSK, cache, DB 관리형 전환 가능성을 contract 기준으로 문서화한다.
- 전환이 앱 리라이트가 아니라 SSM/env/security group/stack boundary의 좁은 변경으로 가능한지 평가할 기준을 만든다.
- stateful resource logical ID를 테스트로 고정해 이후 파일 분리/refactor의 replacement 위험을 줄인다.

변경 파일:

- `docs/managed-service-transition-plan.md`
- `README.md`
- `docs/requirements.md`
- `test/infra-contract.test.ts`
- `docs/infra-improvement-log.md`

검증:

- `npm run build`: pass
- `npm test`: pass, 1 suite / 12 tests

점수 변화:

| 항목 | 이전 | 이후 | 판단 |
|---|---:|---:|---|
| 비용 적합성 | 91 | 91 | 비용 모델과 budget guardrail은 유지된다. 관리형 전환 시 월 $1200 이하 검증 gate를 추가했다. |
| 보안/안전성 | 89 | 90 | 전환 중 SSM/env contract와 SG boundary 유지 조건을 명시했다. broad internal SG 자체는 아직 남아 있지만 dev-only 의도와 전환 gate가 명확해졌다. |
| 운영 안정성 | 88 | 91 | performance test, rollback, migration risk, 7일 observation, Cost Explorer 검증 절차가 추가되어 운영 전환 판단 기준이 생겼다. |
| CDK 모범사례/유지보수성 | 87 | 92 | stateful logical ID 테스트와 stack boundary/contract 기반 전환 기준이 추가되었다. 단일 대형 파일과 반복 runtime service 정의는 아직 95점 기준에 부족하다. |
| 테스트/문서화 | 90 | 94 | 관리형 전환 필수 gate와 logical ID guard가 테스트로 검증된다. |

관리형 전환 가능성 평가:

- Kafka/MSK: SSM `/loop-ad/dev/kafka/bootstrap-brokers`와 `LOOPAD_KAFKA_BOOTSTRAP_BROKERS`를 유지하면 data stack 내부 construct/config 교체로 전환 가능하다. offset/topic migration은 주요 risk다.
- ClickHouse: `/loop-ad/dev/clickhouse/endpoint`와 `LOOPAD_CLICKHOUSE_URL` 유지 시 runtime 변경 없이 전환 가능하다. SQL dialect/schema compatibility가 risk다.
- Cache: `LOOPAD_REDIS_URL`과 `/loop-ad/dev/redis/endpoint` 유지 시 Redis-compatible cache 교체가 좁은 변경으로 가능하다.
- DB: Aurora endpoint/secret contract 유지 시 scaling/replacement가 data stack 중심 변경으로 가능하다.

남은 리스크:

- 단일 `src/loop-ad-stack.ts`가 여전히 크고, runtime service 생성 중복이 많아 CDK 유지보수성 95 기준에는 미달한다.
- service-specific SG로 좁히는 보안 개선은 blast radius가 있어 별도 diff 승인 전에는 적용하지 않았다.

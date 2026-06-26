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

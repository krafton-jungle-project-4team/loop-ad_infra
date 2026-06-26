# Dev Cost Model

이 문서는 loop-ad dev 환경이 월 $200-$300 범위를 벗어나지 않도록 검토하는 로컬 비용 모델과 budget guardrail을 설명한다.

## Guardrail

`LoopAdDevCostGuardrailStack`은 `us-east-1`에 `AWS::Budgets::Budget`을 합성한다.

- Budget name: `loop-ad-dev-monthly-budget`
- Limit: 월 $300
- Alerts:
  - actual cost > 80%
  - actual cost > 100%
  - forecasted cost > 100%
- Subscriber: `LOOP_AD_BUDGET_ALERT_EMAIL`

Budget email subscriber는 배포 후 AWS Budgets confirmation을 수락해야 한다. confirmation 전에는 알림 동작이 검증된 것으로 보지 않는다.

## Deterministic Model

로컬 추정은 `npm run cost:dev`로 실행한다.

```bash
npm run cost:dev
node scripts/estimate-dev-monthly-cost.mjs --json
```

계산은 `scripts/estimate-dev-monthly-cost.mjs`의 명시 가정만 사용한다. AWS API를 호출하지 않으므로 실제 과금 데이터가 아니며, 배포 승인 전 AWS Pricing Calculator 또는 Price List API로 단가를 갱신해야 한다.

현재 planning model 결과:

- Monthly budget: $300.00
- Estimated steady dev total: $241.13
- Headroom: $58.87
- Budget utilization: 80.38%

## Cost Review Rules

- NAT Gateway는 1개만 유지하고, S3 Gateway Endpoint로 S3/ECR layer traffic의 NAT data processing을 줄인다.
- ECS 서비스는 steady 1 task, autoscaling max 2 task를 유지한다. 모든 서비스가 한 달 내내 max로 동작하면 budget incident로 본다.
- Aurora Serverless v2는 min 0 ACU, idle 10분 auto-pause를 유지한다. sustained load로 ACU가 장시간 상승하면 Cost Explorer와 CloudWatch로 평균 ACU를 확인한다.
- ClickHouse와 Kafka는 dev 비용 때문에 단일 `t4g.small` EC2로 유지한다. 운영 안정성이 필요한 단계에서는 관리형 전환 계획을 별도로 검증한다.
- CloudWatch Logs는 dev에서 3개월 retention을 유지한다.

## Required Follow-Up After Deployment

- AWS Budgets email confirmation 완료
- 첫 7일 Cost Explorer service breakdown 확인
- 첫 30일 model 대비 actual/forecasted 비용 비교
- NAT data processing, Aurora ACU, Fargate task-hours, load balancer LCU/NLCU, CloudWatch Logs stored bytes를 우선 점검
- 단가 변경이나 사용량 증가가 있으면 `scripts/estimate-dev-monthly-cost.mjs` 가정을 갱신하고 `npm test`를 다시 실행

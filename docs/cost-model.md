# Dev Cost Model

이 문서는 loop-ad dev 환경이 월 $200-$300 범위를 벗어나지 않도록 검토하는 로컬 비용 모델과 비용 알림 운영 방식을 설명한다.

## Alert Ownership

비용 알림은 별도 정기 비용 알림 체계에서 담당한다. 이 CDK app은 AWS Budget 알림 리소스를 생성하지 않는다.

- 월 $300은 인프라 코드가 생성하는 budget resource가 아니라 dev cost target이다.
- 정기 알림에서 NAT, Fargate, Aurora, Valkey, EC2, EBS, Load Balancer, CloudWatch Logs 비용을 우선 확인한다.
- 알림 체계가 비용 초과를 감지하면 `npm run cost`의 가정과 실제 Cost Explorer 값을 비교해 모델을 갱신한다.

## Deterministic Model

로컬 추정은 `npm run cost`로 실행한다.

```bash
npm run cost
node scripts/estimate-dev-monthly-cost.mjs --json
```

계산은 `scripts/estimate-dev-monthly-cost.mjs`의 명시 가정만 사용한다. AWS API를 호출하지 않으므로 실제 과금 데이터가 아니며, 배포 승인 전 AWS Pricing Calculator 또는 Price List API로 단가를 갱신해야 한다.

현재 planning model 결과:

- Monthly target: $300.00
- Estimated steady dev total: $241.13
- Headroom: $58.87
- Target utilization: 80.38%

## Cost Review Rules

- NAT Gateway는 1개만 유지하고, S3 Gateway Endpoint로 S3/ECR layer traffic의 NAT data processing을 줄인다.
- ECS 서비스는 steady 1 task, autoscaling max 2 task를 유지한다. 모든 서비스가 한 달 내내 max로 동작하면 cost incident로 본다.
- Aurora Serverless v2는 min 0 ACU, idle 10분 auto-pause를 유지한다. sustained load로 ACU가 장시간 상승하면 Cost Explorer와 CloudWatch로 평균 ACU를 확인한다.
- ClickHouse와 Kafka는 dev 비용 때문에 단일 `t4g.small` EC2로 유지한다. 운영 안정성이 필요한 단계에서는 관리형 전환 계획을 별도로 검증한다.
- CloudWatch Logs는 dev에서 3개월 retention을 유지한다.

## Required Follow-Up After Deployment

- 별도 정기 비용 알림 수신/대상 계정 확인
- 첫 7일 Cost Explorer service breakdown 확인
- 첫 30일 model 대비 actual/forecasted 비용 비교
- NAT data processing, Aurora ACU, Fargate task-hours, load balancer LCU/NLCU, CloudWatch Logs stored bytes를 우선 점검
- 단가 변경이나 사용량 증가가 있으면 `scripts/estimate-dev-monthly-cost.mjs` 가정을 갱신하고 `npm test`를 다시 실행

# loop-ad 개발 환경 비용 모델

기준일: 2026-06-25

리전은 `ap-northeast-2`입니다. 월 비용은 730시간 기준으로 계산합니다. 아래 금액은 트래픽이 작고, 테스트 사용자가 5명 이내이며, 활성 사용 시간이 하루 12시간 정도라는 전제를 둡니다.

## 설계 가정

- Dev ECS 서비스 5개는 Fargate ARM64 `0.25 vCPU`, `0.5GB`로 시작합니다.
- Dev 서비스는 기본 1 task, CPU 부하 시 서비스별 최대 2 task까지만 확장합니다.
- Dev private subnet은 NAT Gateway 1개를 통해 ECR, CloudWatch Logs, SSM, ECS public API, 외부 SaaS/API를 호출합니다.
- ECR, CloudWatch Logs, SSM, ECS Interface Endpoint 7개는 만들지 않습니다.
- S3 Gateway Endpoint는 유지합니다.
- Dashboard FE와 demo-shoppingmall FE는 private S3 bucket 앞에 CloudFront Price Class 100을 둡니다.
- GenAI 생성물은 private S3 bucket 앞에 CloudFront Price Class 100을 두고 `gen-ai.asset.dev.<public-domain>`으로 공개합니다.
- ClickHouse는 EC2 `t4g.small` 1대와 gp3 50GB로 시작합니다.
- Aurora PostgreSQL은 Serverless v2 Standard mode, `min 0 ACU`, `max 2 ACU`, idle 10분 auto-pause로 시작합니다.
- MSK는 provisioned `kafka.t3.small` 2 brokers와 broker storage 20GB씩으로 시작합니다.
- MSK bootstrap broker 조회용 custom resource는 배포 시 AWS API를 호출하는 용도라서 별도 상시 컴퓨트 비용으로 잡지 않습니다.
- Redis는 아직 provision 방식을 확정하지 않았으므로 이 비용 모델에 포함하지 않습니다.

## 앱 인프라

| 항목 | 단가 가정 | 월 예상 |
|---|---:|---:|
| Fargate ARM 5 tasks | task당 0.25 vCPU, 0.5GB | `$41.45` |
| NAT Gateway 1개 | `$0.059/hour` | `$43.07` |
| ALB + NLB 시간 과금 | 각 `$0.0225/hour` | `$32.85` |
| Public IPv4 보수 추정 | 5개, `$0.005/hour` | `$18.25` |
| ALB/NLB LCU 소량 트래픽 | 각 1 LCU 가정 | `$10.22` |
| CloudWatch Logs | 5GB ingest 가정 | `$3.80` |
| ECR storage | 10GB 가정 | `$1.00` |
| FE CloudFront/S3 GET | dashboard/demo-shoppingmall 소량 테스트 트래픽 | 사용량 기반 소액 |
| GenAI assets CloudFront/S3 GET | 소량 테스트 트래픽 | 사용량 기반 소액 |
| 앱 인프라 소계 | Interface Endpoint 없음 | `$150.64` |

Interface Endpoint 7개를 2개 AZ에 만들면 endpoint hourly 비용만 약 `$132.86/month`가 추가됩니다. NAT Gateway가 이미 필요한 개발 환경에서는 Interface Endpoint를 제거하는 편이 월 비용 목표에 더 유리합니다.

CloudFront는 ALB/NLB처럼 고정 hourly 비용이 없고 요청 수와 data transfer에 따라 비용이 붙습니다. 개발 테스트 트래픽이 작다는 현재 가정에서는 월 합계의 고정 항목에는 넣지 않고, 실제 사용량 기반 소액 부대 비용으로 봅니다.

## 데이터소스

| 항목 | 단가 가정 | 월 예상 |
|---|---:|---:|
| ClickHouse EC2 | `t4g.small`, `$0.0208/hour` | `$15.18` |
| ClickHouse EBS | gp3 50GB, `$0.0912/GB-month` | `$4.56` |
| ClickHouse 소계 | 1대 기준 | `$19.74` |
| Aurora PostgreSQL Serverless v2 | auto-pause, active 0.5 ACU 12시간/일, `$0.20/ACU-hour` | `$36.50` |
| Aurora storage | 20GB, `$0.12/GB-month` | `$2.40` |
| Aurora I/O | 5M requests, `$0.24/M requests` | `$1.20` |
| Aurora 소계 | 12시간/일 active 기준 | `$40.10` |
| MSK broker | `kafka.t3.small` 2 brokers, `$0.0569/broker-hour` | `$83.07` |
| MSK storage | 20GB/broker, `$0.114/GB-month` | `$4.56` |
| MSK 소계 | 2 brokers 기준 | `$87.63` |

Aurora가 idle 시간에 pause되지 않고 0.5 ACU를 24시간 유지하면 Aurora compute만 `$73.00/month`가 되며, Aurora 소계는 약 `$76.60/month`입니다.

## 월 합계

| 시나리오 | 월 예상 |
|---|---:|
| 앱 인프라만, Interface Endpoint 없음 | `$150.64` |
| 앱 인프라 + ClickHouse + Aurora auto-pause 12시간/일 active + MSK | `$298.12` |
| 앱 인프라 + ClickHouse + Aurora 24시간 0.5 ACU + MSK | `$334.62` |

Aurora CDK construct가 생성하는 Secrets Manager secret과 MSK bootstrap 조회 custom resource의 요청/로그 비용은 위 표의 큰 항목에 넣지 않았습니다. 월 수십 센트 수준의 여유 비용이 더해질 수 있으므로, 실제 운영에서는 `$300`에 아주 가깝게 붙어 있다고 보는 편이 안전합니다.

월 `$300` 목표에 맞추려면 Aurora가 실제 idle 시간에 auto-pause되어야 합니다. MSK는 작은 고정 스펙이어도 월 `$87.63` 수준이므로, 사용량이 아주 작다면 향후 Redpanda/EC2 단일 노드 Kafka/managed serverless 대안을 별도로 비교할 가치가 있습니다.

## 가격 출처

- AWS Price List API: `AmazonECS`, `AmazonEC2`, `AmazonVPC`, `AWSELB`, `AmazonCloudWatch`, `AmazonECR`, `AmazonRDS`, `AmazonMSK`
- Region price index: `ap-northeast-2`

# loop-ad 개발 환경 비용 모델

기준일: 2026-06-25

리전은 `ap-northeast-2`입니다. 월 비용은 730시간 기준으로 계산합니다. 아래 금액은 트래픽이 작고, 테스트 사용자가 5명 이내이며, 활성 사용 시간이 하루 12시간 정도라는 전제를 둡니다.

## 설계 가정

- Dev ECS 서비스 5개는 Fargate ARM64 `0.25 vCPU`, `0.5GB`로 시작합니다.
- Dev 서비스는 기본 1 task, CPU 부하 시 서비스별 최대 2 task까지만 확장합니다.
- Dev private subnet은 NAT Gateway 1개를 통해 ECR, CloudWatch Logs, SSM, ECS public API, 외부 SaaS/API를 호출합니다.
- ECR, CloudWatch Logs, SSM, ECS Interface Endpoint 7개는 만들지 않습니다.
- S3 Gateway Endpoint는 유지합니다.
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
| 앱 인프라 소계 | Interface Endpoint 없음 | `$150.64` |

Interface Endpoint 7개를 2개 AZ에 만들면 endpoint hourly 비용만 약 `$132.86/month`가 추가됩니다. NAT Gateway가 이미 필요한 개발 환경에서는 Interface Endpoint를 제거하는 편이 월 비용 목표에 더 유리합니다.

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

## Aggregation Perf 20k RPS 스택 비용 범위

Aggregation perf 스택은 위 월 `$300` 개발 비용 목표에 포함하지 않습니다. 집계 경로 20k RPS / request 1KB 검증을 위한 임시 benchmark 스택이며, 다음 리소스가 테스트 시간 동안 고정 과금됩니다.

| 항목 | Aggregation perf 기본값 |
|---|---:|
| ECS capacity EC2 | `c7g.xlarge` 6대 기본, 최대 12대 |
| Event Collector | 24 tasks 기본, 최대 48 tasks |
| Ad Context Projector | 12 tasks 기본, 최대 24 tasks |
| ClickHouse | EC2 `c7g.xlarge` 1대 + gp3 500GB, 3k IOPS |
| MSK | `kafka.m7g.xlarge` 2 brokers + broker당 200GB |
| MSK topic | `aggregation-events`, 128 partitions, replication factor 2 |

공식 AWS Price List API 기준 온디맨드 단가는 다음으로 계산합니다.

| 항목 | 단가 가정 |
|---|---:|
| EC2 `c7g.xlarge` Linux | `$0.1632/hour` |
| MSK `kafka.m7g.xlarge` broker | `$0.5015/broker-hour` |
| EC2 gp3 storage | `$0.0912/GB-month` |
| EC2 gp3 추가 IOPS | `$0.0057/IOPS-month` |
| MSK storage | `$0.114/GB-month` |
| MSK provisioned storage throughput | `$0.0912/MiBps-month` |
| NLB hourly | `$0.0225/hour` |
| NLB NLCU | `$0.006/NLCU-hour` |
| Public IPv4 | `$0.005/IP-hour` |

20k RPS에서 request 1KB만 NLB를 통과하면 약 72GB/hour이고, response도 1KB라면 약 144GB/hour입니다. NLB는 TCP 기준 processed bytes 1GB/hour당 1 NLCU로 잡히므로 이 비용을 포함합니다.

| 상태 | 요청 1KB만 | 요청 1KB + 응답 1KB |
|---|---:|---:|
| 기본 6대 | 약 `$2.91/hour` | 약 `$3.34/hour` |
| 최대 12대 | 약 `$3.99/hour` | 약 `$4.42/hour` |

| 테스트 시간 | 기본 6대, 요청 1KB만 | 최대 12대, 요청 1KB만 | 기본 6대, 왕복 2KB | 최대 12대, 왕복 2KB |
|---|---:|---:|---:|---:|
| 2시간 | 약 `$5.81` | 약 `$7.98` | 약 `$6.68` | 약 `$8.85` |
| 4시간 | 약 `$11.63` | 약 `$15.97` | 약 `$13.36` | 약 `$17.69` |
| 8시간 | 약 `$23.26` | 약 `$31.93` | 약 `$26.71` | 약 `$35.39` |
| 12시간 | 약 `$34.89` | 약 `$47.90` | 약 `$40.07` | 약 `$53.08` |

이 표는 aggregation perf 인프라 자체 비용입니다. load generator, 대량 CloudWatch Logs, public data transfer는 별도입니다. NLB는 세 차원 중 가장 큰 값으로 과금되며, 20k RPS / 1KB 패턴에서는 connection을 재사용해도 processed bytes 차원이 비용의 주된 항목입니다.

Aggregation perf 결과 백업 S3 bucket은 Dev 스택이 소유하고 `RemovalPolicy.RETAIN`으로 유지합니다. S3 Standard는 50TB 전까지 `$0.025/GB-month`, PUT/LIST는 `$0.0045/1,000 requests` 수준이라 결과 파일이 수 GB/수천 objects 정도면 aggregation perf compute 비용 대비 작습니다.

따라서 aggregation perf 스택은 켜 둔 시간에 비례해 비용이 증가합니다. 운영 원칙은 `deploy:aggregation-perf`로 테스트 직전에 올리고, load test와 결과 수집이 끝나면 결과를 S3 `aggregation-perf-runs/` prefix에 업로드한 뒤 즉시 `destroy:aggregation-perf`로 내리는 것입니다. 더 높은 RPS 검사가 필요하면 EC2/MSK instance type, broker count, partition count, task count를 함께 올려 같은 구조를 확장합니다.

## 가격 출처

- AWS Price List API: `AmazonECS`, `AmazonEC2`, `AmazonVPC`, `AWSELB`, `AmazonCloudWatch`, `AmazonECR`, `AmazonRDS`, `AmazonMSK`
- Region price index: `ap-northeast-2`

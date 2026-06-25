# loop-ad AWS CDK 요구사항

이 프로젝트는 loop-ad 개발 환경과 집계 경로 성능 테스트 환경을 AWS CDK v2로 관리한다.

## 범위

- 담당: VPC, ECS, ECR, ALB/NLB, 보안그룹, S3 Gateway Endpoint, 개발용 Aurora/ClickHouse/MSK, SSM endpoint contract, GitHub Actions reusable workflow
- 제외: 애플리케이션 코드, SDK, React 구현, 비즈니스 로직, 실제 데이터 적재/로그 운영
- 리전: `ap-northeast-2`

## 환경

### Dev

상시 개발용 스택이다.

- 한 VPC를 소유한다.
- 월 비용 목표는 기본 `$300` 이내로 둔다.
- AWS Budget으로 월간 비용 목표를 명시한다.
- Budget은 지출을 자동 차단하지 않으므로, ECS task 수와 EC2 capacity 수치로 실제 확장 상한을 둔다.
- 외부 SaaS/API 연동을 위해 NAT Gateway가 있는 private subnet에서 실행한다.
- ECR, CloudWatch Logs, SSM, ECS Interface Endpoint는 만들지 않고 NAT Gateway를 통해 public AWS API를 호출한다.
- S3 Gateway Endpoint는 유지해서 S3/ECR layer 트래픽을 NAT data processing으로 보내지 않는다.
- Event Collector, Ad Context Projector, Ad Decision API, Dashboard API, Recommendation을 ECS Fargate로 실행한다.
- 각 개발 서비스는 기본 1 task로 시작하고 CPU 부하에 따라 최대 2 task까지만 자동 확장한다.
- Event Collector는 NLB에만 붙인다.
- Ad Decision API와 Dashboard API는 ALB path rule에만 붙인다.
- Aurora, Redis, ClickHouse, MSK는 SSM endpoint contract로 연결한다.
- Aurora PostgreSQL은 Serverless v2 `16.13`, `min 0 ACU`, `max 2 ACU`, idle 10분 auto-pause로 시작한다.
- ClickHouse는 EC2 `t4g.small`, Amazon Linux 2023, gp3 50GB EBS로 시작한다.
- MSK는 provisioned `kafka.t3.small` 2 brokers와 broker당 20GB storage로 시작한다.
- MSK bootstrap broker 문자열은 배포 시 `GetBootstrapBrokers` custom resource로 조회해 SSM parameter에 넣는다.
- Redis provision 방식은 별도 결정 전까지 endpoint contract만 유지한다.
- 앱 인프라와 ClickHouse, Aurora, MSK를 합산했을 때 Aurora가 idle 시간대에 auto-pause되는 조건으로 월 `$300` 안쪽을 목표로 한다.

### Aggregation Perf

집계 경로 성능 테스트용 임시 스택이다.

- Dev 스택이 export한 VPC/public subnet을 import해서 같은 VPC 안에 뜬다.
- NAT Gateway를 사용하지 않고 public subnet에서만 실행한다.
- 집계 경로 20k RPS / request 1KB 목표 검증을 전제로 한 비용 절감형 임시 benchmark 환경이어야 한다.
- ECS on EC2 capacity는 `c7g.xlarge` 6대를 기본/최소로 시작하고, 최대 12대까지 확장한다.
- Event Collector는 기본 24 tasks로 시작하고, CPU 부하에 따라 최대 48 tasks까지 확장한다.
- Ad Context Projector는 기본 12 tasks로 시작하고, CPU 부하에 따라 최대 24 tasks까지 확장한다.
- Aggregation perf task는 bridge network mode와 dynamic host port를 사용해 한 EC2에 여러 task를 배치할 수 있어야 한다.
- 성능 테스트가 끝나면 이 스택만 destroy할 수 있어야 한다.
- Event Collector와 Ad Context Projector만 둔다.
- ECS on EC2 capacity provider를 사용한다.
- ClickHouse는 집계 경로 성능 테스트 전용 EC2 `c7g.xlarge`, gp3 500GB, 3k IOPS로 둔다.
- MSK는 집계 경로 성능 테스트 전용 provisioned `kafka.m7g.xlarge` 2 brokers와 broker당 200GB storage로 둔다.
- MSK에는 `aggregation-events` topic을 만들고 128 partitions, replication factor 2를 사용한다.
- MSK bootstrap broker 문자열은 배포 시 `GetBootstrapBrokers` custom resource로 조회해 SSM parameter에 넣는다.
- 필요한 endpoint contract는 MSK, Redis, ClickHouse, results bucket만 둔다.
- 성능 분석 결과는 S3에 백업해야 하며, aggregation perf 스택 destroy 이후에도 남아야 한다.
- 결과 백업 S3 bucket은 Dev 스택이 소유하고, Aggregation perf 스택은 bucket name을 import해 `/loop-ad/aggregation-perf/results-bucket-name` SSM parameter로 전달한다.
- Aggregation perf task role은 `aggregation-perf-runs/` prefix에 object upload 권한만 가진다.
- API route, Dashboard, Recommendation, Aurora, frontend, 데이터 로그 관리는 포함하지 않는다.
- 20k RPS 보장은 CDK 설정만으로 완료되는 것이 아니며, 실제 aggregation perf run에서 애플리케이션 처리량, NLB/ECS/MSK quota, load generator, ClickHouse write pattern을 함께 검증해야 한다.
- 향후 더 높은 RPS 검사가 필요하면 같은 구조에서 EC2/MSK instance type, broker count, partition count, task count 상수를 올려 확장한다.

## 운영 명령

- `npm run synth:dev`
- `npm run synth:aggregation-perf`
- `npm run deploy:dev`
- `npm run deploy:aggregation-perf`
- `npm run destroy:aggregation-perf`

`npm run deploy`와 `npm run destroy`는 실수 방지를 위해 차단한다.

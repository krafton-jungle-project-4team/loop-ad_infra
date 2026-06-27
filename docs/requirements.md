# loop-ad AWS CDK 요구사항

이 프로젝트는 loop-ad 개발 환경을 AWS CDK v2로 관리한다.

## 범위

- 담당: CloudFront용 ACM certificate, ECR repository, VPC/network, data storage stack, runtime service stack, ECS, ALB/NLB, 보안그룹, S3 Gateway Endpoint, FE 정적 사이트용 S3/CloudFront, DataStorage S3, GenAI 생성물 공개용 CloudFront, 개발용 Aurora/Valkey/ClickHouse/EC2 Kafka, SSM endpoint contract, GitHub Actions reusable workflow
- 제외: 애플리케이션 코드, SDK, React 구현, 비즈니스 로직, 실제 데이터 적재, DB migration, Kafka topic 생성, ClickHouse schema 생성
- 리전: `ap-northeast-2`
- public domain: `loop-ad.org`

## 환경

### Dev

상시 개발용 스택이다.

- 한 VPC를 소유한다.
- CloudFront custom domain용 ACM certificate는 `us-east-1`의 별도 certificate stack에서 관리한다.
- ALB/NLB public ingress용 ACM certificate는 `ap-northeast-2` runtime stack에서 관리한다.
- Dev data/runtime stack은 certificate stack output ARN을 `.env`로 받아 CloudFront distribution에 import한다.
- ECR repository는 별도 repository stack에서 먼저 만들고, 각 앱 repo가 image를 push한 뒤 ECS runtime stack을 배포한다.
- Dev runtime stack은 ECR repository를 생성하지 않고 고정 repository name contract로 import한다.
- 비용 산정은 `npm run cost:dev`의 명시 가정과 deterministic 계산 결과를 기준으로 검토하고, 배포 승인 전 AWS Pricing Calculator 또는 Price List API로 단가를 갱신한다.
- 비용 알림은 이 CDK app에서 AWS Budget 리소스로 생성하지 않고, 별도 정기 비용 알림 체계에서 담당한다.
- VPC/network, data storage, runtime service는 각각 stack을 나눈다.
- ECS task 수와 EC2 capacity 수치로 실제 확장 상한을 둔다.
- 외부 SaaS/API 연동을 위해 NAT Gateway가 있는 private subnet에서 실행한다.
- VPC는 subnet 배치를 예측 가능하게 유지하기 위해 `ap-northeast-2a`, `ap-northeast-2c` 두 AZ를 명시해 생성한다.
- ECR, CloudWatch Logs, SSM, ECS Interface Endpoint는 만들지 않고 NAT Gateway를 통해 public AWS API를 호출한다.
- S3 Gateway Endpoint는 유지해서 S3/ECR layer 트래픽을 NAT data processing으로 보내지 않는다.
- Dashboard FE는 `https://dashboard.dev.loop-ad.org`, demo-shoppingmall FE는 `https://demo-shoppingmall.dev.loop-ad.org`로 공개한다.
- Public API는 `https://api.dev.loop-ad.org`, Event ingest는 `https://ingest.dev.loop-ad.org`를 기본 외부 contract로 둔다.
- ALB/NLB 모두 public 443 ingress만 열고, load balancer가 TLS를 종료한 뒤 private ECS container의 80 포트로 전달한다.
- 각 FE는 private S3 bucket과 CloudFront OAC를 사용하며, SPA fallback은 `/index.html`로 처리한다.
- DataStorage S3 bucket은 필수로 생성하며 GenAI 생성물은 `genai/generated/` prefix에 저장한다.
- GenAI 생성물은 CloudFront OAC를 통해 `https://gen-ai.asset.dev.loop-ad.org/...`로 외부 조회할 수 있게 한다.
- DataStorage S3 bucket은 public access 차단, 서버 측 암호화, HTTPS 강제, bucket owner enforced object ownership, CloudFront OAC 접근 제어를 필수 보안 조건으로 가진다.
- Dev data stack은 Aurora/Valkey/ClickHouse/EC2 Kafka/DataStorage S3와 SSM endpoint contract를 소유한다.
- Dev runtime stack은 FE 정적 hosting, ALB/NLB, Route53 public runtime record, ECS cluster/service/log group을 소유한다.
- Public HTTPS endpoint와 private service discovery name은 고정 contract로 문서화하고, 앱별 env로 다시 분리하지 않는다.
- Event Collector, Advertisement API, Dashboard API, Decision API를 ECS 서비스로 실행한다.
- 각 ECS 서비스는 `/loop-ad/dev/ecs/<service-id>` 형식의 별도 CloudWatch LogGroup에 stdout/stderr 로그를 남기고 dev에서는 3개월만 보관한다.
- 각 개발 서비스는 기본 1 task로 시작하고 CPU 부하에 따라 최대 2 task까지만 자동 확장한다.
- Event Collector는 NLB에만 붙인다.
- Advertisement API와 Dashboard API는 ALB path rule에만 붙인다.
- Aurora, Redis 호환 Valkey, ClickHouse, Kafka는 SSM endpoint contract로 연결한다.
- Kafka/MSK, ClickHouse, cache, DB의 관리형 전환은 [managed-service-transition-plan.md](managed-service-transition-plan.md)의 performance test, 월 $1200 이하 비용 검증, rollback, migration risk, 앱 env/SSM contract 영향, CDK 변경 범위 gate를 통과해야 한다.
- Aurora PostgreSQL은 안정 기준 버전 `16.13`, Serverless v2 `min 0 ACU`, `max 2 ACU`, idle 10분 auto-pause로 시작한다.
- Redis 호환 cache는 ElastiCache Serverless for Valkey major version `7`로 시작하고, `LOOPAD_REDIS_URL`에는 TLS endpoint인 `rediss://...:6379`를 주입한다.
- ClickHouse는 LTS tag `26.3.13.31`, EC2 `t4g.small`, Amazon Linux 2023, gp3 50GB EBS로 시작한다.
- Kafka는 비용 절감을 위해 Amazon Linux 2023 EC2 `t4g.small` 단일 노드, Apache Kafka `3.9.1`, KRaft mode, gp3 20GB EBS로 시작한다.
- Kafka는 private subnet 안에서만 plain listener `9092`를 열고, production 수준의 HA나 managed broker 운영은 목표로 하지 않는다.
- Kafka bootstrap broker 문자열은 EC2 private DNS와 `9092` port를 조합해 `/loop-ad/dev/kafka/bootstrap-brokers` SSM parameter에 넣는다.
- OpenAI API key는 infra repo 배포 환경의 `LOOP_AD_OPENAI_API_KEY` 값을 `/loop-ad/dev/external/openai/api-key` SSM SecureString으로 주입한 뒤 Runtime stack에서 참조한다.
- 앱 repo GitHub Actions OIDC role은 ECR image push와 ECS service update 권한을 가진다.
- Infra repo GitHub Actions OIDC role은 CDK가 관리하는 dev 인프라 전반을 생성/변경할 수 있는 권한을 가진다.
- `.env`, `CDK_DEFAULT_ACCOUNT`, CDK context 값은 fallback 기본값 없이 필수로 요구한다.
- Dev data stack 실행 시 GenAI asset용 CloudFront certificate ARN을 필수로 요구한다.
- Dev runtime stack 실행 시 frontend site용 CloudFront certificate ARN과 data stack 참조를 필수로 요구한다.

## 운영 명령

- `npm run synth:dev-certificate`
- `npm run synth:dev-repositories`
- `npm run synth:dev-network`
- `npm run synth:dev-data`
- `npm run synth:dev-runtime`
- `npm run synth:dev`
- `npm run cost:dev`
- `npm run put:dev-openai-api-key`
- `npm run deploy:dev-certificate`
- `npm run deploy:dev-repositories`
- `npm run deploy:dev-network`
- `npm run deploy:dev-data`
- `npm run deploy:dev-runtime`
- `npm run deploy:dev`

최초 배포 순서는 `deploy:dev-certificate` -> `deploy:dev-repositories` -> 각 앱 repo의 ECR seed image push -> `deploy:dev-network` -> `deploy:dev-data` -> `put:dev-openai-api-key` -> `deploy:dev-runtime` 순서로 둔다. ECS service는 image와 외부 secret이 준비된 뒤 배포해야 초기 배포에서 image pull 실패나 runtime secret 누락을 피할 수 있다.

데이터 초기화 책임은 현재 infra contract에서 제외한다. DB migration, Kafka topic 생성, ClickHouse schema 생성 주체는 앱 구현이 구체화된 뒤 별도로 정한다.

실제 CDK 배포 전에는 `ap-northeast-2`와 CloudFront certificate용 `us-east-1`에 CDK bootstrap을 먼저 수행한다.

`npm run deploy`와 `npm run destroy`는 실수 방지를 위해 차단한다.

# loop-ad AWS CDK 요구사항

이 프로젝트는 loop-ad 개발 환경을 AWS CDK v2로 관리한다.

## 범위

- 담당: CloudFront용 ACM certificate, ECR repository, VPC, ECS, ALB/NLB, 보안그룹, S3 Gateway Endpoint, FE 정적 사이트용 S3/CloudFront, DataStorage S3, GenAI 생성물 공개용 CloudFront, 개발용 Aurora/Valkey/ClickHouse/MSK, SSM endpoint contract, GitHub Actions reusable workflow
- 제외: 애플리케이션 코드, SDK, React 구현, 비즈니스 로직, 실제 데이터 적재/로그 운영
- 리전: `ap-northeast-2`

## 환경

### Dev

상시 개발용 스택이다.

- 한 VPC를 소유한다.
- CloudFront custom domain용 ACM certificate는 `us-east-1`의 별도 certificate stack에서 관리한다.
- Dev app stack은 certificate stack output ARN을 `.env`로 받아 CloudFront distribution에 import한다.
- ECR repository는 별도 repository stack에서 먼저 만들고, 각 앱 repo가 image를 push한 뒤 ECS app stack을 배포한다.
- Dev app stack은 ECR repository를 생성하지 않고 고정 repository name contract로 import한다.
- ECS task 수와 EC2 capacity 수치로 실제 확장 상한을 둔다.
- 외부 SaaS/API 연동을 위해 NAT Gateway가 있는 private subnet에서 실행한다.
- VPC는 subnet 배치를 예측 가능하게 유지하기 위해 `ap-northeast-2a`, `ap-northeast-2c` 두 AZ를 명시해 생성한다.
- ECR, CloudWatch Logs, SSM, ECS Interface Endpoint는 만들지 않고 NAT Gateway를 통해 public AWS API를 호출한다.
- S3 Gateway Endpoint는 유지해서 S3/ECR layer 트래픽을 NAT data processing으로 보내지 않는다.
- Dashboard FE는 `https://dashboard.dev.<public-domain>`, demo-shoppingmall FE는 `https://demo-shoppingmall.dev.<public-domain>`으로 공개한다.
- 각 FE는 private S3 bucket과 CloudFront OAC를 사용하며, SPA fallback은 `/index.html`로 처리한다.
- DataStorage S3 bucket은 필수로 생성하며 GenAI 생성물은 `genai/generated/` prefix에 저장한다.
- GenAI 생성물은 CloudFront OAC를 통해 `https://gen-ai.asset.dev.<public-domain>/...`로 외부 조회할 수 있게 한다.
- DataStorage S3 bucket은 public access 차단, 서버 측 암호화, HTTPS 강제, bucket owner enforced object ownership, CloudFront OAC 접근 제어를 필수 보안 조건으로 가진다.
- Public domain과 private service endpoint는 고정 contract로 문서화하고, 앱별 env로 다시 분리하지 않는다.
- Event Collector, Ad Context Projector, Advertisement API, Dashboard API, Decision을 ECS 서비스로 실행한다.
- 각 개발 서비스는 기본 1 task로 시작하고 CPU 부하에 따라 최대 2 task까지만 자동 확장한다.
- Event Collector는 NLB에만 붙인다.
- Advertisement API와 Dashboard API는 ALB path rule에만 붙인다.
- Aurora, Redis 호환 Valkey, ClickHouse, MSK는 SSM endpoint contract로 연결한다.
- Aurora PostgreSQL은 안정 기준 버전 `16.13`, Serverless v2 `min 0 ACU`, `max 2 ACU`, idle 10분 auto-pause로 시작한다.
- Redis 호환 cache는 ElastiCache Serverless for Valkey major version `7`로 시작하고, `LOOPAD_REDIS_URL`에는 TLS endpoint인 `rediss://...:6379`를 주입한다.
- ClickHouse는 LTS tag `26.3.13.31`, EC2 `t4g.small`, Amazon Linux 2023, gp3 50GB EBS로 시작한다.
- MSK는 AWS recommended Kafka `3.9.x`, provisioned `kafka.t3.small` 2 brokers와 broker당 20GB storage로 시작한다.
- MSK bootstrap broker 문자열은 배포 시 `GetBootstrapBrokers` custom resource로 조회해 SSM parameter에 넣는다.
- `.env`, `CDK_DEFAULT_ACCOUNT`, CDK context 값은 fallback 기본값 없이 필수로 요구한다.
- Dev app stack 실행 시 CloudFront certificate ARN 두 개를 필수로 요구한다.

## 운영 명령

- `npm run synth:dev-certificate`
- `npm run synth:dev-repositories`
- `npm run synth:dev-network`
- `npm run synth:dev`
- `npm run deploy:dev-certificate`
- `npm run deploy:dev-repositories`
- `npm run deploy:dev-network`
- `npm run deploy:dev`

최초 배포 순서는 `deploy:dev-certificate` -> `deploy:dev-repositories` -> 각 앱 repo의 ECR image push -> `deploy:dev-network` 또는 `deploy:dev` 순서로 둔다. ECS service는 image가 존재한 뒤 배포해야 초기 배포에서 image pull 실패를 피할 수 있다.

실제 CDK 배포 전에는 `ap-northeast-2`와 CloudFront certificate용 `us-east-1`에 CDK bootstrap을 먼저 수행한다.

`npm run deploy`와 `npm run destroy`는 실수 방지를 위해 차단한다.

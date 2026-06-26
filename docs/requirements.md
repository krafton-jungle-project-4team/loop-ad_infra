# loop-ad AWS CDK 요구사항

이 프로젝트는 loop-ad 개발 환경을 AWS CDK v2로 관리한다.

## 범위

- 담당: VPC, ECS, ECR, ALB/NLB, 보안그룹, S3 Gateway Endpoint, FE 정적 사이트용 S3/CloudFront, DataStorage S3, GenAI 생성물 공개용 CloudFront, 개발용 Aurora/ClickHouse/MSK, SSM endpoint contract, GitHub Actions reusable workflow
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
- Aurora, Redis, ClickHouse, MSK는 SSM endpoint contract로 연결한다.
- Aurora PostgreSQL은 Serverless v2 `16.13`, `min 0 ACU`, `max 2 ACU`, idle 10분 auto-pause로 시작한다.
- ClickHouse는 EC2 `t4g.small`, Amazon Linux 2023, gp3 50GB EBS로 시작한다.
- MSK는 provisioned `kafka.t3.small` 2 brokers와 broker당 20GB storage로 시작한다.
- MSK bootstrap broker 문자열은 배포 시 `GetBootstrapBrokers` custom resource로 조회해 SSM parameter에 넣는다.
- Redis provision 방식은 별도 결정 전까지 endpoint contract만 유지한다.
- 앱 인프라와 ClickHouse, Aurora, MSK를 합산했을 때 Aurora가 idle 시간대에 auto-pause되는 조건으로 월 `$300` 안쪽을 목표로 한다.
- `.env`, `CDK_DEFAULT_ACCOUNT`, CDK context 값은 fallback 기본값 없이 필수로 요구한다.

## 운영 명령

- `npm run synth:dev`
- `npm run deploy:dev`

`npm run deploy`와 `npm run destroy`는 실수 방지를 위해 차단한다.

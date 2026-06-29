# loop-ad 인프라

loop-ad dev 환경의 AWS CDK v2 인프라 레포입니다. 이 레포는 앱 코드를 포함하지 않고, dev용 네트워크, 데이터 리소스, 런타임, 정적 프론트 배포 대상, 앱 레포 계약을 관리합니다.

## 현재 구조

- Stack boundary: `Certificate`, `Repository`, `Secrets`, `Network`, `Data`, `Runtime`
- Network: public subnet only VPC, NAT Gateway 없음, private subnet 없음
- Runtime: public ALB 하나와 ECS/Fargate 서비스 3개
- Services: `event-collector`, `dashboard-api`, `decision-api`
- Data: Aurora Serverless v2, ClickHouse EC2, Kafka EC2, DataStorage S3, GenAI assets CloudFront
- Secrets: CDK는 Secrets Manager secret 이름과 lifecycle만 관리하고, 실제 값은 별도 gitignored env sync로 입력

## 주요 명령

```bash
npm run build
npm test
npm run synth
npm run secrets:sync -- --env-file .env.secrets --dry-run
```

`npm run deploy`와 `npm run destroy` 스크립트는 남아 있지만, 실제 배포/삭제는 별도 승인 후에만 실행합니다.
`npm run secrets:sync`는 실제 Secrets Manager 값을 쓰는 명령이므로 AWS 계정에 반영하기 전 별도 운영 승인으로 실행합니다.

## 필수 환경 변수

CDK 실행 전 `.env` 또는 process env에 비밀값이 아닌 설정만 둡니다.

```bash
CDK_DEFAULT_ACCOUNT=123456789012
LOOP_AD_REGION=ap-northeast-2
LOOP_AD_PUBLIC_DOMAIN_NAME=loop-ad.org
LOOP_AD_PUBLIC_HOSTED_ZONE_ID=Z...
LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN=arn:aws:acm:us-east-1:...
LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN=arn:aws:acm:us-east-1:...
LOOP_AD_SECRET_PREFIX=/loop-ad/dev
LOOP_AD_DEVELOPER_IPV4_CIDRS=203.0.113.10/32
LOOP_AD_DEVELOPER_IPV6_CIDRS=2001:db8::10/128
```

모든 값은 명시적으로 제공합니다. Developer CIDR allowlist는 값 없이 비워 둘 수 있지만, 변수 자체는 명시합니다. 비워 두면 Aurora, ClickHouse, Kafka에 대한 직접 접근 rule을 만들지 않습니다. `0.0.0.0/0`과 `::/0`은 금지됩니다.

배포 후 secret 값 동기화는 [docs/secrets-setup.md](docs/secrets-setup.md)를 봅니다.

## 앱 레포 계약

앱 레포는 인프라를 직접 만들지 않습니다. Docker image, health check, env 검증, reusable workflow 호출만 준비합니다.

deploy target과 runtime env 계약은 [docs/app-repository-guide.md](docs/app-repository-guide.md)를 봅니다.

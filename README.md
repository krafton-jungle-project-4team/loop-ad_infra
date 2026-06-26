# loop-ad 인프라

loop-ad 관련 인프라를 관리하는 레포입니다.

이 레포는 애플리케이션 코드나 비즈니스 로직을 다루지 않습니다. 대신 애플리케이션이 올라갈 AWS 인프라, 개발자가 따라야 할 배포/연동 규칙, 그리고 CI/CD 재사용 workflow를 제공합니다.

## 제공하는 것

1. 애플리케이션 개발 가이드
   - 앱 레포 구조, Dockerfile, 환경 변수, ECS 런타임 계약은 [docs/app-repository-guide.md](docs/app-repository-guide.md)에 정리합니다.
   - 서비스 endpoint와 앱에서 env로 받아야 하는 값은 [docs/service-endpoints.md](docs/service-endpoints.md)에 정리합니다.

2. 외부 접근 인프라 정보
   - Dashboard, demo shoppingmall, API, ingest, GenAI asset public endpoint를 문서화합니다.
   - 현재 dev public endpoint 목록은 [docs/service-endpoints.md](docs/service-endpoints.md)를 봅니다.

3. AWS CDK
   - dev 환경의 ACM certificate, VPC/network, ECS, ECR, ALB/NLB, Route53, S3/CloudFront, Aurora, ClickHouse, MSK, SSM contract를 관리합니다.
   - 메인 스택은 [src/loop-ad-stack.ts](src/loop-ad-stack.ts)입니다.

4. CI/CD용 GitHub Actions 템플릿
   - ECS 서비스 배포 reusable workflow: [.github/workflows/ecs-deploy.yml](.github/workflows/ecs-deploy.yml)
   - Frontend 정적 배포 reusable workflow: [.github/workflows/frontend-deploy.yml](.github/workflows/frontend-deploy.yml)
   - 인프라 검증 workflow: [.github/workflows/infra-check.yml](.github/workflows/infra-check.yml)
   - 호출 예시는 [docs/github-actions](docs/github-actions)를 봅니다.

## 주요 명령

```bash
npm run build
npm test
npm run synth:dev-certificate
npm run synth:dev
npm run deploy:dev-certificate
npm run deploy:dev
```

처음 배포할 때는 `npm run deploy:dev-certificate`로 CloudFront용 ACM 인증서를 먼저 만들고, 출력된 ARN을 `.env`에 넣은 뒤 `npm run deploy:dev`를 실행합니다.

`npm run deploy`와 `npm run destroy`는 실수 방지를 위해 막혀 있습니다.

## 환경 변수

CDK 실행 전 `.env` 또는 process env에 아래 값을 설정해야 합니다.

```bash
LOOP_AD_PUBLIC_HOSTED_ZONE_ID=Z...
LOOP_AD_PUBLIC_DOMAIN_NAME=loop-ad.org
LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN=arn:aws:acm:us-east-1:...
LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN=arn:aws:acm:us-east-1:...
```

`CDK_DEFAULT_ACCOUNT`도 CDK 실행 환경에서 제공되어야 합니다.

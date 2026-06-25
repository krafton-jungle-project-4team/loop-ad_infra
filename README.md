# loop-ad_aws_cdk

loop-ad AWS 인프라를 위한 CDK v2 초안 프로젝트입니다. 이 repo의 범위는 AWS 인프라와 GitHub Actions reusable workflow뿐입니다. 애플리케이션 코드, SDK, React 구현, OpenAI/n8n/Discord 자체 설정, 비즈니스 로직은 다루지 않습니다.

## 원칙

- 리전은 `ap-northeast-2`로 고정합니다.
- 실제 AWS `deploy`/`destroy`는 기본 차단합니다.
- CDK L3/project construct와 L2를 우선 사용하고, L1 `Cfn*` 직접 생성은 테스트로 막습니다.
- NLB는 Event SDK ingestion 전용이며 Event Collector만 target으로 허용합니다.
- ALB는 사용자/API HTTP 전용이며 Ad Decision API와 Dashboard API만 target으로 허용합니다.
- CloudFront는 S3 frontend/media bucket만 origin으로 둡니다.
- NAT Gateway는 기본 off입니다. 외부 HTTPS egress가 필요한 서비스에만 명시적으로 SG egress를 엽니다.
- AWS API 접근은 VPC Endpoint를 우선합니다.

## 스택

- `NetworkStack`: VPC, subnet, NAT 옵션, VPC Endpoint, 보안그룹, ECS Cluster, EC2 capacity provider
- `EdgeStack`: ALB, NLB
- `FrontendStack`: S3 frontend/media bucket, CloudFront
- `StorageStack`: ECR repositories
- `StreamStack`: MSK endpoint contract
- `DataStack`: Aurora/Redis/ClickHouse endpoint contract
- `CollectStack`: Event Collector
- `DecisionStack`: Ad Decision API
- `DashboardStack`: Dashboard API
- `AnalyticsStack`: Ad Context Projector, Recommendation Server
- `ObservabilityStack`: CloudWatch dashboard 초안

## 명령

```bash
npm run build
npm test
npm run synth:dev
npm run synth:perf
```

`npm run deploy`와 `npm run destroy`는 의도적으로 실패합니다.

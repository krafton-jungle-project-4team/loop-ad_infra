# Service Endpoints

이 문서는 loop-ad dev 환경에서 고정으로 사용하는 public domain과 private service endpoint contract입니다.

도메인과 service discovery 이름은 인프라 contract이므로 앱별 env로 빼지 않습니다. 앱 코드는 아래 값을 그대로 상수 또는 공유 endpoint 모듈로 사용하고, 값이 바뀌면 이 문서와 관련 앱 코드를 함께 수정합니다.

아래 표의 endpoint는 예시가 아니라 dev 환경에서 그대로 사용하는 값입니다.

## Public Domains

| 용도 | Endpoint | 연결 대상 |
|---|---|---|
| Dashboard FE | `https://dashboard.dev.loop-ad.org` | Dashboard 정적 사이트 CloudFront |
| Demo shoppingmall FE | `https://demo-shoppingmall.dev.loop-ad.org` | Demo shoppingmall 정적 사이트 CloudFront |
| Public API | `http://api.dev.loop-ad.org` | ALB public HTTP listener |
| Event ingest | `http://ingest.dev.loop-ad.org` | NLB public TCP 80 listener |
| GenAI generated assets | `https://gen-ai.asset.dev.loop-ad.org/<object-key>` | DataStorage S3 `genai/generated/` prefix 앞 CloudFront |

현재 CDK contract 기준으로 API와 ingest endpoint는 public port 80입니다. HTTPS ingress를 추가하면 이 문서의 scheme도 같이 바꿉니다.

## Public API Routes

| Domain | Path | 연결 서비스 |
|---|---|---|
| `api.dev.loop-ad.org` | `/api/ads/*` | Advertisement API |
| `api.dev.loop-ad.org` | `/advertisements/*` | Advertisement API |
| `api.dev.loop-ad.org` | `/api/dashboard/*` | Dashboard API |
| `api.dev.loop-ad.org` | `/dashboard/*` | Dashboard API |
| `ingest.dev.loop-ad.org` | TCP/HTTP port `80` | Event Collector |

FE는 위 public domain을 직접 사용합니다. `VITE_API_BASE_URL`, `VITE_INGEST_BASE_URL` 같은 env로 다시 빼지 않습니다.

## Private Service Endpoints

Private endpoint는 ECS service가 VPC 내부에서 다른 service를 호출할 때 사용합니다. public domain을 내부 service-to-service 호출에 사용하지 않습니다.

| 서비스 | Internal endpoint | 주 사용처 |
|---|---|---|
| Event Collector | `http://event-collector.dev.loop-ad.local:80` | 내부 수집 경로가 필요할 때 |
| Ad Context Projector | `http://ad-context-projector.dev.loop-ad.local:80` | 내부 health/debug 경로가 필요할 때 |
| Advertisement API | `http://advertisement-api.dev.loop-ad.local:80` | 내부 광고 API 호출이 필요할 때 |
| Dashboard API | `http://dashboard-api.dev.loop-ad.local:80` | 내부 dashboard API 호출이 필요할 때 |
| Decision | `http://decision.dev.loop-ad.local:80` | Dashboard API가 Decision을 호출할 때 |

`*.dev.loop-ad.local` 이름은 ECS Cloud Map private namespace입니다. VPC 내부 ECS service에서만 resolve된다고 가정합니다.

## Env로 받는 값과 받지 않는 값

Env로 받지 않는 값:

- public domain
- private service endpoint
- public API route prefix
- GenAI generated assets public base URL

Env로 받는 값:

- DB endpoint와 credential
- Redis endpoint
- MSK bootstrap broker
- S3 bucket 이름과 object prefix
- 외부 SaaS API key

고정 도메인은 routing contract이고, data source endpoint와 secret은 runtime dependency입니다. 그래서 도메인은 이 문서에 고정하고, data source와 secret은 앱 runtime env contract로 관리합니다.

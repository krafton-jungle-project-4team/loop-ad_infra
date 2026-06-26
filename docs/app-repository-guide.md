# App Developer Guide

이 문서는 loop-ad 애플리케이션 개발자가 각 서비스 repo를 만들거나 수정할 때 따라야 하는 매뉴얼입니다.

개발자는 이 문서에 정의된 repo 형식, Dockerfile 규칙, npm build 규칙, 환경변수 검증 규칙을 자기 애플리케이션 repo에 적용합니다. DB endpoint, DB password, 외부 API key 같은 실제 값은 인프라 쪽에서 ECS task definition과 배포 workflow를 통해 제공합니다. 애플리케이션 repo와 GitHub Actions workflow는 runtime secret 값을 직접 소유하지 않습니다.

개발자가 해야 할 핵심 작업은 세 가지입니다.

- 서버 repo는 Dockerfile과 ECS deploy workflow를 둡니다.
- FE repo는 통일된 npm build 명령과 frontend deploy workflow를 둡니다.
- 앱 코드는 이 문서의 env 이름을 시작 시점에 검증하고, fallback 없이 config 객체로 사용합니다.

## 책임 분리

| 영역 | 책임 |
|---|---|
| 애플리케이션 개발자 | 이 문서의 repo 형식과 env 이름을 따릅니다. 필수 env가 없으면 시작 또는 build를 실패시키고, secret 값을 로그에 남기지 않습니다. |
| 애플리케이션 repo | Dockerfile, build script, env 검증 코드, deploy workflow를 소유합니다. DB endpoint나 secret 값을 repo, image, workflow에 저장하지 않습니다. |
| 인프라 repo | ECS task definition, ECR, DataStorage S3, S3/CloudFront, DB, internal DNS, SSM/Secrets Manager, IAM 권한을 관리합니다. |
| GitHub Actions | 서버 image를 빌드하고 ECS service의 image만 교체합니다. FE는 정적 파일을 빌드해 S3/CloudFront에 배포합니다. 런타임 DB/secret 값은 다루지 않습니다. |

## 공통 규칙

모든 애플리케이션 repo는 env 값을 느슨하게 다루지 않습니다. 이 규칙은 서버와 FE 모두에 적용됩니다.

- env 값에 fallback이나 기본값을 부여하지 않습니다.
- 서버는 런타임 시작 시점에 필요한 env를 즉시 확인합니다.
- FE는 build 시작 시점에 필요한 public env를 즉시 확인합니다.
- 검증된 env는 별도 config 객체로 모읍니다.
- 필수 env가 없으면 빠르게 실패합니다.
- secret 값은 로그, metric label, error response, browser bundle에 남기지 않습니다.
- `.env`, `.env.local`, `.env.*.local`은 commit하지 않습니다.
- 서버 앱은 AWS SSM Parameter Store나 Secrets Manager를 직접 조회하지 않습니다. ECS에서 주입된 환경변수만 읽습니다.
- `_PARAMETER`로 끝나는 env 이름은 앱이 SSM을 직접 읽는 방식이므로 신규 서버 코드에서는 사용하지 않습니다.
- 앱 코드는 ECS launch type에 의존하지 않습니다. 배포 대상은 `ECS service`로만 봅니다.

금지 예시:

```ts
const apiBaseUrl = process.env.API_BASE_URL || 'http://localhost:3000';
```

권장 예시:

```ts
function requiredEnv(name: string): string {
    const value = process.env[name];
    if (!value) {
        throw new Error(`${name} is required`);
    }
    return value;
}

export const appConfig = Object.freeze({
    env: requiredEnv('LOOPAD_ENV'),
    port: Number(requiredEnv('PORT')),
    serviceId: requiredEnv('LOOPAD_SERVICE_ID'),
});
```

## Repo Type

loop-ad 앱 repo는 크게 두 형식으로 나눕니다. 자기 repo가 어느 형식인지 먼저 정하고 해당 섹션만 적용하면 됩니다.

| 형식 | 배포 대상 | 필수 구성 |
|---|---|---|
| Server | ECS service | Dockerfile, runtime env 검증, ECS deploy workflow |
| Frontend Static Site | S3 + CloudFront | npm build, public build env 검증, frontend deploy workflow |

## Server Repo

서버 repo는 ECS에서 Docker container로 실행됩니다. 서버 개발자는 아래 파일과 규칙을 자기 repo에 맞춰 준비합니다.

### 필수 파일

```text
Dockerfile
.github/workflows/deploy.yml
src/config 또는 internal/config
```

언어별 디렉터리 이름은 repo가 선택할 수 있지만, env를 읽는 코드는 한 곳에 모아야 합니다.

### Dockerfile 규칙

- Docker image는 현재 dev ECS runtime인 `linux/arm64`에서 실행 가능해야 합니다.
- 서버는 `PORT` env를 읽고 `0.0.0.0:${PORT}`로 listen합니다.
- 컨테이너는 기본적으로 `80` 포트를 사용합니다.
- DB endpoint, password, token을 image build 단계에 넣지 않습니다.
- ALB 대상 서비스는 `/health`에서 `200-399`를 반환해야 합니다.

Go 서버 예시:

```dockerfile
FROM golang:1.23-alpine AS build
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o server ./cmd/server

FROM alpine:3.20
WORKDIR /app
COPY --from=build /app/server /app/server
EXPOSE 80
CMD ["/app/server"]
```

### Env 검증 규칙

서버는 이 문서에서 자기 서비스에 해당하는 env를 읽습니다. 앱 코드는 SSM Parameter Store나 Secrets Manager를 직접 조회하지 않고, 이미 주입된 env만 사용합니다.

Go 예시:

```go
package config

import (
    "log"
    "os"
    "strconv"
)

type Config struct {
    Env       string
    ServiceID string
    Port      int
}

func Load() Config {
    port, err := strconv.Atoi(mustEnv("PORT"))
    if err != nil {
        log.Fatalf("PORT must be a number: %v", err)
    }

    return Config{
        Env:       mustEnv("LOOPAD_ENV"),
        ServiceID: mustEnv("LOOPAD_SERVICE_ID"),
        Port:      port,
    }
}

func mustEnv(name string) string {
    value := os.Getenv(name)
    if value == "" {
        log.Fatalf("%s is required", name)
    }
    return value
}
```

TypeScript 서버 예시:

```ts
function requiredEnv(name: string): string {
    const value = process.env[name];
    if (!value) {
        throw new Error(`${name} is required`);
    }
    return value;
}

export const serverConfig = Object.freeze({
    env: requiredEnv('LOOPAD_ENV'),
    serviceId: requiredEnv('LOOPAD_SERVICE_ID'),
    port: Number(requiredEnv('PORT')),
});
```

### Common Server Env

모든 서버 repo는 아래 env가 ECS에서 들어온다고 가정하고 코드를 작성합니다.

| 이름 | 종류 | 예시 | 설명 |
|---|---|---|---|
| `LOOPAD_ENV` | Plain | `dev` | 실행 환경 이름입니다. |
| `LOOPAD_SERVICE_ID` | Plain | `event-collector` | 서비스 식별자입니다. 로그, metric, tracing label에 사용합니다. |
| `LOOPAD_RUNTIME` | Plain | `go`, `node` | 런타임 구분입니다. |
| `PORT` | Plain | `80` | 컨테이너가 listen해야 하는 포트입니다. |

ECS service는 private subnet에서 실행됩니다. 내부 서비스 URL은 Cloud Map 이름을 사용해도 됩니다. 예를 들어 Dashboard API 개발자는 Recommendation 호출 주소로 아래 값을 사용할 수 있습니다.

```text
http://recommendation.dev.loop-ad.local:80
```

이런 내부 DNS 이름은 private VPC 안에서만 의미가 있으므로 plain env로 제공해도 됩니다.

### DataStorage Env

DataStorage는 서버가 사용하는 영속 데이터 저장 계층입니다. Aurora, ClickHouse, MSK, Redis, GenAI 생성물 저장용 S3 bucket을 포함합니다.

DataStorage S3 bucket은 필수로 존재합니다. GenAI 생성물은 아래 prefix에만 저장합니다.

```text
genai/generated/
```

DataStorage S3 bucket은 public access 차단, 서버 측 암호화, HTTPS 강제, bucket owner enforced object ownership, GenAI prefix 기준 IAM 권한을 필수 보안 조건으로 가집니다. 개발자는 이 보안 조건이 존재한다고 가정하고, 앱 코드에서는 아래 env 이름만 사용합니다.

| 이름 | 종류 | 예시 또는 출처 | 설명 |
|---|---|---|---|
| `LOOPAD_DATA_STORAGE_BUCKET` | Plain | 인프라에서 주입 | GenAI 생성물을 저장하는 DataStorage S3 bucket 이름입니다. |
| `LOOPAD_GENAI_GENERATED_ASSETS_PREFIX` | Plain | `genai/generated/` | GenAI 생성물 전용 S3 prefix입니다. |

#### Aurora PostgreSQL

| 이름 | 종류 | 예시 또는 출처 | 설명 |
|---|---|---|---|
| `LOOPAD_AURORA_HOST` | Plain | `dev-loop-ad-aurora-postgres.cluster-...ap-northeast-2.rds.amazonaws.com` | Aurora writer endpoint hostname입니다. |
| `LOOPAD_AURORA_PORT` | Plain | `5432` | PostgreSQL port입니다. |
| `LOOPAD_AURORA_DATABASE` | Plain | `loopad` | 기본 database 이름입니다. |
| `LOOPAD_AURORA_USERNAME` | Secret | Aurora generated secret | DB username입니다. |
| `LOOPAD_AURORA_PASSWORD` | Secret | Aurora generated secret | DB password입니다. |

#### ClickHouse

| 이름 | 종류 | 예시 또는 출처 | 설명 |
|---|---|---|---|
| `LOOPAD_CLICKHOUSE_URL` | Plain | `http://ip-10-0-...ap-northeast-2.compute.internal:8123` | ClickHouse HTTP endpoint입니다. |
| `LOOPAD_CLICKHOUSE_USERNAME` | Secret | SSM SecureString 또는 Secrets Manager | ClickHouse username입니다. |
| `LOOPAD_CLICKHOUSE_PASSWORD` | Secret | SSM SecureString 또는 Secrets Manager | ClickHouse password입니다. |

#### MSK

| 이름 | 종류 | 예시 또는 출처 | 설명 |
|---|---|---|---|
| `LOOPAD_MSK_BOOTSTRAP_BROKERS` | Plain | `b-1.dev-loop-ad-msk...:9092,b-2...:9092` | MSK bootstrap broker 목록입니다. |
| `LOOPAD_EVENT_TOPIC` | Plain | `loop-ad.events.raw` | raw event topic 이름입니다. |

#### Redis

| 이름 | 종류 | 예시 또는 출처 | 설명 |
|---|---|---|---|
| `LOOPAD_REDIS_URL` | Plain 또는 Secret | `redis://...:6379` | Redis endpoint입니다. password가 포함되는 URL이면 secret env로 주입합니다. |

현재 Redis는 provision 방식이 확정되지 않았습니다. Redis에 의존하는 서비스는 `LOOPAD_REDIS_URL`이 `pending://...`처럼 미확정 값이면 boot 단계에서 명확히 실패하거나 해당 기능을 비활성화해야 합니다.

### External Secret Env

| 이름 | 종류 | 출처 | 설명 |
|---|---|---|---|
| `LOOPAD_OPENAI_API_KEY` | Secret | SSM SecureString 또는 Secrets Manager | Recommendation에서 OpenAI API를 호출할 때 사용합니다. |
| `LOOPAD_DISCORD_WEBHOOK_URL` | Secret | SSM SecureString 또는 Secrets Manager | Dashboard API 알림 연동에 사용합니다. |
| `LOOPAD_N8N_WEBHOOK_URL` | Secret | SSM SecureString 또는 Secrets Manager | Dashboard API n8n 연동에 사용합니다. |

### Service Env Contracts

#### Event Collector

Event Collector는 NLB 트래픽을 받아 raw event를 MSK로 발행합니다.

| Env | 종류 | 필수 | 설명 |
|---|---|---:|---|
| 공통 서버 env | Plain | Yes | `LOOPAD_ENV`, `LOOPAD_SERVICE_ID`, `LOOPAD_RUNTIME`, `PORT` |
| `LOOPAD_MSK_BOOTSTRAP_BROKERS` | Plain | Yes | event publish 대상 MSK broker 목록 |
| `LOOPAD_EVENT_TOPIC` | Plain | Yes | raw event topic |

Secret env는 현재 없습니다.

#### Ad Context Projector

Ad Context Projector는 MSK를 consume하고 Redis/ClickHouse에 가공 결과를 씁니다.

| Env | 종류 | 필수 | 설명 |
|---|---|---:|---|
| 공통 서버 env | Plain | Yes | 공통 실행 설정 |
| `LOOPAD_MSK_BOOTSTRAP_BROKERS` | Plain | Yes | event consume 대상 MSK broker 목록 |
| `LOOPAD_EVENT_TOPIC` | Plain | Yes | consume할 raw event topic |
| `LOOPAD_REDIS_URL` | Plain 또는 Secret | Yes | context cache endpoint |
| `LOOPAD_CLICKHOUSE_URL` | Plain | Yes | aggregate/write 대상 ClickHouse endpoint |
| `LOOPAD_CLICKHOUSE_USERNAME` | Secret | Yes | ClickHouse username |
| `LOOPAD_CLICKHOUSE_PASSWORD` | Secret | Yes | ClickHouse password |

#### Ad Decision API

Ad Decision API는 ALB를 통해 공개 API 요청을 받고 Aurora/Redis를 사용합니다.

| Env | 종류 | 필수 | 설명 |
|---|---|---:|---|
| 공통 서버 env | Plain | Yes | 공통 실행 설정 |
| `LOOPAD_AURORA_HOST` | Plain | Yes | Aurora hostname |
| `LOOPAD_AURORA_PORT` | Plain | Yes | Aurora port |
| `LOOPAD_AURORA_DATABASE` | Plain | Yes | Aurora database |
| `LOOPAD_AURORA_USERNAME` | Secret | Yes | Aurora username |
| `LOOPAD_AURORA_PASSWORD` | Secret | Yes | Aurora password |
| `LOOPAD_REDIS_URL` | Plain 또는 Secret | Yes | decision cache endpoint |

HTTP health check는 `/health`에서 `200-399`를 반환해야 합니다.

#### Dashboard API

Dashboard API는 ALB를 통해 dashboard API 요청을 받고 Aurora/ClickHouse/Recommendation/external webhook을 사용합니다.

| Env | 종류 | 필수 | 설명 |
|---|---|---:|---|
| 공통 서버 env | Plain | Yes | 공통 실행 설정 |
| `LOOPAD_AURORA_HOST` | Plain | Yes | Aurora hostname |
| `LOOPAD_AURORA_PORT` | Plain | Yes | Aurora port |
| `LOOPAD_AURORA_DATABASE` | Plain | Yes | Aurora database |
| `LOOPAD_AURORA_USERNAME` | Secret | Yes | Aurora username |
| `LOOPAD_AURORA_PASSWORD` | Secret | Yes | Aurora password |
| `LOOPAD_CLICKHOUSE_URL` | Plain | Yes | analytics query 대상 ClickHouse endpoint |
| `LOOPAD_CLICKHOUSE_USERNAME` | Secret | Yes | ClickHouse username |
| `LOOPAD_CLICKHOUSE_PASSWORD` | Secret | Yes | ClickHouse password |
| `LOOPAD_DATA_STORAGE_BUCKET` | Plain | Yes | GenAI 생성물 DataStorage bucket |
| `LOOPAD_GENAI_GENERATED_ASSETS_PREFIX` | Plain | Yes | `genai/generated/` |
| `LOOPAD_RECOMMENDATION_URL` | Plain | Yes | `http://recommendation.dev.loop-ad.local:80` |
| `LOOPAD_N8N_WEBHOOK_URL` | Secret | Yes | n8n webhook URL |
| `LOOPAD_DISCORD_WEBHOOK_URL` | Secret | Yes | Discord webhook URL |

HTTP health check는 `/health`에서 `200-399`를 반환해야 합니다.

#### Recommendation

Recommendation은 private service이며 Dashboard API에서 Cloud Map DNS로 호출합니다.

| Env | 종류 | 필수 | 설명 |
|---|---|---:|---|
| 공통 서버 env | Plain | Yes | 공통 실행 설정 |
| `LOOPAD_AURORA_HOST` | Plain | Yes | Aurora hostname |
| `LOOPAD_AURORA_PORT` | Plain | Yes | Aurora port |
| `LOOPAD_AURORA_DATABASE` | Plain | Yes | Aurora database |
| `LOOPAD_AURORA_USERNAME` | Secret | Yes | Aurora username |
| `LOOPAD_AURORA_PASSWORD` | Secret | Yes | Aurora password |
| `LOOPAD_CLICKHOUSE_URL` | Plain | Yes | recommendation feature 조회용 ClickHouse endpoint |
| `LOOPAD_CLICKHOUSE_USERNAME` | Secret | Yes | ClickHouse username |
| `LOOPAD_CLICKHOUSE_PASSWORD` | Secret | Yes | ClickHouse password |
| `LOOPAD_DATA_STORAGE_BUCKET` | Plain | Yes | GenAI 생성물 DataStorage bucket |
| `LOOPAD_GENAI_GENERATED_ASSETS_PREFIX` | Plain | Yes | `genai/generated/` |
| `LOOPAD_OPENAI_API_KEY` | Secret | Yes | OpenAI API key |

### Server Deploy Workflow

각 서버 repo는 자기 `.github/workflows/deploy.yml`을 직접 관리하고, 인프라 repo의 reusable workflow를 호출합니다.

```yaml
name: Deploy

on:
    push:
        branches: [main]

permissions:
    contents: read
    id-token: write

jobs:
    deploy:
        uses: krafton-jungle-project-4team/loop-ad_aws_cdk/.github/workflows/ecs-deploy.yml@v1
        with:
            environment: dev
            aws_region: ap-northeast-2
            aws_role_arn: arn:aws:iam::123456789012:role/github-actions-loop-ad-dev
            service_name: event-collector
            ecr_repository: loop-ad/event-collector
            ecs_cluster: dev-loop-ad-cluster
            ecs_service: dev-event-collector
            container_name: event-collector
            dockerfile: Dockerfile
            context: .
            image_tag: ${{ github.sha }}
            wait_for_service_stability: true
```

이 workflow는 runtime env나 secret 값을 정의하지 않습니다. 개발자는 Docker image 배포 설정만 맞추면 됩니다. runtime env와 secret 주입은 인프라 쪽 ECS task definition에서 관리하고, workflow는 image만 교체합니다.

## Frontend Static Site Repo

FE repo는 Docker image가 아니라 정적 파일을 빌드해서 S3에 업로드하고 CloudFront invalidation을 생성합니다. FE 개발자는 npm build 형식과 public env 검증 규칙을 맞춥니다.

### 필수 파일

```text
package.json
.github/workflows/deploy.yml
src/config/env.ts
```

### npm 명령 규칙

FE repo는 npm 기준으로 통일합니다.

```json
{
  "scripts": {
    "build": "vite build",
    "preview": "vite preview"
  }
}
```

배포 workflow는 아래 명령을 사용합니다.

```text
npm ci
npm run build
```

기본 build output directory는 `dist`입니다. 다른 directory를 써야 한다면 FE repo의 deploy workflow에서 `build_output_dir`를 명시적으로 바꿉니다.

### FE Env 규칙

FE env는 build 시점에 정적 파일 안으로 들어갈 수 있습니다. 따라서 브라우저에 노출되어도 되는 값만 사용합니다.

허용 예시:

```text
VITE_API_BASE_URL=https://api.dev.loop-ad.org
VITE_INGEST_BASE_URL=https://ingest.dev.loop-ad.org
```

금지 예시:

```text
VITE_OPENAI_API_KEY=...
VITE_DISCORD_WEBHOOK_URL=...
VITE_AURORA_PASSWORD=...
```

Vite 기준 env 검증 예시:

```ts
function requiredBuildEnv(name: keyof ImportMetaEnv): string {
    const value = import.meta.env[name];
    if (!value) {
        throw new Error(`${name} is required`);
    }
    return value;
}

export const frontendConfig = Object.freeze({
    apiBaseUrl: requiredBuildEnv('VITE_API_BASE_URL'),
});
```

FE도 fallback을 두지 않습니다.

금지 예시:

```ts
const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8080';
```

### Frontend Build Contract

Frontend는 ECS task가 아니므로 runtime env를 받지 않습니다. S3/CloudFront로 배포되는 정적 파일은 build 시점에 필요한 public 설정만 받습니다.

| 이름 | 예시 | 설명 |
|---|---|---|
| `VITE_API_BASE_URL` | `https://api.dev.loop-ad.org` | Dashboard API public base URL |
| `VITE_INGEST_BASE_URL` | `https://ingest.dev.loop-ad.org` | 필요 시 event ingest public base URL |

Frontend build env에는 secret을 넣지 않습니다. 브라우저 bundle에 들어가도 되는 값만 사용합니다.

### Frontend Deploy Workflow

각 FE repo는 자기 `.github/workflows/deploy.yml`에서 인프라 repo의 reusable workflow를 호출합니다.

```yaml
name: Deploy Frontend

on:
    push:
        branches: [main]

permissions:
    contents: read
    id-token: write

jobs:
    deploy:
        uses: krafton-jungle-project-4team/loop-ad_aws_cdk/.github/workflows/frontend-deploy.yml@v1
        with:
            environment: dev
            aws_region: ap-northeast-2
            aws_role_arn: arn:aws:iam::123456789012:role/github-actions-loop-ad-dev
            node_version: '20'
            working_directory: .
            install_command: npm ci
            build_command: npm run build
            build_output_dir: dist
            s3_bucket: loop-ad-dev-dashboard-web
            s3_prefix: .
            cloudfront_distribution_id: E1234567890ABC
            cloudfront_invalidation_paths: '/*'
            asset_cache_control: 'public,max-age=31536000,immutable'
            html_cache_control: 'no-cache,no-store,must-revalidate'
```

FE build env 값은 FE repo의 GitHub Environment variables로 관리할 수 있습니다. 단, FE build env는 브라우저에 노출될 수 있으므로 secret을 넣지 않습니다. secret이 필요한 기능은 FE에서 직접 처리하지 않고 backend API를 통해 호출합니다.

## Local Development

각 앱 repo는 `.env.example`을 둘 수 있습니다. 이 파일은 이름과 형식을 알려주는 용도이며 실제 secret 값을 담지 않습니다.

예시:

```text
LOOPAD_ENV=dev
LOOPAD_SERVICE_ID=dashboard-api
PORT=80
LOOPAD_AURORA_HOST=replace-me
LOOPAD_AURORA_PASSWORD=replace-me
```

실제 local 값은 `.env.local` 또는 개인 shell 환경에서 관리하고 commit하지 않습니다.

## 인프라에서 제공하는 값

아래 값들은 개발자가 앱 repo나 workflow에 직접 넣지 않습니다. 인프라 쪽에서 ECS task definition에 주입합니다.

| 값 | 개발자 처리 |
|---|---|
| DB endpoint, internal service URL, topic 이름 | 문서에 정의된 env 이름으로 읽습니다. |
| DB username/password, webhook URL, API key | secret env 이름으로 읽고 로그에 남기지 않습니다. |
| ECS service, ECR repository, S3 bucket, CloudFront distribution | deploy workflow의 input으로만 사용합니다. |

Reusable ECS deploy workflow는 기존 task definition을 가져와 container image만 교체합니다. 따라서 각 애플리케이션 repo의 workflow가 runtime env나 secret을 다시 정의하지 않습니다.

## Review Checklist

새 앱 repo를 만들거나 배포 전에 아래 항목을 확인합니다.

- 필수 env에 fallback/default가 없는가?
- env 검증이 서버 시작 또는 FE build 시작 시점에 실행되는가?
- 검증된 값이 config 객체로 모이는가?
- secret이 Dockerfile, image build arg, GitHub Actions env, FE bundle에 들어가지 않는가?
- 서버 repo는 Dockerfile과 ECS deploy workflow를 갖고 있는가?
- FE repo는 `npm ci`, `npm run build`, `dist` 기준을 따르는가?
- FE env는 브라우저에 노출되어도 되는 값만 사용하는가?
- 각 repo workflow는 `uses:`로 인프라 repo reusable workflow를 호출하는가?

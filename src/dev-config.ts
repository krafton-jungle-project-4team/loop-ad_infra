import * as logs from 'aws-cdk-lib/aws-logs';
import { normalizeSecretPrefix } from './config-validation';

// dev 인프라의 공통 계약 값입니다.
// 스택 곳곳에 숫자/이름을 흩어 두면 비용과 앱 env 계약 변경 범위를 추적하기 어려워 한 파일에 모읍니다.
export const LOOP_AD_REGION = 'ap-northeast-2';

export const DEV_EVENT_COLLECTOR_FARGATE_CAPACITY = {
    cpu: 256,
    memoryLimitMiB: 512,
    desiredTasks: 1,
    minTasks: 1,
    maxTasks: 4,
} as const;
export const DEV_DASHBOARD_API_FARGATE_CAPACITY = {
    cpu: 512,
    memoryLimitMiB: 1024,
    desiredTasks: 1,
    minTasks: 1,
    maxTasks: 2,
} as const;
export const DEV_DECISION_API_FARGATE_CAPACITY = {
    cpu: 1024,
    memoryLimitMiB: 2048,
    desiredTasks: 1,
    minTasks: 1,
    maxTasks: 1,
} as const;
export const SERVICE_CPU_SCALE_TARGET_PERCENT = 70;
export const DEV_AURORA_MIN_ACU = 0;
export const DEV_AURORA_MAX_ACU = 4;
export const DEV_AURORA_AUTO_PAUSE_MINUTES = 10;
export const DEV_CLICKHOUSE_INSTANCE_TYPE = 't4g.medium';
export const DEV_CLICKHOUSE_VOLUME_GIB = 100;
export const DEV_KAFKA_INSTANCE_TYPE = 't4g.small';
export const DEV_KAFKA_VOLUME_GIB = 40;
export const DEV_CLICKHOUSE_IMAGE = 'clickhouse/clickhouse-server:26.3.13.31';
export const DEV_AL2023_ARM64_AMI_SSM_PARAMETER = '/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64';
export const DEV_KAFKA_VERSION = '3.9.1';
export const DEV_KAFKA_SCALA_VERSION = '2.13';
export const DEV_KAFKA_SCRAM_PORT = 9094;
export const DEV_CLICKHOUSE_HTTP_PORT = 8123;
export const DEV_AURORA_PORT = 5432;
export const DEV_VPC_AVAILABILITY_ZONES = ['ap-northeast-2a', 'ap-northeast-2c'];
export const DEV_ECS_LOG_GROUP_PREFIX = '/loop-ad/dev/ecs';
export const DEV_LOG_RETENTION = logs.RetentionDays.THREE_MONTHS;
export const AURORA_DATABASE_NAME = 'loopad';
export const CLICKHOUSE_DATABASE_NAME = 'loopad';
export const EVENT_TOPIC_NAME = 'loop-ad.events.raw';
export const GENAI_ASSETS_BASE_PREFIX = 'genai/';
export const DASHBOARD_DISPATCH_EMAIL_IDENTITY_NAME = 'loop-ad.org';
export const DASHBOARD_DISPATCH_EMAIL_FROM_ADDRESS = 'noreply@loop-ad.org';
export const EVENT_COLLECTOR_API_RECORD_NAME = 'event.api.dev';
export const DASHBOARD_API_RECORD_NAME = 'dashboard.api.dev';
export const DECISION_API_RECORD_NAME = 'decision.api.dev';
export const GENAI_PUBLIC_ASSETS_RECORD_NAME = 'gen-ai.asset.dev';
export const DASHBOARD_WEB_RECORD_NAME = 'dashboard.dev';
export const DEMO_SHOPPINGMALL_WEB_RECORD_NAME = 'demo-shoppingmall.dev';

// Runtime stack은 repository 이름만 import합니다.
// repository stack을 먼저 배포한 뒤 각 앱이 같은 이름으로 이미지를 push하는 흐름을 고정하기 위한 목록입니다.
export const DEV_APPLICATION_REPOSITORIES = [
    { id: 'EventCollectorRepository', repositoryName: 'loop-ad/event-collector', outputId: 'EventCollectorRepositoryUri' },
    { id: 'DashboardApiRepository', repositoryName: 'loop-ad/dashboard-api', outputId: 'DashboardApiRepositoryUri' },
    { id: 'DecisionApiRepository', repositoryName: 'loop-ad/decision-api', outputId: 'DecisionApiRepositoryUri' },
] as const;

export interface DeveloperAllowlistConfig {
    readonly ipv4Cidrs: readonly string[];
    readonly ipv6Cidrs: readonly string[];
}

export interface PublicHostedZoneConfig {
    readonly hostedZoneId: string;
    readonly domainName: string;
}

export interface LoopAdDevCertificateArns {
    readonly frontendSitesCertificateArn: string;
    readonly genAiGeneratedAssetsCertificateArn: string;
}

export interface LoopAdDevDataSecretNames {
    readonly auroraCredentialsSecretName: string;
    readonly clickHouseCredentialsSecretName: string;
    readonly kafkaAppUserSecretName: string;
    readonly kafkaBrokerUserSecretName: string;
}

export interface LoopAdDevRuntimeSecretNames {
    readonly openAiApiKeySecretName: string;
    readonly geminiApiKeySecretName: string;
    readonly internalApiKeySecretName: string;
    readonly demoDispatchRecipientsSecretName: string;
}

// Data stack과 Runtime stack이 같은 prefix에서 secret 이름을 파생하므로 하나의 타입으로 묶어 전달합니다.
export interface LoopAdDevSecretNames extends LoopAdDevDataSecretNames, LoopAdDevRuntimeSecretNames {}

// prefix만 바뀌어도 앱/런타임 계약이 함께 움직이도록 suffix는 코드에서 고정합니다.
// secret 값은 CDK가 만들거나 읽지 않고, Secrets stack이 만든 빈 secret에 동기화 스크립트가 나중에 값을 넣습니다.
export function buildDevSecretNames(secretPrefix: string): LoopAdDevSecretNames {
    const prefix = normalizeSecretPrefix(secretPrefix);

    return {
        auroraCredentialsSecretName: `${prefix}/aurora/credentials`,
        clickHouseCredentialsSecretName: `${prefix}/clickhouse/credentials`,
        kafkaAppUserSecretName: `${prefix}/kafka/app-user`,
        kafkaBrokerUserSecretName: `${prefix}/kafka/broker-user`,
        openAiApiKeySecretName: `${prefix}/openai/api-key`,
        geminiApiKeySecretName: `${prefix}/gemini/api-key`,
        internalApiKeySecretName: `${prefix}/internal/api-key`,
        demoDispatchRecipientsSecretName: `${prefix}/dashboard-api/demo-dispatch-recipients`,
    };
}

import * as logs from 'aws-cdk-lib/aws-logs';

export const LOOP_AD_REGION = 'ap-northeast-2';

export const DEV_SERVICE_DESIRED_TASKS = 1;
export const DEV_SERVICE_MIN_TASKS = 1;
export const DEV_SERVICE_MAX_TASKS = 2;
export const SERVICE_CPU_SCALE_TARGET_PERCENT = 70;
export const DEV_AURORA_MIN_ACU = 0;
export const DEV_AURORA_MAX_ACU = 2;
export const DEV_AURORA_AUTO_PAUSE_MINUTES = 10;
export const DEV_CLICKHOUSE_VOLUME_GIB = 50;
export const DEV_KAFKA_VOLUME_GIB = 20;
export const DEV_CLICKHOUSE_IMAGE = 'clickhouse/clickhouse-server:26.3.13.31';
export const DEV_KAFKA_VERSION = '3.9.1';
export const DEV_KAFKA_SCALA_VERSION = '2.13';
export const DEV_VALKEY_MAJOR_ENGINE_VERSION = '7';
export const DEV_VALKEY_MAX_DATA_STORAGE_GB = 1;
export const DEV_VALKEY_MAX_ECPU_PER_SECOND = 1000;
export const DEV_VPC_AVAILABILITY_ZONES = ['ap-northeast-2a', 'ap-northeast-2c'];
export const DEV_ECS_LOG_GROUP_PREFIX = '/loop-ad/dev/ecs';
export const DEV_LOG_RETENTION = logs.RetentionDays.THREE_MONTHS;
export const AURORA_DATABASE_NAME = 'loopad';
export const EVENT_TOPIC_NAME = 'loop-ad.events.raw';
export const GENAI_GENERATED_ASSETS_PREFIX = 'genai/generated/';
export const PUBLIC_API_RECORD_NAME = 'api.dev';
export const PUBLIC_INGEST_RECORD_NAME = 'ingest.dev';
export const GENAI_PUBLIC_ASSETS_RECORD_NAME = 'gen-ai.asset.dev';
export const DASHBOARD_WEB_RECORD_NAME = 'dashboard.dev';
export const DEMO_SHOPPINGMALL_WEB_RECORD_NAME = 'demo-shoppingmall.dev';
export const OPENAI_API_KEY_PARAMETER_NAME = '/loop-ad/dev/external/openai/api-key';

export const DEV_APPLICATION_REPOSITORIES = [
    { id: 'EventCollectorRepository', repositoryName: 'loop-ad/event-collector', outputId: 'EventCollectorRepositoryUri' },
    { id: 'AdvertisementApiRepository', repositoryName: 'loop-ad/advertisement-api', outputId: 'AdvertisementApiRepositoryUri' },
    { id: 'DashboardApiRepository', repositoryName: 'loop-ad/dashboard-api', outputId: 'DashboardApiRepositoryUri' },
    { id: 'DecisionApiRepository', repositoryName: 'loop-ad/decision-api', outputId: 'DecisionApiRepositoryUri' },
] as const;

export interface PublicHostedZoneConfig {
    readonly hostedZoneId: string;
    readonly domainName: string;
}

// Certificate stack의 출력값입니다.
// 서로 다른 region stack을 직접 참조하지 않고 ARN만 넘겨 data/runtime stack synth를 단순하게 유지합니다.
export interface LoopAdDevCertificateArns {
    readonly frontendSitesCertificateArn: string;
    readonly genAiGeneratedAssetsCertificateArn: string;
}

import * as cdk from 'aws-cdk-lib';
import { parse as parseDotenv } from 'dotenv';
import { existsSync, readFileSync } from 'node:fs';
import { z } from 'zod';
import { parseDeveloperCidrs } from './config-validation';
import {
    buildDevSecretNames,
    type DeveloperAllowlistConfig,
    type LoopAdDevCertificateArns,
    type LoopAdDevSecretNames,
    type PublicHostedZoneConfig,
} from './dev-config';

export const ENVIRONMENT_NAMES = [
    'dev',
    'dev-certificate',
    'dev-repositories',
    'dev-secrets',
    'dev-network',
    'dev-data',
    'dev-runtime',
] as const;

export type EnvironmentName = typeof ENVIRONMENT_NAMES[number];

// bin은 CDK app 실행만 담당하고, 이 객체가 어떤 stack 조합을 합성할지에 필요한 설정을 담습니다.
// 일부 stack만 합성할 수 있어 optional 필드를 두되, 사용 직전 requireConfig로 빠르게 실패시킵니다.
export interface CdkAppConfig {
    readonly environmentName: EnvironmentName;
    readonly stackEnv: cdk.Environment;
    readonly certificateStackEnv: cdk.Environment;
    readonly publicHostedZone?: PublicHostedZoneConfig;
    readonly developerAllowlist?: DeveloperAllowlistConfig;
    readonly secretNames?: LoopAdDevSecretNames;
    readonly genAiGeneratedAssetsCertificateArn?: string;
    readonly certificateArns?: LoopAdDevCertificateArns;
}

type MutableCdkAppConfig = {
    -readonly [Key in keyof CdkAppConfig]: CdkAppConfig[Key];
};

interface ParsedEnvValues {
    readonly CDK_DEFAULT_ACCOUNT: string;
    readonly LOOP_AD_REGION: string;
    readonly LOOP_AD_PUBLIC_DOMAIN_NAME?: string;
    readonly LOOP_AD_PUBLIC_HOSTED_ZONE_ID?: string;
    readonly LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN?: string;
    readonly LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN?: string;
    readonly LOOP_AD_SECRET_PREFIX?: string;
    readonly LOOP_AD_DEVELOPER_IPV4_CIDRS?: string;
    readonly LOOP_AD_DEVELOPER_IPV6_CIDRS?: string;
}

const NON_SECRET_DOTENV_KEYS = new Set([
    'CDK_DEFAULT_ACCOUNT',
    'LOOP_AD_REGION',
    'LOOP_AD_PUBLIC_DOMAIN_NAME',
    'LOOP_AD_PUBLIC_HOSTED_ZONE_ID',
    'LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN',
    'LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN',
    'LOOP_AD_SECRET_PREFIX',
    'LOOP_AD_DEVELOPER_IPV4_CIDRS',
    'LOOP_AD_DEVELOPER_IPV6_CIDRS',
]);

// zod 스키마는 환경변수 누락을 stack 생성 전에 명확한 메시지로 끊어내기 위한 방어선입니다.
// secret 값 자체는 여기에 포함하지 않고, CDK가 알아야 하는 이름/ARN/도메인 메타데이터만 허용합니다.
const environmentNameSchema = z.enum(ENVIRONMENT_NAMES);
const explicitBlankableEnvValue = z.preprocess(
    (value) => typeof value === 'string' ? value : undefined,
    z.string().transform((value) => value.trim()),
);

const baseEnvSchema = z.object({
    CDK_DEFAULT_ACCOUNT: requiredEnvValue('CDK_DEFAULT_ACCOUNT'),
    LOOP_AD_REGION: requiredEnvValue('LOOP_AD_REGION'),
});
const secretEnvSchema = z.object({
    LOOP_AD_SECRET_PREFIX: requiredEnvValue('LOOP_AD_SECRET_PREFIX'),
});
const developerAllowlistEnvSchema = z.object({
    LOOP_AD_DEVELOPER_IPV4_CIDRS: explicitBlankableEnvValue,
    LOOP_AD_DEVELOPER_IPV6_CIDRS: explicitBlankableEnvValue,
});
const publicHostedZoneEnvSchema = z.object({
    LOOP_AD_PUBLIC_DOMAIN_NAME: requiredEnvValue('LOOP_AD_PUBLIC_DOMAIN_NAME'),
    LOOP_AD_PUBLIC_HOSTED_ZONE_ID: requiredEnvValue('LOOP_AD_PUBLIC_HOSTED_ZONE_ID'),
});
const genAiCertificateEnvSchema = z.object({
    LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN: requiredEnvValue('LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN'),
});
const runtimeCertificateEnvSchema = z.object({
    LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN: requiredEnvValue('LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN'),
    LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN: requiredEnvValue('LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN'),
});

export function readCdkAppConfig(app: cdk.App): CdkAppConfig {
    // 로컬 개발 편의를 위해 .env를 읽지만, 허용 목록에 든 비민감 키만 process.env에 복사합니다.
    // .env.secrets는 별도 동기화 스크립트 입력이므로 CDK synth 경로에서는 읽지 않습니다.
    loadNonSecretDotenv();

    const environmentName = parseEnvironmentName(app.node.tryGetContext('environment'));
    const envValues = parseEnvironmentEnv(environmentName);
    const config: MutableCdkAppConfig = {
        environmentName,
        stackEnv: {
            account: envValues.CDK_DEFAULT_ACCOUNT,
            region: envValues.LOOP_AD_REGION,
        },
        certificateStackEnv: {
            account: envValues.CDK_DEFAULT_ACCOUNT,
            region: 'us-east-1',
        },
    };

    if (envValues.LOOP_AD_PUBLIC_DOMAIN_NAME && envValues.LOOP_AD_PUBLIC_HOSTED_ZONE_ID) {
        config.publicHostedZone = {
            hostedZoneId: envValues.LOOP_AD_PUBLIC_HOSTED_ZONE_ID,
            domainName: envValues.LOOP_AD_PUBLIC_DOMAIN_NAME,
        };
    }

    if (envValues.LOOP_AD_SECRET_PREFIX) {
        config.secretNames = buildDevSecretNames(envValues.LOOP_AD_SECRET_PREFIX);
    }

    if (hasDeveloperAllowlistConfig(environmentName)) {
        config.developerAllowlist = {
            ipv4Cidrs: parseDeveloperCidrs(
                requiredParsedEnvValue(envValues.LOOP_AD_DEVELOPER_IPV4_CIDRS, 'LOOP_AD_DEVELOPER_IPV4_CIDRS'),
                'LOOP_AD_DEVELOPER_IPV4_CIDRS',
                4,
            ),
            ipv6Cidrs: parseDeveloperCidrs(
                requiredParsedEnvValue(envValues.LOOP_AD_DEVELOPER_IPV6_CIDRS, 'LOOP_AD_DEVELOPER_IPV6_CIDRS'),
                'LOOP_AD_DEVELOPER_IPV6_CIDRS',
                6,
            ),
        };
    }

    if (envValues.LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN) {
        config.genAiGeneratedAssetsCertificateArn = envValues.LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN;
    }

    if (envValues.LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN && envValues.LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN) {
        config.certificateArns = {
            frontendSitesCertificateArn: envValues.LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN,
            genAiGeneratedAssetsCertificateArn: envValues.LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN,
        };
    }

    return config;
}

function parseEnvironmentName(value: unknown): EnvironmentName {
    const result = environmentNameSchema.safeParse(value);
    if (!result.success) {
        throw new Error(`Missing or invalid CDK context "environment". Pass one of: ${ENVIRONMENT_NAMES.join(', ')}.`);
    }

    return result.data;
}

function parseEnvironmentEnv(environmentName: EnvironmentName): ParsedEnvValues {
    // environment context마다 필요한 값이 다르므로 stack 경계와 같은 단위로 스키마를 선택합니다.
    // 예를 들어 repository stack은 도메인/secret 이름이 없어도 합성 가능해야 합니다.
    const result = envSchemaFor(environmentName).safeParse(process.env);
    if (!result.success) {
        throw new Error(`Invalid environment for ${environmentName}: ${formatZodIssues(result.error)}`);
    }

    return result.data as ParsedEnvValues;
}

function envSchemaFor(environmentName: EnvironmentName) {
    switch (environmentName) {
        case 'dev-certificate':
            return baseEnvSchema.merge(publicHostedZoneEnvSchema);
        case 'dev-repositories':
            return baseEnvSchema;
        case 'dev-secrets':
            return baseEnvSchema.merge(secretEnvSchema);
        case 'dev-network':
            return baseEnvSchema.merge(developerAllowlistEnvSchema);
        case 'dev-data':
            return baseEnvSchema
                .merge(secretEnvSchema)
                .merge(developerAllowlistEnvSchema)
                .merge(publicHostedZoneEnvSchema)
                .merge(genAiCertificateEnvSchema);
        case 'dev-runtime':
        case 'dev':
            return baseEnvSchema
                .merge(secretEnvSchema)
                .merge(developerAllowlistEnvSchema)
                .merge(publicHostedZoneEnvSchema)
                .merge(runtimeCertificateEnvSchema);
    }
}

function hasDeveloperAllowlistConfig(environmentName: EnvironmentName): boolean {
    // Security Group을 만드는 합성 경로에서만 allowlist를 요구합니다.
    // 빈 문자열도 "직접 접근 없음"이라는 명시 설정으로 처리되므로 변수 자체는 반드시 있어야 합니다.
    return environmentName === 'dev' ||
        environmentName === 'dev-network' ||
        environmentName === 'dev-data' ||
        environmentName === 'dev-runtime';
}

function requiredEnvValue(key: string) {
    // fallback 기본값을 두지 않고 배포자가 의도를 명시하게 합니다.
    // 잘못된 계정/리전에 조용히 합성되는 것을 막는 쪽이 dev 인프라에서도 더 안전합니다.
    return z.preprocess(
        (value) => typeof value === 'string' ? value : '',
        z.string().trim().min(1, `${key} is required.`),
    );
}

function requiredParsedEnvValue(value: string | undefined, key: string): string {
    if (value === undefined) {
        throw new Error(`Missing parsed environment value: ${key}.`);
    }

    return value;
}

function formatZodIssues(error: z.ZodError): string {
    return error.issues
        .map((issue) => `${issue.path.join('.') || 'value'}: ${issue.message}`)
        .join('; ');
}

function loadNonSecretDotenv(): void {
    if (!existsSync('.env')) {
        return;
    }

    const parsed = parseDotenv(readFileSync('.env', 'utf8'));

    for (const [key, value] of Object.entries(parsed)) {
        // 레거시 또는 실수로 추가된 시크릿 값 키가 CDK 프로세스 환경에 들어오지 않게 합니다.
        // 이미 shell에서 지정한 값은 CI나 일회성 명령의 명시 설정으로 보고 덮어쓰지 않습니다.
        if (NON_SECRET_DOTENV_KEYS.has(key) && process.env[key] === undefined) {
            process.env[key] = value;
        }
    }
}

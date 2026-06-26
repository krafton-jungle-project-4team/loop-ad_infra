#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { config as loadDotenv } from 'dotenv';
import { existsSync } from 'node:fs';
import {
    LOOP_AD_REGION,
    LoopAdDevCertificateStack,
    LoopAdDevNetworkStack,
    LoopAdDevStack,
} from '../src/loop-ad-stack';

// 로컬 개발은 .env를 쓰고, CI/CD는 process environment로 값을 주입합니다.
// .env 파일 자체는 선택으로 두되 필수 값 검증은 readRequiredEnv에서 일관되게 처리합니다.
const dotenvResult = existsSync('.env') ? loadDotenv({ path: '.env', quiet: true }) : undefined;
if (dotenvResult?.error) {
    throw new Error(`Failed to load .env: ${dotenvResult.error.message}`);
}

const app = new cdk.App();

const environmentName = readEnvironmentName(app);
const env = {
    account: readRequiredEnv('CDK_DEFAULT_ACCOUNT'),
    region: LOOP_AD_REGION,
};
const publicHostedZone = {
    hostedZoneId: readRequiredEnv('LOOP_AD_PUBLIC_HOSTED_ZONE_ID'),
    domainName: readRequiredEnv('LOOP_AD_PUBLIC_DOMAIN_NAME'),
};

if (environmentName === 'dev-certificate') {
    // CloudFront용 ACM certificate는 us-east-1에 있어야 하므로 별도 stack으로 먼저 배포합니다.
    // 출력된 ARN은 .env 또는 CI secret/input으로 app stack에 전달합니다.
    new LoopAdDevCertificateStack(app, 'LoopAdDevCertificateStack', {
        env: {
            account: env.account,
            region: 'us-east-1',
        },
        publicHostedZone,
    });
} else {
    // dev 환경은 네트워크 기반 stack과 애플리케이션 stack을 함께 합성/배포합니다.
    // 최초 배포 전 분리하는 구조라 기존 리소스 이동으로 인한 교체 이슈는 없습니다.
    const networkStack = new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env,
    });
    new LoopAdDevStack(app, 'LoopAdDevStack', {
        env,
        publicHostedZone,
        network: networkStack,
        certificateArns: {
            frontendSitesCertificateArn: readRequiredEnv('LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN'),
            genAiGeneratedAssetsCertificateArn: readRequiredEnv('LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN'),
        },
    });
}

cdk.Tags.of(app).add('Project', 'loop-ad');
cdk.Tags.of(app).add('CdkProject', 'loop-ad_aws_cdk');
cdk.Tags.of(app).add('Environment', environmentName);

function readEnvironmentName(app: cdk.App): 'dev' | 'dev-certificate' {
    const value = app.node.tryGetContext('environment');
    if (!value) {
        throw new Error('Missing required CDK context "environment". Pass -c environment=dev or -c environment=dev-certificate.');
    }

    if (value !== 'dev' && value !== 'dev-certificate') {
        throw new Error(`environment context must be "dev" or "dev-certificate". Received: ${value}`);
    }

    return value;
}

function readRequiredEnv(key: string): string {
    const value = process.env[key]?.trim();
    if (!value) {
        throw new Error(`Missing required environment variable ${key}. Define it in .env or the process environment.`);
    }

    return value;
}

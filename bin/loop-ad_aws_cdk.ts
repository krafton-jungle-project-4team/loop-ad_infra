#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { config as loadDotenv } from 'dotenv';
import { existsSync } from 'node:fs';
import {
    LOOP_AD_REGION,
    LoopAdDevCertificateStack,
    LoopAdDevDataStack,
    LoopAdDevNetworkStack,
    LoopAdDevRepositoryStack,
    LoopAdDevRuntimeStack,
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

if (environmentName === 'dev-certificate') {
    const publicHostedZone = readPublicHostedZoneConfig();

    // CloudFront용 ACM certificate는 us-east-1에 있어야 하므로 별도 stack으로 먼저 배포합니다.
    // 출력된 ARN은 .env 또는 CI secret/input으로 data/runtime stack에 전달합니다.
    new LoopAdDevCertificateStack(app, 'LoopAdDevCertificateStack', {
        env: {
            account: env.account,
            region: 'us-east-1',
        },
        publicHostedZone,
    });
} else if (environmentName === 'dev-repositories') {
    // ECR repository는 ECS보다 먼저 배포합니다.
    // 각 앱 repo가 image를 push한 뒤 runtime stack을 올리면 초기 배포의 image not found 위험을 줄일 수 있습니다.
    new LoopAdDevRepositoryStack(app, 'LoopAdDevRepositoryStack', {
        env,
    });
} else if (environmentName === 'dev-network') {
    // VPC/network는 변경 주기가 길어 data/runtime stack보다 먼저 독립적으로 배포할 수 있습니다.
    new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env,
    });
} else if (environmentName === 'dev-data') {
    const publicHostedZone = readPublicHostedZoneConfig();
    const networkStack = new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env,
    });
    // Data stack은 DB/cache/broker/storage와 endpoint contract를 runtime보다 먼저 배포합니다.
    new LoopAdDevDataStack(app, 'LoopAdDevDataStack', {
        env,
        publicHostedZone,
        network: networkStack,
        genAiGeneratedAssetsCertificateArn: readRequiredEnv('LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN'),
    });
} else if (environmentName === 'dev-runtime') {
    const publicHostedZone = readPublicHostedZoneConfig();
    const networkStack = new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env,
    });
    const dataStack = new LoopAdDevDataStack(app, 'LoopAdDevDataStack', {
        env,
        publicHostedZone,
        network: networkStack,
        genAiGeneratedAssetsCertificateArn: readRequiredEnv('LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN'),
    });
    // Runtime stack은 앱 image와 data 초기화가 준비된 뒤 ECS/ingress를 올립니다.
    new LoopAdDevRuntimeStack(app, 'LoopAdDevRuntimeStack', {
        env,
        publicHostedZone,
        network: networkStack,
        data: dataStack,
        certificateArns: {
            frontendSitesCertificateArn: readRequiredEnv('LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN'),
            genAiGeneratedAssetsCertificateArn: readRequiredEnv('LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN'),
        },
    });
} else {
    const publicHostedZone = readPublicHostedZoneConfig();

    // dev 환경은 네트워크, 데이터, 런타임 stack을 함께 합성/배포합니다.
    // 최초 배포 전 분리하는 구조라 기존 리소스 이동으로 인한 교체 이슈는 없습니다.
    const networkStack = new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env,
    });
    const dataStack = new LoopAdDevDataStack(app, 'LoopAdDevDataStack', {
        env,
        publicHostedZone,
        network: networkStack,
        genAiGeneratedAssetsCertificateArn: readRequiredEnv('LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN'),
    });
    new LoopAdDevRuntimeStack(app, 'LoopAdDevRuntimeStack', {
        env,
        publicHostedZone,
        network: networkStack,
        data: dataStack,
        certificateArns: {
            frontendSitesCertificateArn: readRequiredEnv('LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN'),
            genAiGeneratedAssetsCertificateArn: readRequiredEnv('LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN'),
        },
    });
}

cdk.Tags.of(app).add('Project', 'loop-ad');
cdk.Tags.of(app).add('CdkProject', 'loop-ad_aws_cdk');
cdk.Tags.of(app).add('Environment', environmentName === 'dev-repositories' || environmentName === 'dev-network' || environmentName === 'dev-data' || environmentName === 'dev-runtime' ? 'dev' : environmentName);

function readEnvironmentName(app: cdk.App): 'dev' | 'dev-certificate' | 'dev-repositories' | 'dev-network' | 'dev-data' | 'dev-runtime' {
    const value = app.node.tryGetContext('environment');
    if (!value) {
        throw new Error('Missing required CDK context "environment". Pass -c environment=dev, dev-certificate, dev-repositories, dev-network, dev-data, or dev-runtime.');
    }

    if (value !== 'dev' && value !== 'dev-certificate' && value !== 'dev-repositories' && value !== 'dev-network' && value !== 'dev-data' && value !== 'dev-runtime') {
        throw new Error(`environment context must be "dev", "dev-certificate", "dev-repositories", "dev-network", "dev-data", or "dev-runtime". Received: ${value}`);
    }

    return value;
}

function readPublicHostedZoneConfig() {
    return {
        hostedZoneId: readRequiredEnv('LOOP_AD_PUBLIC_HOSTED_ZONE_ID'),
        domainName: readRequiredEnv('LOOP_AD_PUBLIC_DOMAIN_NAME'),
    };
}

function readRequiredEnv(key: string): string {
    const value = process.env[key]?.trim();
    if (!value) {
        throw new Error(`Missing required environment variable ${key}. Define it in .env or the process environment.`);
    }

    return value;
}

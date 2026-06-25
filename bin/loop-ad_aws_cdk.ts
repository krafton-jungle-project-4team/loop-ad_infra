#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { config as loadDotenv } from 'dotenv';
import { LOOP_AD_REGION, LoopAdDevStack, LoopAdPerfStack } from '../src/loop-ad-stack';

const dotenvResult = loadDotenv({ path: '.env', quiet: true });
if (dotenvResult.error) {
    throw new Error(`Failed to load .env: ${dotenvResult.error.message}`);
}

const app = new cdk.App();

const environmentName = readEnvironmentName(app);
const env = {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: LOOP_AD_REGION,
};
const publicHostedZone = {
    hostedZoneId: readRequiredEnv('LOOP_AD_PUBLIC_HOSTED_ZONE_ID'),
    domainName: readRequiredEnv('LOOP_AD_PUBLIC_DOMAIN_NAME'),
};

if (environmentName === 'dev') {
    new LoopAdDevStack(app, 'LoopAdDevStack', {
        env,
        publicHostedZone,
    });
}

if (environmentName === 'perf') {
    new LoopAdPerfStack(app, 'LoopAdPerfStack', {
        env,
        publicHostedZone,
    });
}

cdk.Tags.of(app).add('Project', 'loop-ad');
cdk.Tags.of(app).add('CdkProject', 'loop-ad_aws_cdk');
cdk.Tags.of(app).add('Environment', environmentName);

function readEnvironmentName(app: cdk.App): 'dev' | 'perf' {
    const value = app.node.tryGetContext('environment') ?? 'dev';
    if (value !== 'dev' && value !== 'perf') {
        throw new Error(`environment context must be "dev" or "perf". Received: ${value}`);
    }

    return value;
}

function readRequiredEnv(key: string): string {
    const value = process.env[key]?.trim();
    if (!value) {
        throw new Error(`Missing required environment variable ${key}. Define it in .env.`);
    }

    return value;
}

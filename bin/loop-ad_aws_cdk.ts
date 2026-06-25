#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { config as loadDotenv } from 'dotenv';
import { LOOP_AD_REGION, LoopAdDevStack } from '../src/loop-ad-stack';

const dotenvResult = loadDotenv({ path: '.env', quiet: true });
if (dotenvResult.error) {
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

if (environmentName === 'dev') {
    new LoopAdDevStack(app, 'LoopAdDevStack', {
        env,
        publicHostedZone,
    });
}

cdk.Tags.of(app).add('Project', 'loop-ad');
cdk.Tags.of(app).add('CdkProject', 'loop-ad_aws_cdk');
cdk.Tags.of(app).add('Environment', environmentName);

function readEnvironmentName(app: cdk.App): 'dev' {
    const value = app.node.tryGetContext('environment');
    if (!value) {
        throw new Error('Missing required CDK context "environment". Pass -c environment=dev.');
    }

    if (value !== 'dev') {
        throw new Error(`environment context must be "dev". Received: ${value}`);
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

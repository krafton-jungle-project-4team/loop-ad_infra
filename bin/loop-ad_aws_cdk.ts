#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { LOOP_AD_REGION, LoopAdDevStack, LoopAdPerfStack } from '../src/loop-ad-stack';

const app = new cdk.App();

const environmentName = readEnvironmentName(app);
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: LOOP_AD_REGION,
};

if (environmentName === 'dev') {
  new LoopAdDevStack(app, 'LoopAdDevStack', {
    env,
    enableNatGateway: readBooleanContext(app, 'enableNatGateway', false),
  });
}

if (environmentName === 'perf') {
  new LoopAdPerfStack(app, 'LoopAdPerfStack', {
    env,
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

function readBooleanContext(app: cdk.App, key: string, defaultValue: boolean): boolean {
  const value = app.node.tryGetContext(key);
  if (value === undefined) {
    return defaultValue;
  }

  return value === true || value === 'true' || value === '1';
}

#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { buildLoopAdEnvironment } from '../src/app/build-loop-ad-environment';
import { ENVIRONMENT_MODES, LOOP_AD_REGION, type EnvironmentName } from '../src/config/loop-ad-config';

const app = new cdk.App();

const environmentName = readEnvironmentName(app);
const mode = ENVIRONMENT_MODES[environmentName];

buildLoopAdEnvironment(app, {
  mode,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: LOOP_AD_REGION,
  },
  enableNatGateway: readBooleanContext(app, 'enableNatGateway', mode.enableNatGatewayByDefault),
  enableVpcEndpoints: readBooleanContext(app, 'enableVpcEndpoints', mode.enableVpcEndpointsByDefault),
});

cdk.Tags.of(app).add('Project', 'loop-ad');
cdk.Tags.of(app).add('CdkProject', 'loop-ad_aws_cdk');
cdk.Tags.of(app).add('Environment', mode.name);
cdk.Tags.of(app).add('DraftOnly', 'true');

function readEnvironmentName(app: cdk.App): EnvironmentName {
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

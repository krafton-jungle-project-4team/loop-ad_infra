import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import {
    LOOP_AD_REGION,
    LoopAdDevCertificateStack,
    LoopAdDevCostGuardrailStack,
    LoopAdDevDataStack,
    LoopAdDevNetworkStack,
    LoopAdDevRepositoryStack,
    LoopAdDevRuntimeStack,
} from '../src/loop-ad-stack';

const ROOT = join(__dirname, '..');
const SRC_DIR = join(ROOT, 'src');
const testEnv = {
    account: '123456789012',
    region: LOOP_AD_REGION,
};
const testPublicHostedZone = {
    hostedZoneId: 'ZTESTHOSTEDZONEID',
    domainName: 'example.test',
};
const testCertificateArns = {
    frontendSitesCertificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/frontend-sites',
    genAiGeneratedAssetsCertificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/gen-ai-assets',
};

describe('loop-ad CDK guardrails', () => {
    it('keeps the cost guardrail as a separate monthly budget stack', () => {
        const template = Template.fromStack(synthCostGuardrail());

        template.resourceCountIs('AWS::Budgets::Budget', 1);
        template.hasResourceProperties('AWS::Budgets::Budget', {
            Budget: {
                BudgetName: 'loop-ad-dev-monthly-budget',
                BudgetType: 'COST',
                TimeUnit: 'MONTHLY',
                BudgetLimit: {
                    Amount: 300,
                    Unit: 'USD',
                },
                CostTypes: {
                    IncludeCredit: false,
                    IncludeRefund: false,
                    UseBlended: false,
                },
            },
            NotificationsWithSubscribers: Match.arrayWith([
                Match.objectLike({
                    Notification: {
                        NotificationType: 'ACTUAL',
                        ComparisonOperator: 'GREATER_THAN',
                        Threshold: 80,
                        ThresholdType: 'PERCENTAGE',
                    },
                    Subscribers: [
                        {
                            SubscriptionType: 'EMAIL',
                            Address: 'alerts@example.test',
                        },
                    ],
                }),
                Match.objectLike({
                    Notification: {
                        NotificationType: 'FORECASTED',
                        ComparisonOperator: 'GREATER_THAN',
                        Threshold: 100,
                        ThresholdType: 'PERCENTAGE',
                    },
                }),
            ]),
        });
        template.hasOutput('MonthlyDevBudgetName', {
            Value: Match.anyValue(),
        });
    });

    it('keeps the network low-cost and private-by-default', () => {
        const template = Template.fromStack(synthNetwork());

        template.resourceCountIs('AWS::EC2::VPC', 1);
        template.resourceCountIs('AWS::EC2::NatGateway', 1);
        template.resourcePropertiesCountIs('AWS::EC2::VPCEndpoint', {
            VpcEndpointType: 'Gateway',
        }, 1);
        template.resourcePropertiesCountIs('AWS::EC2::VPCEndpoint', {
            VpcEndpointType: 'Interface',
        }, 0);

        const publicIngressRules = ingressRulesFrom(template).filter((rule) => (
            rule.CidrIp === '0.0.0.0/0' || rule.CidrIpv6 === '::/0'
        ));
        expect(publicIngressRules).toHaveLength(2);
        expect(publicIngressRules.every((rule) => rule.IpProtocol === 'tcp' && rule.FromPort === 443 && rule.ToPort === 443)).toBe(true);
    });

    it('keeps stateful data resources cost-capped and non-public', () => {
        const template = Template.fromStack(synthData());

        template.resourceCountIs('AWS::RDS::DBCluster', 1);
        template.resourceCountIs('AWS::RDS::DBInstance', 1);
        template.resourceCountIs('AWS::ElastiCache::ServerlessCache', 1);
        template.resourceCountIs('AWS::MSK::Cluster', 0);
        template.hasResourceProperties('AWS::RDS::DBCluster', {
            Engine: 'aurora-postgresql',
            EngineVersion: '16.13',
            ServerlessV2ScalingConfiguration: {
                MinCapacity: 0,
                MaxCapacity: 2,
                SecondsUntilAutoPause: 600,
            },
        });
        template.hasResourceProperties('AWS::ElastiCache::ServerlessCache', {
            Engine: 'valkey',
            CacheUsageLimits: {
                DataStorage: {
                    Maximum: 1,
                    Unit: 'GB',
                },
                ECPUPerSecond: {
                    Maximum: 1000,
                },
            },
        });
        template.resourcePropertiesCountIs('AWS::EC2::Instance', {
            InstanceType: 't4g.small',
        }, 2);
        template.hasResourceProperties('AWS::S3::Bucket', {
            PublicAccessBlockConfiguration: {
                BlockPublicAcls: true,
                BlockPublicPolicy: true,
                IgnorePublicAcls: true,
                RestrictPublicBuckets: true,
            },
            VersioningConfiguration: {
                Status: 'Enabled',
            },
        });
        expect(ssmParameterNamesFrom(template)).toEqual(expect.arrayContaining([
            '/loop-ad/dev/aurora/endpoint',
            '/loop-ad/dev/redis/endpoint',
            '/loop-ad/dev/clickhouse/endpoint',
            '/loop-ad/dev/kafka/bootstrap-brokers',
            '/loop-ad/dev/data-storage/bucket-name',
        ]));
    });

    it('keeps stateful logical IDs stable across refactors', () => {
        const networkResources = Template.fromStack(synthNetwork()).toJSON().Resources as Record<string, { Type: string }>;
        const dataResources = Template.fromStack(synthData()).toJSON().Resources as Record<string, { Type: string }>;

        expect(networkResources).toHaveProperty('Vpc8378EB38');
        expect(dataResources).toHaveProperty('DataStorageBucket1A195487');
        expect(dataResources).toHaveProperty('AuroraPostgresClusterFE4B644F');
        expect(dataResources).toHaveProperty('AuroraPostgresClusterwriterE7962133');
        expect(dataResources).toHaveProperty('ValkeyServerlessCache');
        expect(dataResources).toHaveProperty('ClickHouseInstance6520CF63');
        expect(dataResources).toHaveProperty('KafkaInstance5AAC3452');
    });

    it('keeps runtime ingress, service, logging, and secret contracts explicit', () => {
        const template = Template.fromStack(synthRuntime());

        template.resourceCountIs('AWS::ECS::Service', 5);
        template.resourceCountIs('AWS::Logs::LogGroup', 5);
        template.resourcePropertiesCountIs('AWS::ElasticLoadBalancingV2::Listener', {
            Port: 80,
        }, 0);
        template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
            Port: 443,
            Protocol: 'HTTPS',
        });
        template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
            Port: 443,
            Protocol: 'TLS',
        });
        for (const serviceId of ['event-collector', 'ad-context-projector', 'advertisement-api', 'dashboard-api', 'decision-api']) {
            template.hasResourceProperties('AWS::Logs::LogGroup', {
                LogGroupName: `/loop-ad/dev/ecs/${serviceId}`,
                RetentionInDays: 90,
            });
            template.hasResourceProperties('AWS::ECS::Service', {
                ServiceName: `dev-${serviceId}`,
                LaunchType: 'FARGATE',
            });
        }
        template.resourcePropertiesCountIs('AWS::ApplicationAutoScaling::ScalableTarget', {
            MinCapacity: 1,
            MaxCapacity: 2,
        }, 5);
        expect(JSON.stringify(template.toJSON())).toContain('LOOPAD_OPENAI_API_KEY');
    });

    it('keeps repositories and certificates in lifecycle-specific stacks', () => {
        const repositoryTemplate = Template.fromStack(synthRepositories());
        const certificateTemplate = Template.fromStack(synthCertificate());

        repositoryTemplate.resourceCountIs('AWS::ECR::Repository', 5);
        repositoryTemplate.hasResourceProperties('AWS::ECR::Repository', {
            RepositoryName: 'loop-ad/event-collector',
            ImageScanningConfiguration: {
                ScanOnPush: true,
            },
        });
        certificateTemplate.resourceCountIs('AWS::CertificateManager::Certificate', 2);
        certificateTemplate.hasResourceProperties('AWS::CertificateManager::Certificate', {
            DomainName: `dashboard.dev.${testPublicHostedZone.domainName}`,
            SubjectAlternativeNames: [`demo-shoppingmall.dev.${testPublicHostedZone.domainName}`],
        });
    });
});

describe('loop-ad local safety contracts', () => {
    it('uses explicit synth/deploy lifecycle scripts and blocks generic deploy commands', () => {
        const packageJson = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')) as {
            scripts: Record<string, string>;
        };
        const refusal = readFileSync(join(ROOT, 'scripts/refuse-deploy.mjs'), 'utf8');

        expect(packageJson.scripts.deploy).toBe('node scripts/refuse-deploy.mjs');
        expect(packageJson.scripts.destroy).toBe('node scripts/refuse-deploy.mjs');
        expect(packageJson.scripts['synth:dev-cost-guardrails']).toContain('LoopAdDevCostGuardrailStack');
        expect(packageJson.scripts['cost:dev']).toBe('node scripts/estimate-dev-monthly-cost.mjs');
        expect(refusal).toContain('Generic deploy/destroy is intentionally blocked');
        expect(refusal).toContain('deploy:dev-cost-guardrails');
    });

    it('requires app context and env values without fallback defaults', () => {
        const cdkApp = readFileSync(join(ROOT, 'bin/loop-ad_aws_cdk.ts'), 'utf8');

        expect(cdkApp).toContain('dev-cost-guardrails');
        expect(cdkApp).toContain("readRequiredEnv('LOOP_AD_BUDGET_ALERT_EMAIL')");
        expect(cdkApp).toContain("readRequiredEnv('CDK_DEFAULT_ACCOUNT')");
        expect(cdkApp).toContain("readRequiredEnv('LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN')");
        expect(cdkApp).not.toContain("?? 'dev'");
    });

    it('keeps L1 constructs limited to documented exceptions', () => {
        const allowed = new Set([
            'budgets.CfnBudget',
            'cdk.CfnOutput',
            'elasticache.CfnServerlessCache',
        ]);
        const violations = sourceFiles(SRC_DIR).flatMap((file) => {
            const source = readFileSync(file, 'utf8');
            const matches = [...source.matchAll(/new\s+([a-zA-Z0-9_]+\.Cfn[A-Za-z0-9_]+)/g)];

            return matches.flatMap((match) => {
                const constructName = match[1];
                return constructName && !allowed.has(constructName) ? [`${file}: ${constructName}`] : [];
            });
        });

        expect(violations).toEqual([]);
    });

    it('keeps CDK source modules split into reviewable files', () => {
        const oversizedFiles = sourceFiles(SRC_DIR).flatMap((file) => {
            const lineCount = readFileSync(file, 'utf8').split('\n').length;
            return lineCount > 950 ? [`${file}: ${lineCount}`] : [];
        });

        expect(oversizedFiles).toEqual([]);
    });

    it('keeps reusable GitHub workflows OIDC-based and infra checks deploy-free', () => {
        const ecsWorkflow = readFileSync(join(ROOT, '.github/workflows/ecs-deploy.yml'), 'utf8');
        const frontendWorkflow = readFileSync(join(ROOT, '.github/workflows/frontend-deploy.yml'), 'utf8');
        const infraWorkflow = readFileSync(join(ROOT, '.github/workflows/infra-check.yml'), 'utf8');

        expect(ecsWorkflow).toContain('id-token: write');
        expect(frontendWorkflow).toContain('id-token: write');
        expect(infraWorkflow).toContain('npm run build');
        expect(infraWorkflow).toContain('npm test');
        expect(infraWorkflow).toContain('npm run synth:${{ inputs.environment }}');
        expect(infraWorkflow).not.toContain('cdk deploy');
        expect(infraWorkflow).not.toContain('cdk diff');
    });

    it('calculates the dev cost model deterministically under the budget limit', () => {
        const result = spawnSync(process.execPath, [join(ROOT, 'scripts/estimate-dev-monthly-cost.mjs'), '--json'], {
            encoding: 'utf8',
        });

        expect(result.status).toBe(0);
        const model = JSON.parse(result.stdout) as {
            budgetLimitUsd: number;
            totalMonthlyUsd: number;
            lineItems: Array<{ id: string; monthlyUsd: number }>;
        };
        expect(model.totalMonthlyUsd).toBeLessThanOrEqual(model.budgetLimitUsd);
        expect(model.lineItems.map((item) => item.id)).toEqual(expect.arrayContaining([
            'nat-gateway-hourly',
            'fargate-arm64-vcpu',
            'aurora-serverless-v2-average-acu',
            'valkey-serverless',
            'clickhouse-ec2',
            'kafka-ec2',
        ]));
        expect(model.lineItems.every((item) => Number.isFinite(item.monthlyUsd) && item.monthlyUsd >= 0)).toBe(true);
    });

    it('documents managed transition gates without changing app contracts', () => {
        const plan = readFileSync(join(ROOT, 'docs/managed-service-transition-plan.md'), 'utf8');

        for (const requiredText of [
            'Performance test',
            'Monthly $1200 verification',
            'Rollback',
            'Migration risk',
            'CDK scope',
            '/loop-ad/dev/kafka/bootstrap-brokers',
            '/loop-ad/dev/clickhouse/endpoint',
            '/loop-ad/dev/redis/endpoint',
            '/loop-ad/dev/aurora/endpoint',
            'LOOPAD_KAFKA_BOOTSTRAP_BROKERS',
            'LOOPAD_CLICKHOUSE_URL',
            'LOOPAD_REDIS_URL',
            'LOOPAD_AURORA_HOST',
            'serverSecurityGroup',
            'dataStorageSecurityGroup',
            'LoopAdDevDataStack',
        ]) {
            expect(plan).toContain(requiredText);
        }
    });
});

function synthCostGuardrail(): LoopAdDevCostGuardrailStack {
    const app = new cdk.App();
    return new LoopAdDevCostGuardrailStack(app, 'LoopAdDevCostGuardrailStack', {
        env: {
            account: testEnv.account,
            region: 'us-east-1',
        },
        budgetAlertEmail: 'alerts@example.test',
    });
}

function synthNetwork(): LoopAdDevNetworkStack {
    const app = new cdk.App();
    return new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env: testEnv,
    });
}

function synthData(): LoopAdDevDataStack {
    const app = new cdk.App();
    const network = new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env: testEnv,
    });
    return new LoopAdDevDataStack(app, 'LoopAdDevDataStack', {
        env: testEnv,
        publicHostedZone: testPublicHostedZone,
        network,
        genAiGeneratedAssetsCertificateArn: testCertificateArns.genAiGeneratedAssetsCertificateArn,
    });
}

function synthRuntime(): LoopAdDevRuntimeStack {
    const app = new cdk.App();
    const network = new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env: testEnv,
    });
    const data = new LoopAdDevDataStack(app, 'LoopAdDevDataStack', {
        env: testEnv,
        publicHostedZone: testPublicHostedZone,
        network,
        genAiGeneratedAssetsCertificateArn: testCertificateArns.genAiGeneratedAssetsCertificateArn,
    });
    return new LoopAdDevRuntimeStack(app, 'LoopAdDevRuntimeStack', {
        env: testEnv,
        publicHostedZone: testPublicHostedZone,
        certificateArns: testCertificateArns,
        network,
        data,
    });
}

function synthRepositories(): LoopAdDevRepositoryStack {
    const app = new cdk.App();
    return new LoopAdDevRepositoryStack(app, 'LoopAdDevRepositoryStack', {
        env: testEnv,
    });
}

function synthCertificate(): LoopAdDevCertificateStack {
    const app = new cdk.App();
    return new LoopAdDevCertificateStack(app, 'LoopAdDevCertificateStack', {
        env: {
            account: testEnv.account,
            region: 'us-east-1',
        },
        publicHostedZone: testPublicHostedZone,
    });
}

function ingressRulesFrom(template: Template): Record<string, unknown>[] {
    const resources = template.toJSON().Resources as Record<string, { Type: string; Properties?: Record<string, unknown> }>;
    return Object.values(resources).flatMap((resource) => {
        if (resource.Type === 'AWS::EC2::SecurityGroupIngress') {
            return [resource.Properties ?? {}];
        }

        if (resource.Type !== 'AWS::EC2::SecurityGroup') {
            return [];
        }

        return (resource.Properties?.SecurityGroupIngress as Record<string, unknown>[] | undefined) ?? [];
    });
}

function ssmParameterNamesFrom(template: Template): string[] {
    const resources = template.toJSON().Resources as Record<string, { Type: string; Properties?: Record<string, unknown> }>;
    return Object.values(resources).flatMap((resource) => (
        resource.Type === 'AWS::SSM::Parameter' ? [String(resource.Properties?.Name ?? '')] : []
    ));
}

function sourceFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((entry) => {
        const path = join(dir, entry);
        if (statSync(path).isDirectory()) {
            return sourceFiles(path);
        }

        return path.endsWith('.ts') ? [path] : [];
    });
}

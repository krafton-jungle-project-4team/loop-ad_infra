import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import {
    LOOP_AD_REGION,
    LoopAdDevCertificateStack,
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
        const dataResources = template.toJSON().Resources as Record<string, { Type: string; Properties?: Record<string, unknown> }>;
        const kafkaRole = ec2RoleWithNameTag(dataResources, 'dev-loop-ad-kafka');
        const clickHouseRole = ec2RoleWithNameTag(dataResources, 'dev-loop-ad-clickhouse');
        expect(JSON.stringify(kafkaRole?.Properties?.ManagedPolicyArns ?? [])).toContain('AmazonSSMManagedInstanceCore');
        expect(JSON.stringify(clickHouseRole?.Properties?.ManagedPolicyArns ?? [])).not.toContain('AmazonSSMManagedInstanceCore');
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

    it('keeps runtime ECS logical IDs stable across helper refactors', () => {
        const resources = Template.fromStack(synthRuntime()).toJSON().Resources as Record<string, { Type: string }>;

        for (const logicalId of [
            'EventCollectorTaskDefinitionD7E6990A',
            'EventCollectorLogGroup84568A76',
            'EventCollectorService1F8A822E',
            'EventCollectorServiceTaskCountTarget3C89D8FF',
            'AdvertisementApiTaskDefinition3BE1FB97',
            'AdvertisementApiLogGroup0D4EBE76',
            'AdvertisementApiServiceE83FF4CB',
            'AdvertisementApiServiceTaskCountTargetC183CF8C',
            'DashboardApiTaskDefinitionD8626F22',
            'DashboardApiLogGroup8A824421',
            'DashboardApiServiceF9B98A69',
            'DashboardApiServiceTaskCountTargetF843A612',
            'DecisionApiTaskDefinition645801B5',
            'DecisionApiLogGroupEE2EF543',
            'DecisionApiService8390708F',
            'DecisionApiServiceTaskCountTargetEB12B73D',
        ]) {
            expect(resources).toHaveProperty(logicalId);
        }
    });

    it('keeps runtime ingress, service, logging, and secret contracts explicit', () => {
        const template = Template.fromStack(synthRuntime());

        template.resourceCountIs('AWS::ECS::Service', 4);
        template.resourceCountIs('AWS::Logs::LogGroup', 4);
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
        for (const serviceId of ['event-collector', 'advertisement-api', 'dashboard-api', 'decision-api']) {
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
        }, 4);
        expect(JSON.stringify(template.toJSON())).toContain('LOOPAD_OPENAI_API_KEY');
    });

    it('keeps repositories and certificates in lifecycle-specific stacks', () => {
        const repositoryTemplate = Template.fromStack(synthRepositories());
        const certificateTemplate = Template.fromStack(synthCertificate());

        repositoryTemplate.resourceCountIs('AWS::ECR::Repository', 4);
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
        expect(packageJson.scripts['cost:dev']).toBe('node scripts/estimate-dev-monthly-cost.mjs');
        expect(refusal).toContain('Generic deploy/destroy is intentionally blocked');
        expect(Object.keys(packageJson.scripts)).not.toContain('deploy:dev-cost-guardrails');
        expect(Object.keys(packageJson.scripts)).not.toContain('synth:dev-cost-guardrails');
    });

    it('requires app context and env values without fallback defaults', () => {
        const cdkApp = readFileSync(join(ROOT, 'bin/loop-ad_aws_cdk.ts'), 'utf8');

        expect(cdkApp).toContain("readRequiredEnv('CDK_DEFAULT_ACCOUNT')");
        expect(cdkApp).toContain("readRequiredEnv('LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN')");
        expect(cdkApp).not.toContain('dev-cost-guardrails');
        expect(cdkApp).not.toContain('LOOP_AD_BUDGET_ALERT_EMAIL');
        expect(cdkApp).not.toContain("?? 'dev'");
    });

    it('keeps L1 constructs limited to documented exceptions', () => {
        const allowed = new Set([
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

    it('does not create CDK-owned budget alert resources', () => {
        const synthesizedSources = sourceFiles(SRC_DIR)
            .map((file) => readFileSync(file, 'utf8'))
            .join('\n');

        expect(synthesizedSources).not.toContain('CfnBudget');
    });
});

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

function ec2RoleWithNameTag(
    resources: Record<string, { Type: string; Properties?: Record<string, unknown> }>,
    name: string,
): { Type: string; Properties?: Record<string, unknown> } | undefined {
    return Object.values(resources).find((resource) => (
        resource.Type === 'AWS::IAM::Role' &&
        JSON.stringify(resource.Properties?.Tags ?? []).includes(name)
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

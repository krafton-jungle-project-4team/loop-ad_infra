import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { LOOP_AD_MONTHLY_COST_TARGET_USD, LOOP_AD_REGION, LoopAdDevStack, LoopAdPerfStack } from '../src/loop-ad-stack';

const testEnv = {
    account: '123456789012',
    region: LOOP_AD_REGION,
};
const testPublicHostedZone = {
    hostedZoneId: 'ZTESTHOSTEDZONEID',
    domainName: 'example.test',
};

describe('loop-ad CDK stacks', () => {
    it('dev stack keeps the permanent VPC, ECR repositories, and five ECS services', () => {
        const stack = synthDev();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::EC2::VPC', 1);
        template.resourceCountIs('AWS::EC2::NatGateway', 1);
        template.resourcePropertiesCountIs('AWS::EC2::VPCEndpoint', {
            VpcEndpointType: 'Interface',
        }, 0);
        template.resourcePropertiesCountIs('AWS::EC2::VPCEndpoint', {
            VpcEndpointType: 'Gateway',
        }, 1);
        template.resourceCountIs('AWS::Budgets::Budget', 1);
        template.resourceCountIs('AWS::ECR::Repository', 5);
        template.resourceCountIs('AWS::ECS::Service', 5);
        template.hasResourceProperties('AWS::Budgets::Budget', {
            Budget: {
                BudgetLimit: {
                    Amount: LOOP_AD_MONTHLY_COST_TARGET_USD,
                    Unit: 'USD',
                },
                BudgetType: 'COST',
                TimeUnit: 'MONTHLY',
            },
        });
        template.hasResourceProperties('AWS::ECR::Repository', {
            RepositoryName: 'loop-ad/event-collector',
        });
        template.hasResourceProperties('AWS::ECR::Repository', {
            RepositoryName: 'loop-ad/dashboard-api',
        });
        template.hasResourceProperties('AWS::ECS::Service', {
            ServiceName: 'dev-event-collector',
            LaunchType: 'FARGATE',
        });
        template.hasResourceProperties('AWS::ECS::Service', {
            ServiceName: 'dev-recommendation',
            LaunchType: 'FARGATE',
        });
        template.resourcePropertiesCountIs('AWS::ApplicationAutoScaling::ScalableTarget', {
            MinCapacity: 1,
            MaxCapacity: 2,
        }, 5);
    });

    it('dev stack exposes only collector through NLB and API services through ALB path rules', () => {
        const stack = synthDev();
        const template = Template.fromStack(stack);

        template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
            Port: 80,
            Protocol: 'TCP',
        });
        template.resourceCountIs('AWS::ElasticLoadBalancingV2::ListenerRule', 2);
        template.hasResourceProperties('AWS::ElasticLoadBalancingV2::ListenerRule', {
            Priority: 20,
            Conditions: Match.arrayWith([
                Match.objectLike({
                    Field: 'path-pattern',
                    PathPatternConfig: {
                        Values: ['/api/ads/*', '/decision/*'],
                    },
                }),
            ]),
        });
        template.hasResourceProperties('AWS::ElasticLoadBalancingV2::ListenerRule', {
            Priority: 30,
            Conditions: Match.arrayWith([
                Match.objectLike({
                    Field: 'path-pattern',
                    PathPatternConfig: {
                        Values: ['/api/dashboard/*', '/dashboard/*'],
                    },
                }),
            ]),
        });
    });

    it('dev stack creates Route53 aliases for public API and ingest subdomains', () => {
        const stack = synthDev();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::Route53::RecordSet', 2);
        template.hasResourceProperties('AWS::Route53::RecordSet', {
            Type: 'A',
            HostedZoneId: testPublicHostedZone.hostedZoneId,
            AliasTarget: Match.objectLike({
                DNSName: Match.anyValue(),
                HostedZoneId: Match.anyValue(),
            }),
        });
        expect(route53RecordNamesFrom(template)).toEqual(expect.arrayContaining([
            `api.dev.${testPublicHostedZone.domainName}.`,
            `ingest.dev.${testPublicHostedZone.domainName}.`,
        ]));
    });

    it('dev stack creates the cost-capped datasource shape', () => {
        const stack = synthDev();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::RDS::DBCluster', 1);
        template.resourceCountIs('AWS::RDS::DBInstance', 1);
        template.resourceCountIs('AWS::MSK::Cluster', 1);
        template.resourceCountIs('Custom::LoopAdMskBootstrapBrokers', 1);
        template.hasResourceProperties('AWS::RDS::DBCluster', {
            DBClusterIdentifier: 'dev-loop-ad-aurora-postgres',
            DatabaseName: 'loopad',
            Engine: 'aurora-postgresql',
            EngineVersion: '16.13',
            ServerlessV2ScalingConfiguration: {
                MinCapacity: 0,
                MaxCapacity: 2,
                SecondsUntilAutoPause: 600,
            },
        });
        template.hasResourceProperties('AWS::RDS::DBInstance', {
            DBInstanceClass: 'db.serverless',
            Engine: 'aurora-postgresql',
            PubliclyAccessible: false,
        });
        template.hasResourceProperties('AWS::EC2::Instance', {
            InstanceType: 't4g.small',
            BlockDeviceMappings: Match.arrayWith([
                Match.objectLike({
                    DeviceName: '/dev/xvda',
                    Ebs: {
                        Encrypted: true,
                        VolumeSize: 50,
                        VolumeType: 'gp3',
                    },
                }),
            ]),
            Tags: Match.arrayWith([
                Match.objectLike({
                    Key: 'Name',
                    Value: 'dev-loop-ad-clickhouse',
                }),
            ]),
        });
        template.hasResourceProperties('AWS::MSK::Cluster', {
            ClusterName: 'dev-loop-ad-msk',
            KafkaVersion: '3.6.0',
            NumberOfBrokerNodes: 2,
            BrokerNodeGroupInfo: Match.objectLike({
                InstanceType: 'kafka.t3.small',
                StorageInfo: {
                    EBSStorageInfo: {
                        VolumeSize: 20,
                    },
                },
            }),
        });
        template.hasResourceProperties('Custom::LoopAdMskBootstrapBrokers', {
            InstallLatestAwsSdk: false,
        });

        expect(JSON.stringify(ssmParameterValueFrom(template, '/loop-ad/dev/aurora/endpoint'))).toContain('Endpoint.Address');
        expect(ssmParameterValueFrom(template, '/loop-ad/dev/redis/endpoint')).toBe('pending://dev/redis');
        expect(JSON.stringify(ssmParameterValueFrom(template, '/loop-ad/dev/clickhouse/endpoint'))).toContain('PrivateDnsName');
        expect(JSON.stringify(ssmParameterValueFrom(template, '/loop-ad/dev/msk/bootstrap-brokers'))).toContain('BootstrapBrokerString');
    });

    it('perf stack imports the dev VPC and creates only the temporary collection path', () => {
        const stack = synthPerf();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::EC2::VPC', 0);
        template.resourceCountIs('AWS::EC2::NatGateway', 0);
        template.resourceCountIs('AWS::Budgets::Budget', 0);
        template.resourceCountIs('AWS::ECR::Repository', 0);
        template.resourceCountIs('AWS::AutoScaling::AutoScalingGroup', 1);
        template.resourceCountIs('AWS::ECS::Service', 2);
        template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 1);
        template.resourceCountIs('AWS::SSM::Parameter', 3);
        template.hasResourceProperties('AWS::ECS::Service', {
            ServiceName: 'perf-event-collector',
            CapacityProviderStrategy: Match.arrayWith([
                Match.objectLike({
                    Weight: 1,
                }),
            ]),
        });
        template.hasResourceProperties('AWS::ECS::Service', {
            ServiceName: 'perf-ad-context-projector',
            CapacityProviderStrategy: Match.arrayWith([
                Match.objectLike({
                    Weight: 1,
                }),
            ]),
        });
        template.hasResourceProperties('AWS::AutoScaling::AutoScalingGroup', {
            MinSize: '0',
            MaxSize: '2',
        });
        template.resourcePropertiesCountIs('AWS::ApplicationAutoScaling::ScalableTarget', {
            MinCapacity: 0,
            MaxCapacity: 2,
        }, 2);
        template.hasResourceProperties('AWS::SSM::Parameter', {
            Name: '/loop-ad/perf/msk/bootstrap-brokers',
            Value: 'pending://perf/msk',
        });
        template.hasResourceProperties('AWS::SSM::Parameter', {
            Name: '/loop-ad/perf/redis/endpoint',
            Value: 'pending://perf/redis',
        });
        template.hasResourceProperties('AWS::SSM::Parameter', {
            Name: '/loop-ad/perf/clickhouse/endpoint',
            Value: 'pending://perf/clickhouse',
        });
        template.resourcePropertiesCountIs('AWS::SSM::Parameter', {
            Name: '/loop-ad/perf/aurora/endpoint',
        }, 0);
        expect(importValuesFrom(template)).toEqual(expect.arrayContaining([
            'loop-ad-dev-public-subnet-ids',
        ]));
    });

    it('perf stack creates only the temporary Route53 ingest alias', () => {
        const stack = synthPerf();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::Route53::RecordSet', 1);
        expect(route53RecordNamesFrom(template)).toEqual([
            `ingest.perf.${testPublicHostedZone.domainName}.`,
        ]);
    });

    it('dev stack exports the shared VPC values consumed by perf', () => {
        const stack = synthDev();
        const template = Template.fromStack(stack);

        template.hasOutput('VpcId', {
            Export: {
                Name: 'loop-ad-dev-vpc-id',
            },
        });
        template.hasOutput('PrivateSubnetIds', {
            Export: {
                Name: 'loop-ad-dev-private-subnet-ids',
            },
        });
        template.hasOutput('PublicSubnetIds', {
            Export: {
                Name: 'loop-ad-dev-public-subnet-ids',
            },
        });
    });
});

function route53RecordNamesFrom(template: Template): string[] {
    const resources = template.toJSON().Resources as Record<string, { Type: string; Properties?: Record<string, unknown> }>;
    return Object.values(resources).flatMap((resource) => {
        if (resource.Type !== 'AWS::Route53::RecordSet') {
            return [];
        }

        return [String(resource.Properties?.Name ?? '')];
    });
}

function importValuesFrom(template: Template): string[] {
    const values = new Set<string>();

    function visit(value: unknown): void {
        if (Array.isArray(value)) {
            for (const item of value) {
                visit(item);
            }
            return;
        }

        if (!value || typeof value !== 'object') {
            return;
        }

        const object = value as Record<string, unknown>;
        const importValue = object['Fn::ImportValue'];
        if (typeof importValue === 'string') {
            values.add(importValue);
        }

        for (const child of Object.values(object)) {
            visit(child);
        }
    }

    visit(template.toJSON());

    return [...values];
}

function ssmParameterValueFrom(template: Template, parameterName: string): unknown {
    const resources = template.toJSON().Resources as Record<string, { Type: string; Properties?: Record<string, unknown> }>;
    const parameter = Object.values(resources).find((resource) => (
        resource.Type === 'AWS::SSM::Parameter'
        && resource.Properties?.Name === parameterName
    ));

    return parameter?.Properties?.Value;
}

function synthDev(): LoopAdDevStack {
    const app = new cdk.App();
    return new LoopAdDevStack(app, 'LoopAdDevStack', {
        env: testEnv,
        publicHostedZone: testPublicHostedZone,
    });
}

function synthPerf(): LoopAdPerfStack {
    const app = new cdk.App();
    return new LoopAdPerfStack(app, 'LoopAdPerfStack', {
        env: testEnv,
        publicHostedZone: testPublicHostedZone,
    });
}

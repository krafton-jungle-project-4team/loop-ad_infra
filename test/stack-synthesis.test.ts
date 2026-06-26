import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import {
    LOOP_AD_MONTHLY_COST_TARGET_USD,
    LOOP_AD_REGION,
    LoopAdDevStack,
} from '../src/loop-ad-stack';

const testEnv = {
    account: '123456789012',
    region: LOOP_AD_REGION,
};
const testPublicHostedZone = {
    hostedZoneId: 'ZTESTHOSTEDZONEID',
    domainName: 'example.test',
};

describe('loop-ad CDK stacks', () => {
    it('dev stack keeps the permanent VPC, DataStorage bucket, ECR repositories, and five ECS services', () => {
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
        template.resourceCountIs('AWS::S3::Bucket', 1);
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
        template.hasResourceProperties('AWS::S3::Bucket', {
            PublicAccessBlockConfiguration: {
                BlockPublicAcls: true,
                BlockPublicPolicy: true,
                IgnorePublicAcls: true,
                RestrictPublicBuckets: true,
            },
            BucketEncryption: {
                ServerSideEncryptionConfiguration: Match.arrayWith([
                    Match.objectLike({
                        ServerSideEncryptionByDefault: {
                            SSEAlgorithm: 'AES256',
                        },
                    }),
                ]),
            },
            OwnershipControls: {
                Rules: Match.arrayWith([
                    Match.objectLike({
                        ObjectOwnership: 'BucketOwnerEnforced',
                    }),
                ]),
            },
            VersioningConfiguration: {
                Status: 'Enabled',
            },
            LifecycleConfiguration: {
                Rules: Match.arrayWith([
                    Match.objectLike({
                        Id: 'AbortIncompleteGenAiGeneratedUploads',
                        Prefix: 'genai/generated/',
                        Status: 'Enabled',
                        AbortIncompleteMultipartUpload: {
                            DaysAfterInitiation: 7,
                        },
                    }),
                ]),
            },
        });
        template.hasResourceProperties('AWS::S3::BucketPolicy', {
            PolicyDocument: {
                Statement: Match.arrayWith([
                    Match.objectLike({
                        Effect: 'Deny',
                        Action: 's3:*',
                        Condition: {
                            Bool: {
                                'aws:SecureTransport': 'false',
                            },
                        },
                    }),
                ]),
            },
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

    it('dev stack creates the cost-capped data storage shape', () => {
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
        expect(JSON.stringify(ssmParameterValueFrom(template, '/loop-ad/dev/data-storage/bucket-name'))).toContain('DataStorageBucket');
        expect(ssmParameterValueFrom(template, '/loop-ad/dev/data-storage/genai-generated-prefix')).toBe('genai/generated/');
        template.hasResourceProperties('AWS::ECS::TaskDefinition', {
            ContainerDefinitions: Match.arrayWith([
                Match.objectLike({
                    Name: 'dashboard-api',
                    Environment: Match.arrayWith([
                        Match.objectLike({
                            Name: 'LOOPAD_DATA_STORAGE_BUCKET',
                            Value: Match.anyValue(),
                        }),
                        Match.objectLike({
                            Name: 'LOOPAD_GENAI_GENERATED_ASSETS_PREFIX',
                            Value: 'genai/generated/',
                        }),
                    ]),
                }),
            ]),
        });
        template.hasResourceProperties('AWS::ECS::TaskDefinition', {
            ContainerDefinitions: Match.arrayWith([
                Match.objectLike({
                    Name: 'recommendation',
                    Environment: Match.arrayWith([
                        Match.objectLike({
                            Name: 'LOOPAD_DATA_STORAGE_BUCKET',
                            Value: Match.anyValue(),
                        }),
                        Match.objectLike({
                            Name: 'LOOPAD_GENAI_GENERATED_ASSETS_PREFIX',
                            Value: 'genai/generated/',
                        }),
                    ]),
                }),
            ]),
        });
        expect(JSON.stringify(template.toJSON())).toContain('genai/generated/*');
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

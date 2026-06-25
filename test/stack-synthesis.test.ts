import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import {
    LOOP_AD_MONTHLY_COST_TARGET_USD,
    LOOP_AD_AGGREGATION_PERF_TARGET_RPS,
    LOOP_AD_REGION,
    LoopAdDevStack,
    LoopAdAggregationPerfStack,
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
            BucketEncryption: {
                ServerSideEncryptionConfiguration: [
                    {
                        ServerSideEncryptionByDefault: {
                            SSEAlgorithm: 'AES256',
                        },
                    },
                ],
            },
            PublicAccessBlockConfiguration: {
                BlockPublicAcls: true,
                BlockPublicPolicy: true,
                IgnorePublicAcls: true,
                RestrictPublicBuckets: true,
            },
            VersioningConfiguration: {
                Status: 'Enabled',
            },
            LifecycleConfiguration: {
                Rules: Match.arrayWith([
                    Match.objectLike({
                        Prefix: 'aggregation-perf-runs/',
                        Status: 'Enabled',
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

    it('aggregation perf stack imports the dev VPC and creates the 20k RPS aggregation path', () => {
        const stack = synthAggregationPerf();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::EC2::VPC', 0);
        template.resourceCountIs('AWS::EC2::NatGateway', 0);
        template.resourceCountIs('AWS::Budgets::Budget', 0);
        template.resourceCountIs('AWS::ECR::Repository', 0);
        template.resourceCountIs('AWS::AutoScaling::AutoScalingGroup', 1);
        template.resourceCountIs('AWS::AutoScaling::LaunchConfiguration', 1);
        template.resourceCountIs('AWS::EC2::Instance', 1);
        template.resourceCountIs('AWS::ECS::Service', 2);
        template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 1);
        template.resourceCountIs('AWS::MSK::Cluster', 1);
        template.resourceCountIs('AWS::MSK::Configuration', 1);
        template.resourceCountIs('AWS::MSK::Topic', 1);
        template.resourceCountIs('Custom::LoopAdAggregationPerfMskBootstrapBrokers', 1);
        template.resourceCountIs('AWS::SSM::Parameter', 4);
        template.hasResourceProperties('AWS::ECS::Service', {
            ServiceName: 'aggregation-perf-event-collector',
            DesiredCount: 24,
            CapacityProviderStrategy: Match.arrayWith([
                Match.objectLike({
                    Weight: 1,
                }),
            ]),
            PlacementStrategies: Match.arrayWith([
                Match.objectLike({
                    Field: 'attribute:ecs.availability-zone',
                    Type: 'spread',
                }),
                Match.objectLike({
                    Field: 'instanceId',
                    Type: 'spread',
                }),
            ]),
        });
        template.hasResourceProperties('AWS::ECS::Service', {
            ServiceName: 'aggregation-perf-ad-context-projector',
            DesiredCount: 12,
            CapacityProviderStrategy: Match.arrayWith([
                Match.objectLike({
                    Weight: 1,
                }),
            ]),
            PlacementStrategies: Match.arrayWith([
                Match.objectLike({
                    Field: 'attribute:ecs.availability-zone',
                    Type: 'spread',
                }),
                Match.objectLike({
                    Field: 'instanceId',
                    Type: 'spread',
                }),
            ]),
        });
        template.hasResourceProperties('AWS::AutoScaling::AutoScalingGroup', {
            DesiredCapacity: '6',
            MinSize: '6',
            MaxSize: '12',
        });
        template.hasResourceProperties('AWS::AutoScaling::LaunchConfiguration', {
            InstanceType: 'c7g.xlarge',
            InstanceMonitoring: true,
            BlockDeviceMappings: Match.arrayWith([
                Match.objectLike({
                    DeviceName: '/dev/xvda',
                    Ebs: {
                        Encrypted: true,
                        VolumeSize: 100,
                        VolumeType: 'gp3',
                    },
                }),
            ]),
        });
        template.resourcePropertiesCountIs('AWS::ApplicationAutoScaling::ScalableTarget', {
            MinCapacity: 24,
            MaxCapacity: 48,
        }, 1);
        template.resourcePropertiesCountIs('AWS::ApplicationAutoScaling::ScalableTarget', {
            MinCapacity: 12,
            MaxCapacity: 24,
        }, 1);
        template.hasResourceProperties('AWS::EC2::Instance', {
            InstanceType: 'c7g.xlarge',
            BlockDeviceMappings: Match.arrayWith([
                Match.objectLike({
                    DeviceName: '/dev/xvda',
                    Ebs: {
                        Encrypted: true,
                        Iops: 3000,
                        VolumeSize: 500,
                        VolumeType: 'gp3',
                    },
                }),
            ]),
            Tags: Match.arrayWith([
                Match.objectLike({
                    Key: 'Name',
                    Value: 'aggregation-perf-loop-ad-clickhouse',
                }),
            ]),
        });
        template.hasResourceProperties('AWS::MSK::Configuration', {
            Name: 'aggregation-perf-loop-ad-msk-throughput',
            KafkaVersionsList: ['3.6.0'],
            ServerProperties: Match.stringLikeRegexp('num.partitions=128'),
        });
        template.hasResourceProperties('AWS::MSK::Cluster', {
            ClusterName: 'aggregation-perf-loop-ad-msk',
            KafkaVersion: '3.6.0',
            NumberOfBrokerNodes: 2,
            BrokerNodeGroupInfo: Match.objectLike({
                InstanceType: 'kafka.m7g.xlarge',
                StorageInfo: {
                    EBSStorageInfo: {
                        ProvisionedThroughput: {
                            Enabled: true,
                            VolumeThroughput: 250,
                        },
                        VolumeSize: 200,
                    },
                },
            }),
            EnhancedMonitoring: 'PER_BROKER',
        });
        template.hasResourceProperties('AWS::MSK::Topic', {
            TopicName: 'aggregation-events',
            PartitionCount: 128,
            ReplicationFactor: 2,
        });
        template.hasResourceProperties('Custom::LoopAdAggregationPerfMskBootstrapBrokers', {
            InstallLatestAwsSdk: false,
        });
        template.hasResourceProperties('AWS::ECS::TaskDefinition', {
            NetworkMode: 'bridge',
            RequiresCompatibilities: ['EC2'],
            ContainerDefinitions: Match.arrayWith([
                Match.objectLike({
                    Name: 'event-collector',
                    Cpu: 1024,
                    MemoryReservation: 2048,
                    PortMappings: Match.arrayWith([
                        {
                            ContainerPort: 80,
                            HostPort: 0,
                            Protocol: 'tcp',
                        },
                    ]),
                    Environment: Match.arrayWith([
                        {
                            Name: 'LOOPAD_AGGREGATION_PERF_TARGET_RPS',
                            Value: String(LOOP_AD_AGGREGATION_PERF_TARGET_RPS),
                        },
                        {
                            Name: 'LOOPAD_MSK_TOPIC',
                            Value: 'aggregation-events',
                        },
                        {
                            Name: 'LOOPAD_MSK_TOPIC_PARTITIONS',
                            Value: '128',
                        },
                        {
                            Name: 'LOOPAD_AGGREGATION_PERF_RESULTS_BUCKET_PARAMETER',
                            Value: Match.anyValue(),
                        },
                        {
                            Name: 'LOOPAD_AGGREGATION_PERF_RESULTS_S3_PREFIX',
                            Value: 'aggregation-perf-runs/',
                        },
                    ]),
                }),
            ]),
        });
        template.hasResourceProperties('AWS::ECS::TaskDefinition', {
            NetworkMode: 'bridge',
            RequiresCompatibilities: ['EC2'],
            ContainerDefinitions: Match.arrayWith([
                Match.objectLike({
                    Name: 'ad-context-projector',
                    Cpu: 1024,
                    MemoryReservation: 2048,
                    Environment: Match.arrayWith([
                        {
                            Name: 'LOOPAD_AGGREGATION_PERF_TARGET_RPS',
                            Value: String(LOOP_AD_AGGREGATION_PERF_TARGET_RPS),
                        },
                        {
                            Name: 'LOOPAD_CLICKHOUSE_ENDPOINT_PARAMETER',
                            Value: Match.anyValue(),
                        },
                        {
                            Name: 'LOOPAD_AGGREGATION_PERF_RESULTS_BUCKET_PARAMETER',
                            Value: Match.anyValue(),
                        },
                        {
                            Name: 'LOOPAD_AGGREGATION_PERF_RESULTS_S3_PREFIX',
                            Value: 'aggregation-perf-runs/',
                        },
                    ]),
                }),
            ]),
        });
        template.hasResourceProperties('AWS::SSM::Parameter', {
            Name: '/loop-ad/aggregation-perf/msk/bootstrap-brokers',
            Value: Match.objectLike({
                'Fn::GetAtt': Match.arrayWith(['BootstrapBrokerString']),
            }),
        });
        template.hasResourceProperties('AWS::SSM::Parameter', {
            Name: '/loop-ad/aggregation-perf/redis/endpoint',
            Value: 'pending://aggregation-perf/redis',
        });
        template.hasResourceProperties('AWS::SSM::Parameter', {
            Name: '/loop-ad/aggregation-perf/clickhouse/endpoint',
            Value: Match.objectLike({
                'Fn::Join': Match.anyValue(),
            }),
        });
        template.hasResourceProperties('AWS::SSM::Parameter', {
            Name: '/loop-ad/aggregation-perf/results-bucket-name',
            Value: {
                'Fn::ImportValue': 'loop-ad-aggregation-perf-results-bucket-name',
            },
        });
        template.hasResourceProperties('AWS::IAM::Policy', {
            PolicyDocument: {
                Statement: Match.arrayWith([
                    Match.objectLike({
                        Action: Match.arrayWith([
                            's3:AbortMultipartUpload',
                            's3:PutObject',
                            's3:PutObjectTagging',
                        ]),
                        Effect: 'Allow',
                        Resource: Match.objectLike({
                            'Fn::Join': Match.anyValue(),
                        }),
                    }),
                ]),
                Version: '2012-10-17',
            },
        });
        expect(JSON.stringify(ssmParameterValueFrom(template, '/loop-ad/aggregation-perf/clickhouse/endpoint'))).toContain('PrivateDnsName');
        template.resourcePropertiesCountIs('AWS::SSM::Parameter', {
            Name: '/loop-ad/aggregation-perf/aurora/endpoint',
        }, 0);
        expect(importValuesFrom(template)).toEqual(expect.arrayContaining([
            'loop-ad-dev-public-subnet-ids',
            'loop-ad-aggregation-perf-results-bucket-name',
        ]));
    });

    it('aggregation perf stack creates only the temporary Route53 ingest alias', () => {
        const stack = synthAggregationPerf();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::Route53::RecordSet', 1);
        expect(route53RecordNamesFrom(template)).toEqual([
            `ingest.aggregation-perf.${testPublicHostedZone.domainName}.`,
        ]);
    });

    it('dev stack exports the shared VPC values consumed by aggregation perf', () => {
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
        template.hasOutput('AggregationPerfResultsBucketName', {
            Export: {
                Name: 'loop-ad-aggregation-perf-results-bucket-name',
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

function synthAggregationPerf(): LoopAdAggregationPerfStack {
    const app = new cdk.App();
    return new LoopAdAggregationPerfStack(app, 'LoopAdAggregationPerfStack', {
        env: testEnv,
        publicHostedZone: testPublicHostedZone,
    });
}

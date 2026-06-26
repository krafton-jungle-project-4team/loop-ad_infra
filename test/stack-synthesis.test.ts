import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import {
    LOOP_AD_REGION,
    LoopAdDevCertificateStack,
    LoopAdDevNetworkStack,
    LoopAdDevRepositoryStack,
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
const testCertificateArns = {
    frontendSitesCertificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/frontend-sites',
    genAiGeneratedAssetsCertificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/gen-ai-assets',
};

describe('loop-ad CDK stacks', () => {
    it('dev certificate stack creates the CloudFront ACM certificates in us-east-1', () => {
        const stack = synthDevCertificate();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::CertificateManager::Certificate', 2);
        template.hasResourceProperties('AWS::CertificateManager::Certificate', {
            DomainName: `dashboard.dev.${testPublicHostedZone.domainName}`,
            SubjectAlternativeNames: [`demo-shoppingmall.dev.${testPublicHostedZone.domainName}`],
            ValidationMethod: 'DNS',
            DomainValidationOptions: Match.arrayWith([
                Match.objectLike({
                    DomainName: `dashboard.dev.${testPublicHostedZone.domainName}`,
                    HostedZoneId: testPublicHostedZone.hostedZoneId,
                }),
            ]),
        });
        template.hasResourceProperties('AWS::CertificateManager::Certificate', {
            DomainName: `gen-ai.asset.dev.${testPublicHostedZone.domainName}`,
            ValidationMethod: 'DNS',
            DomainValidationOptions: Match.arrayWith([
                Match.objectLike({
                    DomainName: `gen-ai.asset.dev.${testPublicHostedZone.domainName}`,
                    HostedZoneId: testPublicHostedZone.hostedZoneId,
                }),
            ]),
        });
        template.hasOutput('FrontendSitesCertificateArn', {
            Value: Match.anyValue(),
        });
        template.hasOutput('GenAiGeneratedAssetsCertificateArn', {
            Value: Match.anyValue(),
        });
    });

    it('dev network stack owns the permanent VPC and network guardrails', () => {
        const stack = synthDevNetwork();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::EC2::VPC', 1);
        template.resourceCountIs('AWS::EC2::NatGateway', 1);
        template.resourcePropertiesCountIs('AWS::EC2::VPCEndpoint', {
            VpcEndpointType: 'Interface',
        }, 0);
        template.resourcePropertiesCountIs('AWS::EC2::VPCEndpoint', {
            VpcEndpointType: 'Gateway',
        }, 1);
        template.resourceCountIs('AWS::EC2::SecurityGroup', 4);
    });

    it('dev repository stack owns application image repositories', () => {
        const stack = synthDevRepositories();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::ECR::Repository', 5);
        template.hasResourceProperties('AWS::ECR::Repository', {
            RepositoryName: 'loop-ad/event-collector',
            ImageScanningConfiguration: {
                ScanOnPush: true,
            },
            LifecyclePolicy: Match.objectLike({
                LifecyclePolicyText: Match.anyValue(),
            }),
        });
        template.hasResourceProperties('AWS::ECR::Repository', {
            RepositoryName: 'loop-ad/dashboard-api',
        });
        template.hasResourceProperties('AWS::ECR::Repository', {
            RepositoryName: 'loop-ad/advertisement-api',
        });
        template.hasResourceProperties('AWS::ECR::Repository', {
            RepositoryName: 'loop-ad/decision',
        });
        template.hasOutput('EventCollectorRepositoryUri', {
            Value: Match.anyValue(),
        });
        template.hasOutput('DashboardApiRepositoryUri', {
            Value: Match.anyValue(),
        });
    });

    it('dev app stack keeps DataStorage bucket and five ECS services', () => {
        const stack = synthDev();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::S3::Bucket', 3);
        template.resourceCountIs('AWS::CloudFront::Distribution', 3);
        template.resourceCountIs('AWS::CloudFront::OriginAccessControl', 3);
        template.resourceCountIs('AWS::ECR::Repository', 0);
        template.resourceCountIs('AWS::ECS::Service', 5);
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
        const bucketPolicyStatements = bucketPolicyStatementsFrom(template);
        expect(bucketPolicyStatements).toEqual(expect.arrayContaining([
            expect.objectContaining({
                Effect: 'Allow',
                Principal: {
                    Service: 'cloudfront.amazonaws.com',
                },
                Action: 's3:GetObject',
                Condition: {
                    StringEquals: {
                        'AWS:SourceArn': expect.anything(),
                    },
                },
            }),
        ]));
        template.hasResourceProperties('AWS::CloudFront::Distribution', {
            DistributionConfig: Match.objectLike({
                Aliases: [`gen-ai.asset.dev.${testPublicHostedZone.domainName}`],
                Origins: Match.arrayWith([
                    Match.objectLike({
                        OriginPath: '/genai/generated',
                        OriginAccessControlId: Match.anyValue(),
                    }),
                ]),
                DefaultCacheBehavior: Match.objectLike({
                    ViewerProtocolPolicy: 'redirect-to-https',
                    AllowedMethods: ['GET', 'HEAD'],
                    CachedMethods: ['GET', 'HEAD'],
                }),
                ViewerCertificate: Match.objectLike({
                    AcmCertificateArn: testCertificateArns.genAiGeneratedAssetsCertificateArn,
                    SslSupportMethod: 'sni-only',
                    MinimumProtocolVersion: 'TLSv1.2_2021',
                }),
            }),
        });
        template.hasResourceProperties('AWS::CloudFront::Distribution', {
            DistributionConfig: Match.objectLike({
                Aliases: [`dashboard.dev.${testPublicHostedZone.domainName}`],
                DefaultRootObject: 'index.html',
                CustomErrorResponses: Match.arrayWith([
                    Match.objectLike({
                        ErrorCode: 403,
                        ResponseCode: 200,
                        ResponsePagePath: '/index.html',
                    }),
                    Match.objectLike({
                        ErrorCode: 404,
                        ResponseCode: 200,
                        ResponsePagePath: '/index.html',
                    }),
                ]),
            }),
        });
        template.hasResourceProperties('AWS::CloudFront::Distribution', {
            DistributionConfig: Match.objectLike({
                Aliases: [`demo-shoppingmall.dev.${testPublicHostedZone.domainName}`],
                DefaultRootObject: 'index.html',
                CustomErrorResponses: Match.arrayWith([
                    Match.objectLike({
                        ErrorCode: 403,
                        ResponseCode: 200,
                        ResponsePagePath: '/index.html',
                    }),
                    Match.objectLike({
                        ErrorCode: 404,
                        ResponseCode: 200,
                        ResponsePagePath: '/index.html',
                    }),
                ]),
            }),
        });
        template.hasResourceProperties('AWS::ECS::Service', {
            ServiceName: 'dev-event-collector',
            LaunchType: 'FARGATE',
        });
        template.hasResourceProperties('AWS::ECS::Service', {
            ServiceName: 'dev-decision',
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
                        Values: ['/api/ads/*', '/advertisements/*'],
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

    it('dev stack creates Route53 aliases for public API, ingest, frontend, and GenAI assets subdomains', () => {
        const stack = synthDev();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::Route53::RecordSet', 5);
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
            `dashboard.dev.${testPublicHostedZone.domainName}.`,
            `demo-shoppingmall.dev.${testPublicHostedZone.domainName}.`,
            `gen-ai.asset.dev.${testPublicHostedZone.domainName}.`,
        ]));
    });

    it('dev stack creates the cost-capped data storage shape', () => {
        const stack = synthDev();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::RDS::DBCluster', 1);
        template.resourceCountIs('AWS::RDS::DBInstance', 1);
        template.resourceCountIs('AWS::ElastiCache::ServerlessCache', 1);
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
        template.hasResourceProperties('AWS::ElastiCache::ServerlessCache', {
            Engine: 'valkey',
            MajorEngineVersion: '7',
            ServerlessCacheName: 'dev-loop-ad-valkey',
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
            KafkaVersion: '3.9.x',
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
        expect(JSON.stringify(ssmParameterValueFrom(template, '/loop-ad/dev/redis/endpoint'))).toContain('Endpoint.Address');
        expect(JSON.stringify(ssmParameterValueFrom(template, '/loop-ad/dev/clickhouse/endpoint'))).toContain('PrivateDnsName');
        expect(JSON.stringify(ssmParameterValueFrom(template, '/loop-ad/dev/msk/bootstrap-brokers'))).toContain('BootstrapBrokerString');
        expect(JSON.stringify(ssmParameterValueFrom(template, '/loop-ad/dev/data-storage/bucket-name'))).toContain('DataStorageBucket');
        expect(ssmParameterValueFrom(template, '/loop-ad/dev/data-storage/genai-generated-prefix')).toBe('genai/generated/');
        expect(ssmParameterValueFrom(template, '/loop-ad/dev/data-storage/genai-generated-assets-public-base-url')).toBe(`https://gen-ai.asset.dev.${testPublicHostedZone.domainName}`);
        expect(ssmParameterValueFrom(template, '/loop-ad/dev/frontend/dashboard-web/bucket-name')).toBe('loop-ad-dev-dashboard-web');
        expect(JSON.stringify(ssmParameterValueFrom(template, '/loop-ad/dev/frontend/dashboard-web/cloudfront-distribution-id'))).toContain('DashboardWebDistribution');
        expect(ssmParameterValueFrom(template, '/loop-ad/dev/frontend/demo-shoppingmall-web/bucket-name')).toBe('loop-ad-dev-demo-shoppingmall-web');
        expect(JSON.stringify(ssmParameterValueFrom(template, '/loop-ad/dev/frontend/demo-shoppingmall-web/cloudfront-distribution-id'))).toContain('DemoShoppingmallWebDistribution');
        template.hasResourceProperties('AWS::ECS::TaskDefinition', {
            ContainerDefinitions: Match.arrayWith([
                Match.objectLike({
                    Name: 'event-collector',
                    Environment: Match.arrayWith([
                        Match.objectLike({
                            Name: 'PORT',
                            Value: '80',
                        }),
                        Match.objectLike({
                            Name: 'LOOPAD_MSK_BOOTSTRAP_BROKERS',
                            Value: Match.anyValue(),
                        }),
                        Match.objectLike({
                            Name: 'LOOPAD_EVENT_TOPIC',
                            Value: 'loop-ad.events.raw',
                        }),
                    ]),
                }),
            ]),
        });
        template.hasResourceProperties('AWS::ECS::TaskDefinition', {
            ContainerDefinitions: Match.arrayWith([
                Match.objectLike({
                    Name: 'advertisement-api',
                    Environment: Match.arrayWith([
                        Match.objectLike({
                            Name: 'LOOPAD_REDIS_URL',
                            Value: Match.anyValue(),
                        }),
                        Match.objectLike({
                            Name: 'LOOPAD_AURORA_HOST',
                            Value: Match.anyValue(),
                        }),
                        Match.objectLike({
                            Name: 'LOOPAD_AURORA_DATABASE',
                            Value: 'loopad',
                        }),
                    ]),
                    Secrets: Match.arrayWith([
                        Match.objectLike({ Name: 'LOOPAD_AURORA_USERNAME' }),
                        Match.objectLike({ Name: 'LOOPAD_AURORA_PASSWORD' }),
                    ]),
                }),
            ]),
        });
        template.hasResourceProperties('AWS::ECS::TaskDefinition', {
            ContainerDefinitions: Match.arrayWith([
                Match.objectLike({
                    Name: 'dashboard-api',
                    Environment: Match.arrayWith([
                        Match.objectLike({
                            Name: 'LOOPAD_CLICKHOUSE_URL',
                            Value: Match.anyValue(),
                        }),
                        Match.objectLike({
                            Name: 'LOOPAD_DATA_STORAGE_BUCKET',
                            Value: Match.anyValue(),
                        }),
                        Match.objectLike({
                            Name: 'LOOPAD_GENAI_GENERATED_ASSETS_PREFIX',
                            Value: 'genai/generated/',
                        }),
                    ]),
                    Secrets: Match.arrayWith([
                        Match.objectLike({ Name: 'LOOPAD_AURORA_USERNAME' }),
                        Match.objectLike({ Name: 'LOOPAD_AURORA_PASSWORD' }),
                    ]),
                }),
            ]),
        });
        template.hasResourceProperties('AWS::ECS::TaskDefinition', {
            ContainerDefinitions: Match.arrayWith([
                Match.objectLike({
                    Name: 'decision',
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
                    Secrets: Match.arrayWith([
                        Match.objectLike({ Name: 'LOOPAD_AURORA_USERNAME' }),
                        Match.objectLike({ Name: 'LOOPAD_AURORA_PASSWORD' }),
                        Match.objectLike({ Name: 'LOOPAD_OPENAI_API_KEY' }),
                    ]),
                }),
            ]),
        });
        const synthesizedTemplate = JSON.stringify(template.toJSON());
        expect(synthesizedTemplate).toContain('clickhouse/clickhouse-server:26.3.13.31');
        expect(synthesizedTemplate).toContain('rediss://');
        expect(synthesizedTemplate).toContain('genai/generated/*');
        expect(synthesizedTemplate).not.toContain('ENDPOINT_PARAMETER');
        expect(synthesizedTemplate).not.toContain('SECRET_PARAMETER');
        expect(synthesizedTemplate).not.toContain('LOOPAD_COMPUTE_TARGET');
        expect(synthesizedTemplate).not.toContain('LOOPAD_DECISION_URL');
        expect(synthesizedTemplate).not.toContain('LOOPAD_GENAI_GENERATED_ASSETS_PUBLIC_BASE_URL');
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

function bucketPolicyStatementsFrom(template: Template): Record<string, unknown>[] {
    const resources = template.toJSON().Resources as Record<string, { Type: string; Properties?: Record<string, unknown> }>;
    return Object.values(resources).flatMap((resource) => {
        if (resource.Type !== 'AWS::S3::BucketPolicy') {
            return [];
        }

        const policyDocument = resource.Properties?.PolicyDocument as { Statement?: Record<string, unknown>[] } | undefined;
        return policyDocument?.Statement ?? [];
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
    const network = new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env: testEnv,
    });
    return new LoopAdDevStack(app, 'LoopAdDevStack', {
        env: testEnv,
        publicHostedZone: testPublicHostedZone,
        certificateArns: testCertificateArns,
        network,
    });
}

function synthDevRepositories(): LoopAdDevRepositoryStack {
    const app = new cdk.App();
    return new LoopAdDevRepositoryStack(app, 'LoopAdDevRepositoryStack', {
        env: testEnv,
    });
}

function synthDevCertificate(): LoopAdDevCertificateStack {
    const app = new cdk.App();
    return new LoopAdDevCertificateStack(app, 'LoopAdDevCertificateStack', {
        env: {
            account: testEnv.account,
            region: 'us-east-1',
        },
        publicHostedZone: testPublicHostedZone,
    });
}

function synthDevNetwork(): LoopAdDevNetworkStack {
    const app = new cdk.App();
    return new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env: testEnv,
    });
}

import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { LOOP_AD_REGION, LoopAdDevStack, LoopAdPerfStack } from '../src/loop-ad-stack';

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
        template.resourceCountIs('AWS::ECR::Repository', 5);
        template.resourceCountIs('AWS::ECS::Service', 5);
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

    it('perf stack imports the dev VPC and creates only the temporary collection path', () => {
        const stack = synthPerf();
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::EC2::VPC', 0);
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
        template.hasOutput('EndpointSecurityGroupId', {
            Export: {
                Name: 'loop-ad-dev-vpc-endpoint-security-group-id',
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

function synthDev(): LoopAdDevStack {
    const app = new cdk.App();
    return new LoopAdDevStack(app, 'LoopAdDevStack', {
        env: testEnv,
        enableNatGateway: false,
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

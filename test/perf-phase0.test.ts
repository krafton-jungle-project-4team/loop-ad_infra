import { Match, Template } from 'aws-cdk-lib/assertions';
import { resourcesOf, synthPerfPhase0 } from './helpers';

describe('performance test phase 0 infrastructure', () => {
    it('creates an isolated internal ALB fixed-response endpoint', () => {
        const template = Template.fromStack(synthPerfPhase0());

        template.resourcePropertiesCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', {
            Type: 'application',
            Scheme: 'internal',
        }, 1);
        template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
            Port: 80,
            Protocol: 'HTTP',
            DefaultActions: [
                Match.objectLike({
                    Type: 'fixed-response',
                    FixedResponseConfig: Match.objectLike({
                        StatusCode: '404',
                    }),
                }),
            ],
        });
        template.hasResourceProperties('AWS::ElasticLoadBalancingV2::ListenerRule', {
            Conditions: [
                Match.objectLike({
                    Field: 'path-pattern',
                    PathPatternConfig: {
                        Values: ['/__fixed'],
                    },
                }),
            ],
            Actions: [
                Match.objectLike({
                    Type: 'fixed-response',
                    FixedResponseConfig: Match.objectLike({
                        StatusCode: '204',
                    }),
                }),
            ],
        });
        template.resourceCountIs('AWS::ECS::Service', 0);
    });

    it('uses one EC2 Spot-backed ECS capacity unit for the load generator', () => {
        const template = Template.fromStack(synthPerfPhase0());

        template.resourceCountIs('AWS::ECS::Cluster', 1);
        template.hasResourceProperties('AWS::AutoScaling::AutoScalingGroup', {
            MinSize: '1',
            MaxSize: '1',
            DesiredCapacity: '1',
        });
        template.hasResourceProperties('AWS::EC2::LaunchTemplate', {
            LaunchTemplateData: Match.objectLike({
                InstanceType: 'c6in.8xlarge',
                InstanceMarketOptions: {
                    MarketType: 'spot',
                    SpotOptions: {
                        MaxPrice: '1.6',
                    },
                },
            }),
        });
    });

    it('defines a one-shot k6 task for the 50k rps ALB fixed-response run', () => {
        const template = Template.fromStack(synthPerfPhase0());
        const templateText = JSON.stringify(template.toJSON());

        template.hasResourceProperties('AWS::ECS::TaskDefinition', {
            NetworkMode: 'bridge',
            ContainerDefinitions: [
                Match.objectLike({
                    Name: 'phase0-k6-load-generator',
                    Image: 'grafana/k6:latest',
                    Cpu: 30720,
                    Memory: 57344,
                    EntryPoint: ['sh', '-c'],
                    Environment: Match.arrayWith([
                        { Name: 'TARGET_RPS', Value: '50000' },
                        { Name: 'DURATION', Value: '5m' },
                        { Name: 'PAYLOAD_BYTES', Value: '1024' },
                        { Name: 'PRE_ALLOCATED_VUS', Value: '4096' },
                        { Name: 'MAX_VUS', Value: '20000' },
                    ]),
                }),
            ],
        });
        template.hasResourceProperties('AWS::Logs::LogGroup', {
            LogGroupName: '/loop-ad/perf/phase0/k6',
            RetentionInDays: 7,
        });
        expect(templateText).toContain('constant-arrival-rate');
        expect(templateText).toContain('K6_SUMMARY_JSON_BEGIN');
    });

    it('limits phase 0 security group ingress to load generator traffic', () => {
        const template = Template.fromStack(synthPerfPhase0());
        const resources = resourcesOf(template);
        const serialized = JSON.stringify(resources);

        expect(serialized).not.toContain('0.0.0.0/0","FromPort":80');
        expect(serialized).toContain('Load generator may send phase 0 traffic to ALB.');
    });
});

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
        template.resourceCountIs('AWS::ECS::TaskDefinition', 0);
        template.resourceCountIs('AWS::AutoScaling::AutoScalingGroup', 0);
        template.resourceCountIs('AWS::EC2::LaunchTemplate', 0);
    });

    it('creates a security group for Artillery Fargate workers', () => {
        const template = Template.fromStack(synthPerfPhase0());

        template.resourceCountIs('AWS::EC2::SecurityGroup', 2);
        template.hasResourceProperties('AWS::EC2::SecurityGroup', {
            GroupDescription: 'Perf phase 0 internal ALB fixed response endpoint.',
        });
        template.hasResourceProperties('AWS::EC2::SecurityGroup', {
            GroupDescription: 'Perf phase 0 Artillery Fargate workers.',
        });
    });

    it('creates scoped Artillery runner and worker IAM roles', () => {
        const template = Template.fromStack(synthPerfPhase0());
        const templateText = JSON.stringify(template.toJSON());

        template.resourceCountIs('AWS::IAM::Role', 2);
        template.hasResourceProperties('AWS::IAM::Role', {
            RoleName: 'loop-ad-perf-phase0-artillery-runner',
        });
        template.hasResourceProperties('AWS::IAM::Role', {
            RoleName: 'loop-ad-perf-phase0-artillery-worker',
            AssumeRolePolicyDocument: {
                Statement: [
                    Match.objectLike({
                        Principal: {
                            Service: 'ecs-tasks.amazonaws.com',
                        },
                        Condition: Match.objectLike({
                            StringEquals: {
                                'aws:SourceAccount': '123456789012',
                            },
                        }),
                    }),
                ],
            },
        });
        expect(templateText).toContain('iam:PassedToService');
        expect(templateText).toContain('ecs-tasks.amazonaws.com');
        expect(templateText).toContain('AWSServiceRoleForECS');
        expect(templateText).toContain('artilleryio-test-data-*');
        expect(templateText).toContain('artilleryio*');
        expect(templateText).toContain('parameter/artilleryio/*');
        expect(templateText).not.toContain('"iam:PassRole","Resource":"*"');
    });

    it('outputs Artillery run-fargate inputs for the 50k rps run', () => {
        const template = Template.fromStack(synthPerfPhase0());
        const templateText = JSON.stringify(template.toJSON());

        expect(templateText).toContain('Phase0ArtilleryTargetBaseUrl');
        expect(templateText).toContain('Phase0ArtillerySubnetIds');
        expect(templateText).toContain('Phase0ArtillerySecurityGroupId');
        expect(templateText).toContain('Phase0ArtilleryClusterName');
        expect(templateText).toContain('Phase0ArtilleryRunnerRoleArn');
        expect(templateText).toContain('Phase0ArtilleryWorkerRoleName');
        expect(templateText).toContain('artillery run-fargate');
        expect(templateText).toContain('--cluster');
        expect(templateText).toContain('--count 20');
        expect(templateText).toContain('--spot');
        expect(templateText).toContain('--task-role-name');
        expect(templateText).toContain('performance-tests/phase0/alb-fixed-response.yml');
    });

    it('limits phase 0 security group ingress to load generator traffic', () => {
        const template = Template.fromStack(synthPerfPhase0());
        const resources = resourcesOf(template);
        const serialized = JSON.stringify(resources);

        expect(serialized).not.toContain('0.0.0.0/0","FromPort":80');
        expect(serialized).toContain('Artillery Fargate workers may reach ALB HTTP listener.');
        expect(serialized).toContain('Artillery Fargate workers may send phase 0 traffic to ALB.');
    });
});

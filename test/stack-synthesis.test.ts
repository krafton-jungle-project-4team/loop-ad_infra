import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { buildLoopAdEnvironment } from '../src/app/build-loop-ad-environment';
import { ENVIRONMENT_MODES, LOOP_AD_REGION } from '../src/config/loop-ad-config';

const testEnv = {
  account: '123456789012',
  region: LOOP_AD_REGION,
};

describe('CDK stack synthesis', () => {
  it('StorageStack이 서비스 정의에서 ECR repository를 만든다', () => {
    const stacks = synth('dev');
    const template = Template.fromStack(stacks.storage);

    template.resourceCountIs('AWS::ECR::Repository', 5);
    template.hasResourceProperties('AWS::ECR::Repository', {
      RepositoryName: 'loopad/event-collector',
    });
    template.hasResourceProperties('AWS::ECR::Repository', {
      RepositoryName: 'loopad/dashboard-api',
    });
  });

  it('Data/Stream stack이 datastore endpoint contract를 SSM으로 만든다', () => {
    const stacks = synth('dev');

    Template.fromStack(stacks.data).hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/loop-ad/dev/redis/endpoint',
      Value: 'pending://dev/redis',
    });
    Template.fromStack(stacks.stream).hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/loop-ad/dev/msk/bootstrap-brokers',
      Value: 'pending://dev/msk',
    });
  });

  it('dev에서는 collector가 Fargate로 실행되고 NLB target으로만 붙는다', () => {
    const stacks = synth('dev');
    const collectTemplate = Template.fromStack(stacks.collect);
    const edgeTemplate = Template.fromStack(stacks.edge);

    collectTemplate.hasResourceProperties('AWS::ECS::Service', {
      ServiceName: 'dev-event-collector',
      LaunchType: 'FARGATE',
    });
    edgeTemplate.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 80,
      Protocol: 'TCP',
    });
    edgeTemplate.resourceCountIs('AWS::ElasticLoadBalancingV2::ListenerRule', 2);
  });

  it('perf에서는 collector/projector가 ECS on EC2 capacity provider 경로를 사용한다', () => {
    const stacks = synth('perf');

    Template.fromStack(stacks.network).resourceCountIs('AWS::AutoScaling::AutoScalingGroup', 1);
    Template.fromStack(stacks.collect).hasResourceProperties('AWS::ECS::Service', {
      ServiceName: 'perf-event-collector',
      CapacityProviderStrategy: Match.arrayWith([
        Match.objectLike({
          Weight: 1,
        }),
      ]),
    });
    Template.fromStack(stacks.analytics).hasResourceProperties('AWS::ECS::Service', {
      ServiceName: 'perf-ad-context-projector',
      CapacityProviderStrategy: Match.arrayWith([
        Match.objectLike({
          Weight: 1,
        }),
      ]),
    });
    expect(stacks.decision).toBeUndefined();
    expect(stacks.dashboard).toBeUndefined();
  });

  it('ALB는 dev API 서비스 두 개에만 listener rule을 만든다', () => {
    const stacks = synth('dev');

    const edgeTemplate = Template.fromStack(stacks.edge);

    edgeTemplate.hasResourceProperties('AWS::ElasticLoadBalancingV2::ListenerRule', {
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
    edgeTemplate.hasResourceProperties('AWS::ElasticLoadBalancingV2::ListenerRule', {
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

  it('task environment가 endpoint contract와 external placeholder를 받는다', () => {
    const stacks = synth('dev');

    Template.fromStack(stacks.analytics).hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: 'recommendation',
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: 'LOOPAD_CLICKHOUSE_ENDPOINT_PARAMETER',
              Value: Match.anyValue(),
            }),
            Match.objectLike({
              Name: 'LOOPAD_OPENAI_SECRET_PARAMETER',
              Value: '/loop-ad/dev/external/openai/api-key',
            }),
          ]),
        }),
      ]),
    });
  });
});

function synth(modeName: 'dev' | 'perf') {
  const app = new cdk.App();
  return buildLoopAdEnvironment(app, {
    mode: ENVIRONMENT_MODES[modeName],
    env: testEnv,
    enableNatGateway: ENVIRONMENT_MODES[modeName].enableNatGatewayByDefault,
    enableVpcEndpoints: ENVIRONMENT_MODES[modeName].enableVpcEndpointsByDefault,
  });
}

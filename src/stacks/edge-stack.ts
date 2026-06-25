import * as cdk from 'aws-cdk-lib';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Construct } from 'constructs';
import type { EnvironmentMode } from '../config/loop-ad-config';
import type { EdgeResources, NetworkResources } from './stack-interfaces';

export interface EdgeStackProps extends cdk.StackProps {
  readonly mode: EnvironmentMode;
  readonly network: NetworkResources;
}

export class EdgeStack extends cdk.Stack implements EdgeResources {
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly albListener: elbv2.ApplicationListener;
  public readonly nlb: elbv2.NetworkLoadBalancer;

  public constructor(scope: Construct, id: string, props: EdgeStackProps) {
    super(scope, id, props);

    this.alb = new elbv2.ApplicationLoadBalancer(this, 'ApplicationLoadBalancer', {
      vpc: props.network.vpc,
      internetFacing: true,
      securityGroup: props.network.edgeSecurityGroups.alb,
      vpcSubnets: {
        subnetGroupName: 'public',
      },
    });

    this.albListener = this.alb.addListener('HttpListener', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      open: false,
      defaultAction: elbv2.ListenerAction.fixedResponse(404, {
        contentType: 'text/plain',
        messageBody: 'No loop-ad API route is registered.',
      }),
    });

    this.nlb = new elbv2.NetworkLoadBalancer(this, 'NetworkLoadBalancer', {
      vpc: props.network.vpc,
      internetFacing: true,
      securityGroups: [props.network.edgeSecurityGroups.nlb],
      vpcSubnets: {
        subnetGroupName: 'public',
      },
    });

    // Route53은 domain/hosted zone이 정해진 뒤 이 스택 안에서 ALB/NLB alias record만 추가한다.
  }
}

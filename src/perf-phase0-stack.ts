import { CfnOutput, Duration, Fn, Stack, type StackProps } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Construct } from 'constructs';
import { DEV_VPC_AVAILABILITY_ZONES } from './dev-config';

const PHASE0_FIXED_RESPONSE_PATH = '/__fixed';

export class LoopAdPerfPhase0Stack extends Stack {
    public constructor(scope: Construct, id: string, props?: StackProps) {
        super(scope, id, props);

        const vpc = new ec2.Vpc(this, 'Vpc', {
            vpcName: 'perf-phase0-loop-ad-vpc',
            availabilityZones: DEV_VPC_AVAILABILITY_ZONES,
            natGateways: 0,
            restrictDefaultSecurityGroup: false,
            subnetConfiguration: [
                {
                    name: 'public',
                    subnetType: ec2.SubnetType.PUBLIC,
                    cidrMask: 24,
                },
            ],
        });
        const publicSubnets = vpc.selectSubnets({ subnetGroupName: 'public' });

        const albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Perf phase 0 internal ALB fixed response endpoint.',
        });
        const artilleryWorkerSecurityGroup = new ec2.SecurityGroup(this, 'ArtilleryWorkerSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Perf phase 0 Artillery Fargate workers.',
        });
        albSecurityGroup.addIngressRule(artilleryWorkerSecurityGroup, ec2.Port.tcp(80), 'Artillery Fargate workers may reach ALB HTTP listener.');
        artilleryWorkerSecurityGroup.addEgressRule(albSecurityGroup, ec2.Port.tcp(80), 'Artillery Fargate workers may send phase 0 traffic to ALB.');
        artilleryWorkerSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Artillery workers may reach AWS APIs, ECR, S3, SQS, and CloudWatch Logs.');

        const alb = new elbv2.ApplicationLoadBalancer(this, 'ApplicationLoadBalancer', {
            vpc,
            vpcSubnets: publicSubnets,
            internetFacing: false,
            securityGroup: albSecurityGroup,
            loadBalancerName: 'perf-phase0-loop-ad-alb',
            idleTimeout: Duration.seconds(60),
        });
        const listener = alb.addListener('HttpListener', {
            port: 80,
            protocol: elbv2.ApplicationProtocol.HTTP,
            open: false,
            defaultAction: elbv2.ListenerAction.fixedResponse(404, {
                contentType: 'text/plain',
                messageBody: 'No perf phase 0 route is registered.',
            }),
        });
        listener.addAction('FixedResponseAction', {
            priority: 10,
            conditions: [elbv2.ListenerCondition.pathPatterns([PHASE0_FIXED_RESPONSE_PATH])],
            action: elbv2.ListenerAction.fixedResponse(204),
        });

        const artillerySubnetIds = Fn.join(',', publicSubnets.subnetIds);
        const artilleryWorkerSecurityGroupId = artilleryWorkerSecurityGroup.securityGroupId;
        const artilleryTargetBaseUrl = `http://${alb.loadBalancerDnsName}`;
        const fixedResponseTargetUrl = `${artilleryTargetBaseUrl}${PHASE0_FIXED_RESPONSE_PATH}`;

        new CfnOutput(this, 'Phase0LoadBalancerDnsName', {
            value: alb.loadBalancerDnsName,
            description: 'Internal ALB DNS name for phase 0 fixed-response load tests.',
        });
        new CfnOutput(this, 'Phase0TargetUrl', {
            value: fixedResponseTargetUrl,
            description: 'Fixed-response target URL for direct checks.',
        });
        new CfnOutput(this, 'Phase0ArtilleryTargetBaseUrl', {
            value: artilleryTargetBaseUrl,
            description: 'Base target URL for Artillery run-fargate.',
        });
        new CfnOutput(this, 'Phase0ArtillerySubnetIds', {
            value: artillerySubnetIds,
            description: 'Comma-separated public subnet IDs for Artillery Fargate workers.',
        });
        new CfnOutput(this, 'Phase0ArtillerySecurityGroupId', {
            value: artilleryWorkerSecurityGroupId,
            description: 'Security group ID for Artillery Fargate workers.',
        });
        new CfnOutput(this, 'Phase0ArtilleryRunCommand', {
            value: [
                'artillery run-fargate',
                `--region ${Stack.of(this).region}`,
                '--count 20',
                '--spot',
                '--cpu 4',
                '--memory 8',
                `--subnet-ids ${artillerySubnetIds}`,
                `--security-group-ids ${artilleryWorkerSecurityGroupId}`,
                `--target ${artilleryTargetBaseUrl}`,
                '--output performance-tests/run_<id>/artillery-report.json',
                'performance-tests/phase0/alb-fixed-response.yml',
            ].join(' '),
            description: 'Example Artillery Fargate command for the phase 0 50k aggregate rps run.',
        });
    }
}

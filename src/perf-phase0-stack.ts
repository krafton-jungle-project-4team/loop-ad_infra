import { CfnOutput, Duration, RemovalPolicy, Stack, type StackProps } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { DEV_VPC_AVAILABILITY_ZONES } from './dev-config';

const LOAD_GENERATOR_INSTANCE_TYPE = 'c6in.8xlarge';
const LOAD_GENERATOR_SPOT_MAX_PRICE_USD_PER_HOUR = '1.60';
const LOAD_GENERATOR_IMAGE = 'grafana/k6:latest';
const LOAD_GENERATOR_CONTAINER_NAME = 'phase0-k6-load-generator';
const PHASE0_FIXED_RESPONSE_PATH = '/__fixed';
const PHASE0_TARGET_RPS = '50000';
const PHASE0_DURATION = '5m';
const PHASE0_PAYLOAD_BYTES = '1024';
const PHASE0_PRE_ALLOCATED_VUS = '4096';
const PHASE0_MAX_VUS = '20000';

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

        const cluster = new ecs.Cluster(this, 'Cluster', {
            vpc,
            clusterName: 'perf-phase0-loop-ad-cluster',
            containerInsightsV2: ecs.ContainerInsights.ENABLED,
        });
        const loadGeneratorCapacity = cluster.addCapacity('LoadGeneratorCapacity', {
            vpcSubnets: publicSubnets,
            instanceType: new ec2.InstanceType(LOAD_GENERATOR_INSTANCE_TYPE),
            machineImage: ecs.EcsOptimizedImage.amazonLinux2(ecs.AmiHardwareType.STANDARD),
            minCapacity: 1,
            maxCapacity: 1,
            desiredCapacity: 1,
            associatePublicIpAddress: true,
            spotPrice: LOAD_GENERATOR_SPOT_MAX_PRICE_USD_PER_HOUR,
            spotInstanceDraining: true,
            allowAllOutbound: false,
        });
        loadGeneratorCapacity.connections.allowTo(alb, ec2.Port.tcp(80), 'Load generator may send phase 0 traffic to ALB.');
        loadGeneratorCapacity.connections.allowToAnyIpv4(ec2.Port.tcp(443), 'Load generator host may pull images and write AWS service logs.');
        loadGeneratorCapacity.role.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'));

        const logGroup = new logs.LogGroup(this, 'LoadGeneratorLogGroup', {
            logGroupName: '/loop-ad/perf/phase0/k6',
            retention: logs.RetentionDays.ONE_WEEK,
            removalPolicy: RemovalPolicy.DESTROY,
        });
        logGroup.grantWrite(loadGeneratorCapacity.role);

        const taskDefinition = new ecs.Ec2TaskDefinition(this, 'LoadGeneratorTaskDefinition', {
            networkMode: ecs.NetworkMode.BRIDGE,
        });
        taskDefinition.addContainer('LoadGeneratorContainer', {
            containerName: LOAD_GENERATOR_CONTAINER_NAME,
            image: ecs.ContainerImage.fromRegistry(LOAD_GENERATOR_IMAGE),
            cpu: 30720,
            memoryLimitMiB: 57344,
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'k6',
                logGroup,
            }),
            environment: {
                TARGET_URL: `http://${alb.loadBalancerDnsName}${PHASE0_FIXED_RESPONSE_PATH}`,
                TARGET_RPS: PHASE0_TARGET_RPS,
                DURATION: PHASE0_DURATION,
                PAYLOAD_BYTES: PHASE0_PAYLOAD_BYTES,
                PRE_ALLOCATED_VUS: PHASE0_PRE_ALLOCATED_VUS,
                MAX_VUS: PHASE0_MAX_VUS,
            },
            entryPoint: ['sh', '-c'],
            command: [renderK6Command()],
        });

        new CfnOutput(this, 'Phase0LoadBalancerDnsName', {
            value: alb.loadBalancerDnsName,
            description: 'Internal ALB DNS name for phase 0 fixed-response load tests.',
        });
        new CfnOutput(this, 'Phase0TargetUrl', {
            value: `http://${alb.loadBalancerDnsName}${PHASE0_FIXED_RESPONSE_PATH}`,
            description: 'Default k6 target URL.',
        });
        new CfnOutput(this, 'Phase0ClusterName', {
            value: cluster.clusterName,
            description: 'ECS cluster name for phase 0 load generator runs.',
        });
        new CfnOutput(this, 'Phase0TaskDefinitionArn', {
            value: taskDefinition.taskDefinitionArn,
            description: 'ECS task definition ARN for the k6 load generator.',
        });
        new CfnOutput(this, 'Phase0ContainerName', {
            value: LOAD_GENERATOR_CONTAINER_NAME,
            description: 'Container name to use when overriding k6 run settings.',
        });
        new CfnOutput(this, 'Phase0LogGroupName', {
            value: logGroup.logGroupName,
            description: 'CloudWatch Logs group containing k6 output and summary JSON.',
        });
        new CfnOutput(this, 'Phase0RunTaskCommand', {
            value: `aws ecs run-task --cluster ${cluster.clusterName} --launch-type EC2 --task-definition ${taskDefinition.taskDefinitionArn} --count 1`,
            description: 'Run one phase 0 k6 task with the default 50k rps settings.',
        });
    }
}

function renderK6Command(): string {
    return String.raw`cat > /tmp/phase0.js <<'EOF'
import http from 'k6/http';
import { check } from 'k6';

const payloadBytes = Number(__ENV.PAYLOAD_BYTES || '1024');
const body = 'x'.repeat(Math.max(0, payloadBytes - 64));
const payload = JSON.stringify({
  event_id: 'phase0-fixed-response',
  event_type: 'perf.phase0',
  body,
});

export const options = {
  discardResponseBodies: true,
  scenarios: {
    phase0_fixed_response: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.TARGET_RPS || '50000'),
      timeUnit: '1s',
      duration: __ENV.DURATION || '5m',
      preAllocatedVUs: Number(__ENV.PRE_ALLOCATED_VUS || '4096'),
      maxVUs: Number(__ENV.MAX_VUS || '20000'),
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
  },
};

export default function () {
  const response = http.post(__ENV.TARGET_URL, payload, {
    headers: { 'Content-Type': 'application/json' },
    timeout: __ENV.REQUEST_TIMEOUT || '5s',
  });
  check(response, {
    'status is 204': (r) => r.status === 204,
  });
}
EOF

k6 run --summary-export=/tmp/k6-summary.json /tmp/phase0.js
status=$?
echo 'K6_SUMMARY_JSON_BEGIN'
cat /tmp/k6-summary.json || true
echo 'K6_SUMMARY_JSON_END'
exit $status`;
}

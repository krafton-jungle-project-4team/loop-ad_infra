import { CfnOutput, Duration, Fn, Stack, type StackProps } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { DEV_VPC_AVAILABILITY_ZONES } from './dev-config';

const PHASE0_FIXED_RESPONSE_PATH = '/__fixed';
const ARTILLERY_RUNNER_ROLE_NAME = 'loop-ad-perf-phase0-artillery-runner';
const ARTILLERY_WORKER_ROLE_NAME = 'loop-ad-perf-phase0-artillery-worker';
const ARTILLERY_CLUSTER_NAME = 'perf-phase0-loop-ad-artillery-cluster';

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

        const cluster = new ecs.Cluster(this, 'ArtilleryCluster', {
            vpc,
            clusterName: ARTILLERY_CLUSTER_NAME,
            containerInsightsV2: ecs.ContainerInsights.ENABLED,
        });
        const workerRole = createArtilleryWorkerRole(this, cluster);
        const runnerRole = createArtilleryRunnerRole(this, cluster, workerRole);
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
        new CfnOutput(this, 'Phase0ArtilleryClusterName', {
            value: cluster.clusterName,
            description: 'ECS cluster name for Artillery Fargate workers.',
        });
        new CfnOutput(this, 'Phase0ArtilleryRunnerRoleArn', {
            value: runnerRole.roleArn,
            description: 'Role ARN to assume before running Artillery.',
        });
        new CfnOutput(this, 'Phase0ArtilleryWorkerRoleName', {
            value: workerRole.roleName,
            description: 'Task role name for Artillery Fargate workers.',
        });
        new CfnOutput(this, 'Phase0ArtilleryWorkerRoleArn', {
            value: workerRole.roleArn,
            description: 'Task role ARN for Artillery Fargate workers.',
        });
        new CfnOutput(this, 'Phase0ArtilleryAssumeRunnerRoleCommand', {
            value: `aws sts assume-role --role-arn ${runnerRole.roleArn} --role-session-name loop-ad-perf-phase0`,
            description: 'Assume this role, then run Artillery with the exported temporary credentials.',
        });
        new CfnOutput(this, 'Phase0ArtilleryRunCommand', {
            value: [
                'artillery run-fargate',
                `--region ${Stack.of(this).region}`,
                `--cluster ${cluster.clusterName}`,
                '--count 20',
                '--spot',
                '--cpu 4',
                '--memory 8',
                `--task-role-name ${workerRole.roleName}`,
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

function createArtilleryWorkerRole(scope: Construct, cluster: ecs.ICluster): iam.Role {
    const stack = Stack.of(scope);
    const workerRole = new iam.Role(scope, 'ArtilleryWorkerRole', {
        roleName: ARTILLERY_WORKER_ROLE_NAME,
        assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com', {
            conditions: {
                StringEquals: {
                    'aws:SourceAccount': stack.account,
                },
                ArnLike: {
                    'aws:SourceArn': `arn:${stack.partition}:ecs:${stack.region}:${stack.account}:*`,
                },
            },
        }),
        description: 'Perf phase 0 Artillery Fargate worker task role.',
    });

    workerRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy'));
    workerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtilleryWorkerSqsAccess',
        actions: ['sqs:*'],
        resources: [artillerySqsArn(scope)],
    }));
    workerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtilleryWorkerS3Access',
        actions: [
            's3:GetObject',
            's3:GetObjectAcl',
            's3:GetObjectTagging',
            's3:GetObjectVersion',
            's3:PutObject',
            's3:PutObjectAcl',
            's3:ListBucket',
            's3:GetBucketLocation',
        ],
        resources: artilleryS3Arns(scope),
    }));
    workerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtilleryWorkerSecretRead',
        actions: [
            'ssm:GetParameter',
            'ssm:GetParameters',
            'ssm:GetParametersByPath',
            'secretsmanager:GetSecretValue',
        ],
        resources: [
            artillerySsmArn(scope),
            artillerySecretsArn(scope),
        ],
    }));

    new CfnOutput(scope, 'Phase0ArtilleryClusterArn', {
        value: cluster.clusterArn,
        description: 'ECS cluster ARN scoped by the Artillery runner role.',
    });

    return workerRole;
}

function createArtilleryRunnerRole(scope: Construct, cluster: ecs.ICluster, workerRole: iam.IRole): iam.Role {
    const stack = Stack.of(scope);
    const runnerRole = new iam.Role(scope, 'ArtilleryRunnerRole', {
        roleName: ARTILLERY_RUNNER_ROLE_NAME,
        assumedBy: new iam.AccountPrincipal(stack.account),
        description: 'Perf phase 0 role used by operators or CI to run Artillery on AWS Fargate.',
        maxSessionDuration: Duration.hours(1),
    });

    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ReadPhase0WorkerRole',
        actions: ['iam:GetRole'],
        resources: [workerRole.roleArn],
    }));
    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'PassOnlyPhase0WorkerRoleToEcsTasks',
        actions: ['iam:PassRole'],
        resources: [workerRole.roleArn],
        conditions: {
            StringEquals: {
                'iam:PassedToService': 'ecs-tasks.amazonaws.com',
            },
        },
    }));
    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'CreateEcsServiceLinkedRoleIfMissing',
        actions: ['iam:CreateServiceLinkedRole'],
        resources: [`arn:${stack.partition}:iam::*:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS*`],
        conditions: {
            StringLike: {
                'iam:AWSServiceName': 'ecs.amazonaws.com',
            },
        },
    }));
    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtilleryEcsGeneral',
        actions: [
            'ecs:ListClusters',
            'ecs:RegisterTaskDefinition',
            'ecs:DeregisterTaskDefinition',
            'ecs:ListTaskDefinitions',
            'ecs:DescribeTaskDefinition',
        ],
        resources: ['*'],
    }));
    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtilleryEcsClusterRead',
        actions: [
            'ecs:DescribeClusters',
            'ecs:ListContainerInstances',
        ],
        resources: [cluster.clusterArn],
    }));
    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtilleryEcsRunOnPhase0Cluster',
        actions: [
            'ecs:SubmitTaskStateChange',
            'ecs:DescribeTasks',
            'ecs:ListTasks',
            'ecs:StartTask',
            'ecs:StopTask',
            'ecs:RunTask',
        ],
        resources: ['*'],
        conditions: {
            ArnEquals: {
                'ecs:cluster': cluster.clusterArn,
            },
        },
    }));
    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtillerySqsAccess',
        actions: ['sqs:*'],
        resources: [artillerySqsArn(scope)],
    }));
    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtilleryS3Access',
        actions: [
            's3:CreateBucket',
            's3:DeleteObject',
            's3:GetObject',
            's3:GetObjectAcl',
            's3:GetObjectTagging',
            's3:GetObjectVersion',
            's3:PutObject',
            's3:PutObjectAcl',
            's3:ListBucket',
            's3:GetBucketLocation',
            's3:GetBucketLogging',
            's3:GetBucketPolicy',
            's3:GetBucketTagging',
            's3:PutBucketPolicy',
            's3:PutBucketTagging',
            's3:PutMetricsConfiguration',
            's3:GetLifecycleConfiguration',
            's3:PutLifecycleConfiguration',
        ],
        resources: artilleryS3Arns(scope),
    }));
    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtillerySsmAccess',
        actions: [
            'ssm:PutParameter',
            'ssm:GetParameter',
            'ssm:GetParameters',
            'ssm:DeleteParameter',
            'ssm:DescribeParameters',
            'ssm:GetParametersByPath',
        ],
        resources: [artillerySsmArn(scope)],
    }));
    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtillerySecretRead',
        actions: ['secretsmanager:GetSecretValue'],
        resources: [artillerySecretsArn(scope)],
    }));
    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtilleryLogRetention',
        actions: ['logs:PutRetentionPolicy'],
        resources: [`arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:artilleryio-log-group/*`],
    }));
    runnerRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ArtilleryVpcDiscovery',
        actions: [
            'ec2:DescribeRouteTables',
            'ec2:DescribeVpcs',
            'ec2:DescribeSubnets',
        ],
        resources: ['*'],
    }));

    return runnerRole;
}

function artilleryS3Arns(scope: Construct): string[] {
    const stack = Stack.of(scope);
    return [
        `arn:${stack.partition}:s3:::artilleryio-test-data-*`,
        `arn:${stack.partition}:s3:::artilleryio-test-data-*/*`,
    ];
}

function artillerySqsArn(scope: Construct): string {
    const stack = Stack.of(scope);
    return `arn:${stack.partition}:sqs:${stack.region}:${stack.account}:artilleryio*`;
}

function artillerySsmArn(scope: Construct): string {
    const stack = Stack.of(scope);
    return `arn:${stack.partition}:ssm:${stack.region}:${stack.account}:parameter/artilleryio/*`;
}

function artillerySecretsArn(scope: Construct): string {
    const stack = Stack.of(scope);
    return `arn:${stack.partition}:secretsmanager:${stack.region}:${stack.account}:secret:artilleryio/*`;
}

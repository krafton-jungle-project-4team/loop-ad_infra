import { Duration, RemovalPolicy, Stack, type StackProps } from 'aws-cdk-lib';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as msk from 'aws-cdk-lib/aws-msk';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';

export const LOOP_AD_REGION = 'ap-northeast-2';
export const LOOP_AD_MONTHLY_COST_TARGET_USD = 300;
export const LOOP_AD_AGGREGATION_PERF_TARGET_RPS = 20_000;

const DEV_SERVICE_DESIRED_TASKS = 1;
const DEV_SERVICE_MIN_TASKS = 1;
const DEV_SERVICE_MAX_TASKS = 2;
const SERVICE_CPU_SCALE_TARGET_PERCENT = 70;
const DEV_AURORA_MIN_ACU = 0;
const DEV_AURORA_MAX_ACU = 2;
const DEV_AURORA_AUTO_PAUSE_MINUTES = 10;
const DEV_CLICKHOUSE_VOLUME_GIB = 50;
const DEV_MSK_BROKER_COUNT = 2;
const DEV_MSK_STORAGE_GIB_PER_BROKER = 20;
const AGGREGATION_PERF_EC2_DESIRED_INSTANCES = 6;
const AGGREGATION_PERF_EC2_MIN_INSTANCES = 6;
const AGGREGATION_PERF_EC2_MAX_INSTANCES = 12;
const AGGREGATION_PERF_EC2_INSTANCE_TYPE = 'c7g.xlarge';
const AGGREGATION_PERF_EVENT_COLLECTOR_DESIRED_TASKS = 24;
const AGGREGATION_PERF_EVENT_COLLECTOR_MIN_TASKS = 24;
const AGGREGATION_PERF_EVENT_COLLECTOR_MAX_TASKS = 48;
const AGGREGATION_PERF_PROJECTOR_DESIRED_TASKS = 12;
const AGGREGATION_PERF_PROJECTOR_MIN_TASKS = 12;
const AGGREGATION_PERF_PROJECTOR_MAX_TASKS = 24;
const AGGREGATION_PERF_CPU_SCALE_TARGET_PERCENT = 50;
const AGGREGATION_PERF_CLICKHOUSE_INSTANCE_TYPE = 'c7g.xlarge';
const AGGREGATION_PERF_CLICKHOUSE_VOLUME_GIB = 500;
const AGGREGATION_PERF_CLICKHOUSE_VOLUME_IOPS = 3_000;
const AGGREGATION_PERF_MSK_BROKER_COUNT = 2;
const AGGREGATION_PERF_MSK_BROKER_INSTANCE_TYPE = 'kafka.m7g.xlarge';
const AGGREGATION_PERF_MSK_STORAGE_GIB_PER_BROKER = 200;
const AGGREGATION_PERF_MSK_VOLUME_THROUGHPUT = 250;
const AGGREGATION_PERF_MSK_TOPIC_PARTITIONS = 128;
const AGGREGATION_PERF_MSK_TOPIC_REPLICATION_FACTOR = 2;
const AGGREGATION_PERF_MSK_KAFKA_VERSION = '3.6.0';

export interface PublicHostedZoneConfig {
    readonly hostedZoneId: string;
    readonly domainName: string;
}

export interface LoopAdDevStackProps extends StackProps {
    readonly publicHostedZone: PublicHostedZoneConfig;
}

export interface LoopAdAggregationPerfStackProps extends StackProps {
    readonly publicHostedZone: PublicHostedZoneConfig;
}

// 상시 개발 스택입니다. 공유 VPC와 항상 떠 있는 개발 서버를 소유합니다.
export class LoopAdDevStack extends Stack {
    public constructor(scope: Construct, id: string, props: LoopAdDevStackProps) {
        super(scope, id, props);

        // Dev가 유일한 VPC를 만듭니다. Dev server는 NAT가 있는 private subnet을 쓰고,
        // Aggregation perf server는 NAT 없이 public subnet만 import해서 사용합니다.
        const vpc = new ec2.Vpc(this, 'Vpc', {
            vpcName: 'dev-loop-ad-vpc',
            maxAzs: 2,
            natGateways: 1,
            subnetConfiguration: [
                {
                    name: 'public',
                    subnetType: ec2.SubnetType.PUBLIC,
                    cidrMask: 24,
                },
                {
                    name: 'private',
                    subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidrMask: 24,
                },
            ],
        });

        // 모든 ECS 서비스는 private subnet 그룹에서 실행됩니다.
        const privateSubnets = vpc.selectSubnets({ subnetGroupName: 'private' });

        // 비용 guardrail입니다. AWS Budget은 지출을 차단하지는 않지만 월간 목표를 계정에 명시합니다.
        new budgets.CfnBudget(this, 'MonthlyCostBudget', {
            budget: {
                budgetName: 'loop-ad-monthly-cost-target',
                budgetLimit: {
                    amount: LOOP_AD_MONTHLY_COST_TARGET_USD,
                    unit: 'USD',
                },
                budgetType: 'COST',
                timeUnit: 'MONTHLY',
            },
        });

        // S3 Gateway Endpoint는 hourly 비용 없이 route table에 붙습니다.
        // ECR layer 다운로드와 S3 접근 비용을 NAT data processing으로 보내지 않기 위해 유지합니다.
        vpc.addGatewayEndpoint('S3GatewayEndpoint', {
            service: ec2.GatewayVpcEndpointAwsService.S3,
            subnets: [privateSubnets],
        });

        // Dev는 상시 운영 환경이므로 Fargate cluster를 사용합니다.
        const cluster = new ecs.Cluster(this, 'Cluster', {
            vpc,
            clusterName: 'dev-loop-ad-cluster',
            containerInsightsV2: ecs.ContainerInsights.ENABLED,
            defaultCloudMapNamespace: {
                name: 'dev.loop-ad.local',
            },
        });

        // public ingress는 load balancer 종류별로 나누고, private 서비스는
        // stack을 읽기 쉽게 유지하기 위해 넓은 내부 SG를 공유합니다.
        const albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Dev ALB public HTTP ingress only.',
        });
        const nlbSecurityGroup = new ec2.SecurityGroup(this, 'NlbSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Dev NLB event ingestion ingress only.',
        });
        const serverSecurityGroup = new ec2.SecurityGroup(this, 'ServerSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Dev ECS server SG shared by app services.',
        });
        const dataSourceSecurityGroup = new ec2.SecurityGroup(this, 'DataSourceSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Dev datasource SG shared by internal data endpoints.',
        });

        // 인터넷 트래픽은 public load balancer만 받습니다.
        albSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public HTTP to dev ALB.');
        nlbSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public ingest to dev NLB.');

        // VPC 내부에서는 SG 경계를 기준으로 server와 datasource가 서로 신뢰합니다.
        albSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'ALB may reach dev servers.');
        nlbSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'NLB may reach dev servers.');
        serverSecurityGroup.addIngressRule(albSecurityGroup, ec2.Port.allTraffic(), 'ALB may enter dev servers.');
        serverSecurityGroup.addIngressRule(nlbSecurityGroup, ec2.Port.allTraffic(), 'NLB may enter dev servers.');
        serverSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may call each other.');
        serverSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may call each other.');
        serverSecurityGroup.addEgressRule(dataSourceSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may reach internal datasources.');
        dataSourceSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may enter internal datasources.');
        dataSourceSecurityGroup.addIngressRule(dataSourceSecurityGroup, ec2.Port.allTraffic(), 'Dev datasources may call each other.');
        dataSourceSecurityGroup.addEgressRule(dataSourceSecurityGroup, ec2.Port.allTraffic(), 'Dev datasources may call each other.');

        // Dev server는 외부 SaaS/API 및 AWS public API를 NAT로 호출합니다.
        serverSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Dev servers may use external HTTPS through NAT.');
        // ClickHouse bootstrap과 datasource 관리 작업도 NAT를 통해 HTTPS를 사용할 수 있습니다.
        dataSourceSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Dev datasources may use external HTTPS through NAT.');

        // ECR repository는 Dev가 소유합니다. Aggregation perf는 임시 image storage를 만들지 않고
        // 이 repository들을 재사용합니다.
        const [
            eventCollectorRepository,
            projectorRepository,
            decisionRepository,
            dashboardRepository,
            recommendationRepository,
        ] = [
            { id: 'EventCollectorRepository', repositoryName: 'loop-ad/event-collector' },
            { id: 'AdContextProjectorRepository', repositoryName: 'loop-ad/ad-context-projector' },
            { id: 'AdDecisionApiRepository', repositoryName: 'loop-ad/ad-decision-api' },
            { id: 'DashboardApiRepository', repositoryName: 'loop-ad/dashboard-api' },
            { id: 'RecommendationRepository', repositoryName: 'loop-ad/recommendation' },
        ].map((repository) => new ecr.Repository(this, repository.id, {
            repositoryName: repository.repositoryName,
            imageScanOnPush: true,
            lifecycleRules: [{ maxImageCount: 20 }],
            removalPolicy: RemovalPolicy.RETAIN,
        }));

        const aggregationPerfResultsBucket = new s3.Bucket(this, 'AggregationPerfResultsBucket', {
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            encryption: s3.BucketEncryption.S3_MANAGED,
            enforceSSL: true,
            versioned: true,
            lifecycleRules: [
                {
                    prefix: 'aggregation-perf-runs/',
                    abortIncompleteMultipartUploadAfter: Duration.days(7),
                    noncurrentVersionExpiration: Duration.days(30),
                },
            ],
            removalPolicy: RemovalPolicy.RETAIN,
        });

        // 비용을 낮게 유지하는 개발용 datasource입니다.
        const auroraCluster = new rds.DatabaseCluster(this, 'AuroraPostgresCluster', {
            clusterIdentifier: 'dev-loop-ad-aurora-postgres',
            engine: rds.DatabaseClusterEngine.auroraPostgres({
                version: rds.AuroraPostgresEngineVersion.VER_16_13,
            }),
            writer: rds.ClusterInstance.serverlessV2('writer'),
            serverlessV2MinCapacity: DEV_AURORA_MIN_ACU,
            serverlessV2MaxCapacity: DEV_AURORA_MAX_ACU,
            serverlessV2AutoPauseDuration: Duration.minutes(DEV_AURORA_AUTO_PAUSE_MINUTES),
            defaultDatabaseName: 'loopad',
            vpc,
            vpcSubnets: privateSubnets,
            securityGroups: [dataSourceSecurityGroup],
            backup: {
                retention: Duration.days(1),
            },
            deletionProtection: false,
            removalPolicy: RemovalPolicy.SNAPSHOT,
        });

        const clickHouseInstance = new ec2.Instance(this, 'ClickHouseInstance', {
            vpc,
            vpcSubnets: privateSubnets,
            securityGroup: dataSourceSecurityGroup,
            instanceName: 'dev-loop-ad-clickhouse',
            instanceType: new ec2.InstanceType('t4g.small'),
            machineImage: ec2.MachineImage.latestAmazonLinux2023({
                cpuType: ec2.AmazonLinuxCpuType.ARM_64,
            }),
            blockDevices: [
                {
                    deviceName: '/dev/xvda',
                    volume: ec2.BlockDeviceVolume.ebs(DEV_CLICKHOUSE_VOLUME_GIB, {
                        encrypted: true,
                        volumeType: ec2.EbsDeviceVolumeType.GP3,
                    }),
                },
            ],
            requireImdsv2: true,
        });
        clickHouseInstance.userData.addCommands(
            'set -eux',
            'dnf update -y',
            'dnf install -y docker',
            'systemctl enable --now docker',
            'mkdir -p /var/lib/clickhouse',
            'docker run -d --restart unless-stopped --name clickhouse-server -p 8123:8123 -p 9000:9000 -v /var/lib/clickhouse:/var/lib/clickhouse clickhouse/clickhouse-server:latest',
        );

        const mskCluster = new msk.CfnCluster(this, 'MskCluster', {
            clusterName: 'dev-loop-ad-msk',
            kafkaVersion: '3.6.0',
            numberOfBrokerNodes: DEV_MSK_BROKER_COUNT,
            brokerNodeGroupInfo: {
                clientSubnets: privateSubnets.subnetIds,
                instanceType: 'kafka.t3.small',
                securityGroups: [dataSourceSecurityGroup.securityGroupId],
                storageInfo: {
                    ebsStorageInfo: {
                        volumeSize: DEV_MSK_STORAGE_GIB_PER_BROKER,
                    },
                },
            },
            clientAuthentication: {
                unauthenticated: {
                    enabled: true,
                },
            },
            encryptionInfo: {
                encryptionInTransit: {
                    clientBroker: 'TLS_PLAINTEXT',
                    inCluster: true,
                },
            },
            enhancedMonitoring: 'DEFAULT',
        });

        const mskBootstrapBrokers = new cr.AwsCustomResource(this, 'MskBootstrapBrokers', {
            resourceType: 'Custom::LoopAdMskBootstrapBrokers',
            onUpdate: {
                service: 'kafka',
                action: 'GetBootstrapBrokers',
                parameters: {
                    ClusterArn: mskCluster.attrArn,
                },
                outputPaths: ['BootstrapBrokerString'],
                physicalResourceId: cr.PhysicalResourceId.of('dev-loop-ad-msk-bootstrap-brokers'),
            },
            installLatestAwsSdk: false,
            logGroup: new logs.LogGroup(this, 'MskBootstrapBrokersLogGroup', {
                retention: logs.RetentionDays.THREE_DAYS,
                removalPolicy: RemovalPolicy.DESTROY,
            }),
            policy: cr.AwsCustomResourcePolicy.fromStatements([
                new iam.PolicyStatement({
                    actions: ['kafka:GetBootstrapBrokers'],
                    resources: [mskCluster.attrArn],
                }),
            ]),
        });

        // endpoint contract는 SSM에 둡니다. Task definition은 SSM parameter 이름만 알고,
        // 실제 datasource endpoint는 여기에서 교체할 수 있습니다.
        const [auroraEndpoint, redisEndpoint, clickhouseEndpoint, mskEndpoint] = [
            {
                id: 'AuroraEndpointParameter',
                parameterName: '/loop-ad/dev/aurora/endpoint',
                stringValue: auroraCluster.clusterEndpoint.hostname,
                description: 'Dev Aurora PostgreSQL endpoint contract.',
            },
            {
                id: 'RedisEndpointParameter',
                parameterName: '/loop-ad/dev/redis/endpoint',
                stringValue: 'pending://dev/redis',
                description: 'Dev Redis endpoint contract.',
            },
            {
                id: 'ClickHouseEndpointParameter',
                parameterName: '/loop-ad/dev/clickhouse/endpoint',
                stringValue: cdk.Fn.join('', ['http://', clickHouseInstance.instancePrivateDnsName, ':8123']),
                description: 'Dev ClickHouse endpoint contract.',
            },
            {
                id: 'MskEndpointParameter',
                parameterName: '/loop-ad/dev/msk/bootstrap-brokers',
                stringValue: mskBootstrapBrokers.getResponseField('BootstrapBrokerString'),
                description: 'Dev MSK bootstrap broker contract.',
            },
        ].map((parameter) => new ssm.StringParameter(this, parameter.id, {
            parameterName: parameter.parameterName,
            stringValue: parameter.stringValue,
            description: parameter.description,
        }));

        // ALB는 API 경로를 열고, NLB는 raw event ingestion 경로를 엽니다.
        const alb = new elbv2.ApplicationLoadBalancer(this, 'ApplicationLoadBalancer', {
            vpc,
            internetFacing: true,
            securityGroup: albSecurityGroup,
            vpcSubnets: { subnetGroupName: 'public' },
        });
        const albListener = alb.addListener('HttpListener', {
            port: 80,
            protocol: elbv2.ApplicationProtocol.HTTP,
            open: false,
            defaultAction: elbv2.ListenerAction.fixedResponse(404, {
                contentType: 'text/plain',
                messageBody: 'No loop-ad API route is registered.',
            }),
        });
        const nlb = new elbv2.NetworkLoadBalancer(this, 'NetworkLoadBalancer', {
            vpc,
            internetFacing: true,
            securityGroups: [nlbSecurityGroup],
            vpcSubnets: { subnetGroupName: 'public' },
        });

        // .env에서 받은 public hosted zone을 import합니다.
        // fromHostedZoneAttributes는 synth 때 AWS lookup을 하지 않고 record template만 만듭니다.
        const publicHostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'PublicHostedZone', {
            hostedZoneId: props.publicHostedZone.hostedZoneId,
            zoneName: props.publicHostedZone.domainName,
        });

        for (const dnsRecord of [
            {
                id: 'DevApiDnsRecord',
                recordName: 'api.dev',
                target: route53.RecordTarget.fromAlias(new route53Targets.LoadBalancerTarget(alb)),
            },
            {
                id: 'DevIngestDnsRecord',
                recordName: 'ingest.dev',
                target: route53.RecordTarget.fromAlias(new route53Targets.LoadBalancerTarget(nlb)),
            },
        ] as const) {
            new route53.ARecord(this, dnsRecord.id, {
                zone: publicHostedZone,
                recordName: dnsRecord.recordName,
                target: dnsRecord.target,
            });
        }

        // Event Collector는 NLB 트래픽을 받고 event를 MSK로 발행합니다.
        const eventCollectorTask = new ecs.FargateTaskDefinition(this, 'EventCollectorTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        mskEndpoint.grantRead(eventCollectorTask.taskRole);
        const eventCollectorLogGroup = new logs.LogGroup(this, 'EventCollectorLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const eventCollectorContainer = eventCollectorTask.addContainer('EventCollectorContainer', {
            containerName: 'event-collector',
            image: ecs.ContainerImage.fromEcrRepository(eventCollectorRepository, 'latest'),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'event-collector',
                logGroup: eventCollectorLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'event-collector',
                LOOPAD_RUNTIME: 'go',
                LOOPAD_COMPUTE_TARGET: 'fargate',
                LOOPAD_MSK_ENDPOINT_PARAMETER: mskEndpoint.parameterName,
            },
        });
        eventCollectorContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const eventCollectorService = new ecs.FargateService(this, 'EventCollectorService', {
            cluster,
            taskDefinition: eventCollectorTask,
            serviceName: 'dev-event-collector',
            desiredCount: DEV_SERVICE_DESIRED_TASKS,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'event-collector' },
            healthCheckGracePeriod: Duration.seconds(60),
        });
        eventCollectorService.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization('EventCollectorCpuScaling', {
            targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
        });

        // NLB는 TCP 80 포트를 collector service로 직접 전달합니다.
        const nlbListener = nlb.addListener('EventCollectorListener', {
            port: 80,
            protocol: elbv2.Protocol.TCP,
        });
        nlbListener.addTargets('EventCollectorTargets', {
            targets: [eventCollectorService],
            port: 80,
            protocol: elbv2.Protocol.TCP,
            healthCheck: {
                enabled: true,
                port: '80',
            },
        });

        // Projector는 MSK를 consume하고 가공된 context를 Redis/ClickHouse에 씁니다.
        const projectorTask = new ecs.FargateTaskDefinition(this, 'AdContextProjectorTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        mskEndpoint.grantRead(projectorTask.taskRole);
        redisEndpoint.grantRead(projectorTask.taskRole);
        clickhouseEndpoint.grantRead(projectorTask.taskRole);
        const projectorLogGroup = new logs.LogGroup(this, 'AdContextProjectorLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const projectorContainer = projectorTask.addContainer('AdContextProjectorContainer', {
            containerName: 'ad-context-projector',
            image: ecs.ContainerImage.fromEcrRepository(projectorRepository, 'latest'),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'ad-context-projector',
                logGroup: projectorLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'ad-context-projector',
                LOOPAD_RUNTIME: 'go',
                LOOPAD_COMPUTE_TARGET: 'fargate',
                LOOPAD_MSK_ENDPOINT_PARAMETER: mskEndpoint.parameterName,
                LOOPAD_REDIS_ENDPOINT_PARAMETER: redisEndpoint.parameterName,
                LOOPAD_CLICKHOUSE_ENDPOINT_PARAMETER: clickhouseEndpoint.parameterName,
            },
        });
        projectorContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const projectorService = new ecs.FargateService(this, 'AdContextProjectorService', {
            cluster,
            taskDefinition: projectorTask,
            serviceName: 'dev-ad-context-projector',
            desiredCount: DEV_SERVICE_DESIRED_TASKS,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'ad-context-projector' },
        });
        projectorService.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization('AdContextProjectorCpuScaling', {
            targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
        });

        // Decision API는 ALB를 통해 public 광고 결정 경로를 제공합니다.
        const decisionTask = new ecs.FargateTaskDefinition(this, 'AdDecisionApiTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        redisEndpoint.grantRead(decisionTask.taskRole);
        auroraEndpoint.grantRead(decisionTask.taskRole);
        const decisionLogGroup = new logs.LogGroup(this, 'AdDecisionApiLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const decisionContainer = decisionTask.addContainer('AdDecisionApiContainer', {
            containerName: 'ad-decision-api',
            image: ecs.ContainerImage.fromEcrRepository(decisionRepository, 'latest'),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'ad-decision-api',
                logGroup: decisionLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'ad-decision-api',
                LOOPAD_RUNTIME: 'go',
                LOOPAD_COMPUTE_TARGET: 'fargate',
                LOOPAD_REDIS_ENDPOINT_PARAMETER: redisEndpoint.parameterName,
                LOOPAD_AURORA_ENDPOINT_PARAMETER: auroraEndpoint.parameterName,
            },
        });
        decisionContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const decisionService = new ecs.FargateService(this, 'AdDecisionApiService', {
            cluster,
            taskDefinition: decisionTask,
            serviceName: 'dev-ad-decision-api',
            desiredCount: DEV_SERVICE_DESIRED_TASKS,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'ad-decision-api' },
            healthCheckGracePeriod: Duration.seconds(60),
        });
        decisionService.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization('AdDecisionApiCpuScaling', {
            targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
        });
        albListener.addTargets('AdDecisionApiTargets', {
            targets: [decisionService],
            port: 80,
            protocol: elbv2.ApplicationProtocol.HTTP,
            priority: 20,
            conditions: [elbv2.ListenerCondition.pathPatterns(['/api/ads/*', '/decision/*'])],
            healthCheck: {
                enabled: true,
                path: '/health',
                healthyHttpCodes: '200-399',
            },
        });

        // Dashboard API는 dashboard 경로를 제공하고 Cloud Map으로 Recommendation을 호출합니다.
        const dashboardTask = new ecs.FargateTaskDefinition(this, 'DashboardApiTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        auroraEndpoint.grantRead(dashboardTask.taskRole);
        clickhouseEndpoint.grantRead(dashboardTask.taskRole);
        const dashboardLogGroup = new logs.LogGroup(this, 'DashboardApiLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const dashboardContainer = dashboardTask.addContainer('DashboardApiContainer', {
            containerName: 'dashboard-api',
            image: ecs.ContainerImage.fromEcrRepository(dashboardRepository, 'latest'),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'dashboard-api',
                logGroup: dashboardLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'dashboard-api',
                LOOPAD_RUNTIME: 'go',
                LOOPAD_COMPUTE_TARGET: 'fargate',
                LOOPAD_AURORA_ENDPOINT_PARAMETER: auroraEndpoint.parameterName,
                LOOPAD_CLICKHOUSE_ENDPOINT_PARAMETER: clickhouseEndpoint.parameterName,
                LOOPAD_RECOMMENDATION_URL: 'http://recommendation.dev.loop-ad.local:80',
                LOOPAD_N8N_SECRET_PARAMETER: '/loop-ad/dev/external/n8n/webhook',
                LOOPAD_DISCORD_SECRET_PARAMETER: '/loop-ad/dev/external/discord/webhook',
            },
        });
        dashboardContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const dashboardService = new ecs.FargateService(this, 'DashboardApiService', {
            cluster,
            taskDefinition: dashboardTask,
            serviceName: 'dev-dashboard-api',
            desiredCount: DEV_SERVICE_DESIRED_TASKS,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'dashboard-api' },
            healthCheckGracePeriod: Duration.seconds(60),
        });
        dashboardService.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization('DashboardApiCpuScaling', {
            targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
        });
        albListener.addTargets('DashboardApiTargets', {
            targets: [dashboardService],
            port: 80,
            protocol: elbv2.ApplicationProtocol.HTTP,
            priority: 30,
            conditions: [elbv2.ListenerCondition.pathPatterns(['/api/dashboard/*', '/dashboard/*'])],
            healthCheck: {
                enabled: true,
                path: '/health',
                healthyHttpCodes: '200-399',
            },
        });

        // Recommendation은 private 전용이며 public ALB에 연결하지 않습니다.
        const recommendationTask = new ecs.FargateTaskDefinition(this, 'RecommendationTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        auroraEndpoint.grantRead(recommendationTask.taskRole);
        clickhouseEndpoint.grantRead(recommendationTask.taskRole);
        const recommendationLogGroup = new logs.LogGroup(this, 'RecommendationLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const recommendationContainer = recommendationTask.addContainer('RecommendationContainer', {
            containerName: 'recommendation',
            image: ecs.ContainerImage.fromEcrRepository(recommendationRepository, 'latest'),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'recommendation',
                logGroup: recommendationLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'recommendation',
                LOOPAD_RUNTIME: 'go',
                LOOPAD_COMPUTE_TARGET: 'fargate',
                LOOPAD_AURORA_ENDPOINT_PARAMETER: auroraEndpoint.parameterName,
                LOOPAD_CLICKHOUSE_ENDPOINT_PARAMETER: clickhouseEndpoint.parameterName,
                LOOPAD_OPENAI_SECRET_PARAMETER: '/loop-ad/dev/external/openai/api-key',
            },
        });
        recommendationContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const recommendationService = new ecs.FargateService(this, 'RecommendationService', {
            cluster,
            taskDefinition: recommendationTask,
            serviceName: 'dev-recommendation',
            desiredCount: DEV_SERVICE_DESIRED_TASKS,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'recommendation' },
        });
        recommendationService.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization('RecommendationCpuScaling', {
            targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
        });

        // Aggregation perf는 이 output들을 import해서 같은 VPC 안에 임시 서버를 만듭니다.
        for (const output of [
            {
                id: 'VpcId',
                value: vpc.vpcId,
                exportName: 'loop-ad-dev-vpc-id',
            },
            {
                id: 'VpcAvailabilityZones',
                value: cdk.Fn.join(',', vpc.availabilityZones),
                exportName: 'loop-ad-dev-vpc-availability-zones',
            },
            {
                id: 'PublicSubnetIds',
                value: cdk.Fn.join(',', vpc.publicSubnets.map((subnet) => subnet.subnetId)),
                exportName: 'loop-ad-dev-public-subnet-ids',
            },
            {
                id: 'PublicSubnetRouteTableIds',
                value: cdk.Fn.join(',', vpc.publicSubnets.map((subnet) => subnet.routeTable.routeTableId)),
                exportName: 'loop-ad-dev-public-subnet-route-table-ids',
            },
            {
                id: 'PrivateSubnetIds',
                value: cdk.Fn.join(',', privateSubnets.subnets.map((subnet) => subnet.subnetId)),
                exportName: 'loop-ad-dev-private-subnet-ids',
            },
            {
                id: 'PrivateSubnetRouteTableIds',
                value: cdk.Fn.join(',', privateSubnets.subnets.map((subnet) => subnet.routeTable.routeTableId)),
                exportName: 'loop-ad-dev-private-subnet-route-table-ids',
            },
            {
                id: 'AggregationPerfResultsBucketName',
                value: aggregationPerfResultsBucket.bucketName,
                exportName: 'loop-ad-aggregation-perf-results-bucket-name',
            },
        ] as const) {
            new cdk.CfnOutput(this, output.id, {
                value: output.value,
                exportName: output.exportName,
            });
        }
    }
}

// 집계 경로 전용 임시 성능 테스트 스택입니다. dev VPC를 import하지만 자체
// cluster, load balancer, service, aggregation perf endpoint contract를 따로 만듭니다.
export class LoopAdAggregationPerfStack extends Stack {
    public constructor(scope: Construct, id: string, props: LoopAdAggregationPerfStackProps) {
        super(scope, id, props);

        // 이 스택은 전체 서비스가 아니라 집계 경로만 검증합니다.
        // Dev가 소유한 VPC/ECR/results bucket은 재사용하고, 테스트 중 필요한
        // ECS capacity, ClickHouse, MSK, NLB, SSM contract만 임시로 만듭니다.

        // Dev에서 VPC 형태만 import합니다. aggregation perf server는 분리하면서
        // 별도 VPC와 subnet layout은 만들지 않습니다.
        const availabilityZones = cdk.Fn.importListValue('loop-ad-dev-vpc-availability-zones', 2);
        const publicSubnetIds = cdk.Fn.importListValue('loop-ad-dev-public-subnet-ids', 2);
        const vpc = ec2.Vpc.fromVpcAttributes(this, 'DevVpc', {
            vpcId: cdk.Fn.importValue('loop-ad-dev-vpc-id'),
            availabilityZones,
            publicSubnetIds: [],
            privateSubnetIds: [],
            isolatedSubnetIds: [],
        });
        // public subnet만 다시 감싸서 NAT Gateway 없이 테스트 리소스를 올립니다.
        // private subnet import를 피해야 perf stack이 dev NAT 경로에 기대지 않습니다.
        const publicSubnets = [
            {
                id: 'PublicSubnet1',
                subnetId: publicSubnetIds[0],
                availabilityZone: availabilityZones[0],
            },
            {
                id: 'PublicSubnet2',
                subnetId: publicSubnetIds[1],
                availabilityZone: availabilityZones[1],
            },
        ].map((subnet) => ec2.Subnet.fromSubnetAttributes(this, subnet.id, {
            subnetId: subnet.subnetId,
            availabilityZone: subnet.availabilityZone,
        }));
        // 결과 bucket은 Dev 스택이 소유하고 retain합니다.
        // perf stack을 destroy해도 분석 결과를 잃지 않도록 이름만 import합니다.
        const aggregationPerfResultsBucket = s3.Bucket.fromBucketName(
            this,
            'AggregationPerfResultsBucket',
            cdk.Fn.importValue('loop-ad-aggregation-perf-results-bucket-name'),
        );
        // 별도 ECS cluster를 만들어 테스트 capacity와 metrics가 dev 서비스와 섞이지 않게 합니다.
        const cluster = new ecs.Cluster(this, 'Cluster', {
            vpc,
            clusterName: 'aggregation-perf-loop-ad-cluster',
            containerInsightsV2: ecs.ContainerInsights.ENABLED,
            defaultCloudMapNamespace: {
                name: 'aggregation-perf.loop-ad.local',
            },
        });

        // Security group은 역할별로 세 개만 둡니다.
        // public ingress는 NLB에만 열고, ECS server와 datasource는 SG 참조로만 서로 통신합니다.
        const nlbSecurityGroup = new ec2.SecurityGroup(this, 'NlbSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Aggregation perf NLB event ingestion ingress only.',
        });
        const serverSecurityGroup = new ec2.SecurityGroup(this, 'ServerSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Aggregation perf ECS server SG shared by test services.',
        });
        const dataSourceSecurityGroup = new ec2.SecurityGroup(this, 'DataSourceSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Aggregation perf datasource SG shared by internal data endpoints.',
        });

        // 인터넷은 aggregation perf NLB까지만 접근할 수 있고 이후 트래픽은 내부 통신입니다.
        nlbSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public ingest to aggregation perf NLB.');
        nlbSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'NLB may reach aggregation perf servers.');
        serverSecurityGroup.addIngressRule(nlbSecurityGroup, ec2.Port.allTraffic(), 'NLB may enter aggregation perf servers.');
        serverSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Aggregation perf servers may call each other.');
        serverSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Aggregation perf servers may call each other.');
        serverSecurityGroup.addEgressRule(dataSourceSecurityGroup, ec2.Port.allTraffic(), 'Aggregation perf servers may reach internal datasources.');
        dataSourceSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Aggregation perf servers may enter internal datasources.');
        dataSourceSecurityGroup.addIngressRule(dataSourceSecurityGroup, ec2.Port.allTraffic(), 'Aggregation perf datasources may call each other.');
        dataSourceSecurityGroup.addEgressRule(dataSourceSecurityGroup, ec2.Port.allTraffic(), 'Aggregation perf datasources may call each other.');
        serverSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Aggregation perf servers may use public HTTPS egress.');
        dataSourceSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Aggregation perf datasources may use public HTTPS egress.');

        // Aggregation perf는 20k RPS/1KB 요청을 기본 목표로 두되, max capacity로 여유를 남깁니다.
        // 이 stack은 필요한 테스트 시간에만 올렸다가 destroy하는 ephemeral benchmark 환경입니다.
        // desired/min/max를 명시해 테스트 시작 시 기본 capacity를 바로 확보하고 상한도 코드로 고정합니다.
        const autoScalingGroup = new autoscaling.AutoScalingGroup(this, 'EcsEc2AutoScalingGroup', {
            vpc,
            vpcSubnets: { subnets: publicSubnets },
            securityGroup: serverSecurityGroup,
            instanceType: new ec2.InstanceType(AGGREGATION_PERF_EC2_INSTANCE_TYPE),
            machineImage: ecs.EcsOptimizedImage.amazonLinux2023(ecs.AmiHardwareType.ARM),
            minCapacity: AGGREGATION_PERF_EC2_MIN_INSTANCES,
            desiredCapacity: AGGREGATION_PERF_EC2_DESIRED_INSTANCES,
            maxCapacity: AGGREGATION_PERF_EC2_MAX_INSTANCES,
            instanceMonitoring: autoscaling.Monitoring.DETAILED,
            blockDevices: [
                {
                    deviceName: '/dev/xvda',
                    volume: autoscaling.BlockDeviceVolume.ebs(100, {
                        encrypted: true,
                        volumeType: autoscaling.EbsDeviceVolumeType.GP3,
                    }),
                },
            ],
        });
        const capacityProvider = new ecs.AsgCapacityProvider(this, 'EcsEc2CapacityProvider', {
            autoScalingGroup,
            enableManagedScaling: true,
            enableManagedTerminationProtection: false,
        });
        cluster.addAsgCapacityProvider(capacityProvider);

        // Aggregation perf는 storage를 만들지 않고 dev가 소유한 image repository를 재사용합니다.
        const [eventCollectorRepository, projectorRepository] = [
            { id: 'EventCollectorRepository', repositoryName: 'loop-ad/event-collector' },
            { id: 'AdContextProjectorRepository', repositoryName: 'loop-ad/ad-context-projector' },
        ].map((repository) => ecr.Repository.fromRepositoryName(
            this,
            repository.id,
            repository.repositoryName,
        ));

        // ClickHouse는 집계 결과 write path의 sink입니다.
        // Dev datasource와 분리해서 perf run의 write pattern이 개발 환경 데이터를 흔들지 않게 합니다.
        const aggregationPerfClickHouseInstance = new ec2.Instance(this, 'ClickHouseInstance', {
            vpc,
            vpcSubnets: { subnets: publicSubnets },
            securityGroup: dataSourceSecurityGroup,
            instanceName: 'aggregation-perf-loop-ad-clickhouse',
            instanceType: new ec2.InstanceType(AGGREGATION_PERF_CLICKHOUSE_INSTANCE_TYPE),
            machineImage: ec2.MachineImage.latestAmazonLinux2023({
                cpuType: ec2.AmazonLinuxCpuType.ARM_64,
            }),
            blockDevices: [
                {
                    deviceName: '/dev/xvda',
                    volume: ec2.BlockDeviceVolume.ebs(AGGREGATION_PERF_CLICKHOUSE_VOLUME_GIB, {
                        encrypted: true,
                        iops: AGGREGATION_PERF_CLICKHOUSE_VOLUME_IOPS,
                        volumeType: ec2.EbsDeviceVolumeType.GP3,
                    }),
                },
            ],
            requireImdsv2: true,
        });
        // User data는 Docker 기반 ClickHouse bootstrap만 수행합니다.
        // schema 생성, tuning, 결과 적재 방식은 애플리케이션/perf runner 쪽에서 검증합니다.
        aggregationPerfClickHouseInstance.userData.addCommands(
            'set -eux',
            'dnf update -y',
            'dnf install -y docker',
            'systemctl enable --now docker',
            'mkdir -p /var/lib/clickhouse',
            'docker run -d --restart unless-stopped --name clickhouse-server -p 8123:8123 -p 9000:9000 -v /var/lib/clickhouse:/var/lib/clickhouse clickhouse/clickhouse-server:latest',
        );

        // MSK는 collector가 쓴 aggregation event를 projector가 읽는 전용 stream입니다.
        // dev MSK와 분리해 benchmark partition/throughput 설정을 독립적으로 바꿀 수 있게 합니다.
        const aggregationPerfMskConfiguration = new msk.CfnConfiguration(this, 'MskConfiguration', {
            name: 'aggregation-perf-loop-ad-msk-throughput',
            kafkaVersionsList: [AGGREGATION_PERF_MSK_KAFKA_VERSION],
            description: 'Cost-aware MSK configuration for loop-ad aggregation path 20k RPS tests.',
            serverProperties: [
                'auto.create.topics.enable=false',
                'num.io.threads=8',
                'num.network.threads=4',
                'num.partitions=128',
                'default.replication.factor=2',
                'min.insync.replicas=1',
                'log.retention.hours=6',
            ].join('\n'),
        });
        const aggregationPerfMskCluster = new msk.CfnCluster(this, 'MskCluster', {
            clusterName: 'aggregation-perf-loop-ad-msk',
            kafkaVersion: AGGREGATION_PERF_MSK_KAFKA_VERSION,
            numberOfBrokerNodes: AGGREGATION_PERF_MSK_BROKER_COUNT,
            brokerNodeGroupInfo: {
                clientSubnets: publicSubnets.map((subnet) => subnet.subnetId),
                instanceType: AGGREGATION_PERF_MSK_BROKER_INSTANCE_TYPE,
                securityGroups: [dataSourceSecurityGroup.securityGroupId],
                storageInfo: {
                    ebsStorageInfo: {
                        provisionedThroughput: {
                            enabled: true,
                            volumeThroughput: AGGREGATION_PERF_MSK_VOLUME_THROUGHPUT,
                        },
                        volumeSize: AGGREGATION_PERF_MSK_STORAGE_GIB_PER_BROKER,
                    },
                },
            },
            clientAuthentication: {
                unauthenticated: {
                    enabled: true,
                },
            },
            configurationInfo: {
                arn: aggregationPerfMskConfiguration.attrArn,
                revision: aggregationPerfMskConfiguration.attrLatestRevisionRevision,
            },
            encryptionInfo: {
                encryptionInTransit: {
                    clientBroker: 'TLS_PLAINTEXT',
                    inCluster: true,
                },
            },
            enhancedMonitoring: 'PER_BROKER',
        });
        // topic을 명시적으로 생성해 auto-create topic 설정에 기대지 않습니다.
        // partition/replication factor가 CDK diff와 테스트 expectation에 드러나게 하기 위함입니다.
        const aggregationEventsTopic = new msk.CfnTopic(this, 'AggregationEventsTopic', {
            clusterArn: aggregationPerfMskCluster.attrArn,
            topicName: 'aggregation-events',
            partitionCount: AGGREGATION_PERF_MSK_TOPIC_PARTITIONS,
            replicationFactor: AGGREGATION_PERF_MSK_TOPIC_REPLICATION_FACTOR,
        });
        aggregationEventsTopic.node.addDependency(aggregationPerfMskCluster);

        // MSK bootstrap broker 문자열은 CfnCluster attribute로 직접 얻을 수 없어 배포 중 조회합니다.
        // 애플리케이션 task는 이 값을 직접 알지 않고 SSM parameter name만 env로 받습니다.
        const aggregationPerfMskBootstrapBrokers = new cr.AwsCustomResource(this, 'MskBootstrapBrokers', {
            resourceType: 'Custom::LoopAdAggregationPerfMskBootstrapBrokers',
            onUpdate: {
                service: 'kafka',
                action: 'GetBootstrapBrokers',
                parameters: {
                    ClusterArn: aggregationPerfMskCluster.attrArn,
                },
                outputPaths: ['BootstrapBrokerString'],
                physicalResourceId: cr.PhysicalResourceId.of('aggregation-perf-loop-ad-msk-bootstrap-brokers'),
            },
            installLatestAwsSdk: false,
            logGroup: new logs.LogGroup(this, 'MskBootstrapBrokersLogGroup', {
                retention: logs.RetentionDays.THREE_DAYS,
                removalPolicy: RemovalPolicy.DESTROY,
            }),
            policy: cr.AwsCustomResourcePolicy.fromStatements([
                new iam.PolicyStatement({
                    actions: ['kafka:GetBootstrapBrokers'],
                    resources: [aggregationPerfMskCluster.attrArn],
                }),
            ]),
        });

        // Aggregation perf endpoint contract는 집계 경로 성능 테스트 전용 data path로 분리합니다.
        const [redisEndpoint, clickhouseEndpoint, mskEndpoint, resultsBucketName] = [
            {
                id: 'RedisEndpointParameter',
                parameterName: '/loop-ad/aggregation-perf/redis/endpoint',
                stringValue: 'pending://aggregation-perf/redis',
                description: 'Aggregation perf Redis endpoint contract.',
            },
            {
                id: 'ClickHouseEndpointParameter',
                parameterName: '/loop-ad/aggregation-perf/clickhouse/endpoint',
                stringValue: cdk.Fn.join('', ['http://', aggregationPerfClickHouseInstance.instancePrivateDnsName, ':8123']),
                description: 'Aggregation perf ClickHouse endpoint contract.',
            },
            {
                id: 'MskEndpointParameter',
                parameterName: '/loop-ad/aggregation-perf/msk/bootstrap-brokers',
                stringValue: aggregationPerfMskBootstrapBrokers.getResponseField('BootstrapBrokerString'),
                description: 'Aggregation perf MSK bootstrap broker contract.',
            },
            {
                id: 'ResultsBucketNameParameter',
                parameterName: '/loop-ad/aggregation-perf/results-bucket-name',
                stringValue: aggregationPerfResultsBucket.bucketName,
                description: 'Aggregation perf analysis result S3 backup bucket contract.',
            },
        ].map((parameter) => new ssm.StringParameter(this, parameter.id, {
            parameterName: parameter.parameterName,
            stringValue: parameter.stringValue,
            description: parameter.description,
        }));
        // perf task가 분석 결과를 남길 수 있게 하되, 쓰기 권한은 결과 prefix로만 제한합니다.
        const grantAggregationPerfResultsUpload = (role: iam.IGrantable) => {
            role.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
                actions: [
                    's3:AbortMultipartUpload',
                    's3:PutObject',
                    's3:PutObjectTagging',
                ],
                resources: [
                    aggregationPerfResultsBucket.arnForObjects('aggregation-perf-runs/*'),
                ],
            }));
        };

        // Aggregation perf는 집계 경로만 노출합니다. ALB, dashboard, API, frontend는 없습니다.
        const nlb = new elbv2.NetworkLoadBalancer(this, 'NetworkLoadBalancer', {
            vpc,
            internetFacing: true,
            securityGroups: [nlbSecurityGroup],
            vpcSubnets: { subnets: publicSubnets },
        });

        // Aggregation perf도 같은 public hosted zone에 임시 ingest record만 만듭니다.
        const publicHostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'PublicHostedZone', {
            hostedZoneId: props.publicHostedZone.hostedZoneId,
            zoneName: props.publicHostedZone.domainName,
        });
        new route53.ARecord(this, 'AggregationPerfIngestDnsRecord', {
            zone: publicHostedZone,
            recordName: 'ingest.aggregation-perf',
            target: route53.RecordTarget.fromAlias(new route53Targets.LoadBalancerTarget(nlb)),
        });

        // Event Collector는 이 스택에서 유일하게 외부 요청을 받는 서비스입니다.
        // bridge networking과 dynamic host port로 한 EC2에 여러 collector task를 밀도 있게 배치합니다.
        const eventCollectorTask = new ecs.Ec2TaskDefinition(this, 'EventCollectorTaskDefinition', {
            networkMode: ecs.NetworkMode.BRIDGE,
        });
        mskEndpoint.grantRead(eventCollectorTask.taskRole);
        resultsBucketName.grantRead(eventCollectorTask.taskRole);
        grantAggregationPerfResultsUpload(eventCollectorTask.taskRole);
        const eventCollectorLogGroup = new logs.LogGroup(this, 'EventCollectorLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const eventCollectorContainer = eventCollectorTask.addContainer('EventCollectorContainer', {
            containerName: 'event-collector',
            image: ecs.ContainerImage.fromEcrRepository(eventCollectorRepository, 'latest'),
            cpu: 1024,
            memoryReservationMiB: 2048,
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'event-collector',
                logGroup: eventCollectorLogGroup,
            }),
            environment: {
                // app은 endpoint 값을 하드코딩하지 않고 SSM parameter name을 받아 런타임에 조회합니다.
                LOOPAD_ENV: 'aggregation-perf',
                LOOPAD_SERVICE_ID: 'event-collector',
                LOOPAD_RUNTIME: 'go',
                LOOPAD_COMPUTE_TARGET: 'ecs-ec2',
                LOOPAD_AGGREGATION_PERF_TARGET_RPS: String(LOOP_AD_AGGREGATION_PERF_TARGET_RPS),
                LOOPAD_MSK_TOPIC: 'aggregation-events',
                LOOPAD_MSK_TOPIC_PARTITIONS: String(AGGREGATION_PERF_MSK_TOPIC_PARTITIONS),
                LOOPAD_MSK_ENDPOINT_PARAMETER: mskEndpoint.parameterName,
                LOOPAD_AGGREGATION_PERF_RESULTS_BUCKET_PARAMETER: resultsBucketName.parameterName,
                LOOPAD_AGGREGATION_PERF_RESULTS_S3_PREFIX: 'aggregation-perf-runs/',
            },
        });
        eventCollectorContainer.addPortMappings({ containerPort: 80, hostPort: 0, protocol: ecs.Protocol.TCP });
        // service autoscaling은 task count만 조정합니다.
        // EC2 capacity는 위 ASG capacity provider가 받쳐주며, placement는 AZ와 instance에 분산합니다.
        const eventCollectorService = new ecs.Ec2Service(this, 'EventCollectorService', {
            cluster,
            taskDefinition: eventCollectorTask,
            serviceName: 'aggregation-perf-event-collector',
            desiredCount: AGGREGATION_PERF_EVENT_COLLECTOR_DESIRED_TASKS,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            placementStrategies: [
                ecs.PlacementStrategy.spreadAcross(ecs.BuiltInAttributes.AVAILABILITY_ZONE),
                ecs.PlacementStrategy.spreadAcrossInstances(),
            ],
            capacityProviderStrategies: [
                {
                    capacityProvider: capacityProvider.capacityProviderName,
                    weight: 1,
                },
            ],
        });
        eventCollectorService.autoScaleTaskCount({
            minCapacity: AGGREGATION_PERF_EVENT_COLLECTOR_MIN_TASKS,
            maxCapacity: AGGREGATION_PERF_EVENT_COLLECTOR_MAX_TASKS,
        }).scaleOnCpuUtilization('EventCollectorCpuScaling', {
            targetUtilizationPercent: AGGREGATION_PERF_CPU_SCALE_TARGET_PERCENT,
        });

        const nlbListener = nlb.addListener('EventCollectorListener', {
            port: 80,
            protocol: elbv2.Protocol.TCP,
        });
        // ECS service를 target으로 붙이면 dynamic host port를 NLB target group에 자동 등록합니다.
        nlbListener.addTargets('EventCollectorTargets', {
            targets: [eventCollectorService],
            port: 80,
            protocol: elbv2.Protocol.TCP,
            healthCheck: {
                enabled: true,
                port: '80',
            },
        });

        // Projector는 같은 aggregation topic을 읽고 ClickHouse에 결과를 쓰는 마지막 단계입니다.
        // Collector와 분리해 read/write 병목을 각각 CloudWatch/ECS metrics로 볼 수 있게 합니다.
        const projectorTask = new ecs.Ec2TaskDefinition(this, 'AdContextProjectorTaskDefinition', {
            networkMode: ecs.NetworkMode.BRIDGE,
        });
        mskEndpoint.grantRead(projectorTask.taskRole);
        redisEndpoint.grantRead(projectorTask.taskRole);
        clickhouseEndpoint.grantRead(projectorTask.taskRole);
        resultsBucketName.grantRead(projectorTask.taskRole);
        grantAggregationPerfResultsUpload(projectorTask.taskRole);
        const projectorLogGroup = new logs.LogGroup(this, 'AdContextProjectorLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const projectorContainer = projectorTask.addContainer('AdContextProjectorContainer', {
            containerName: 'ad-context-projector',
            image: ecs.ContainerImage.fromEcrRepository(projectorRepository, 'latest'),
            cpu: 1024,
            memoryReservationMiB: 2048,
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'ad-context-projector',
                logGroup: projectorLogGroup,
            }),
            environment: {
                // projector도 endpoint 실값 대신 contract parameter name을 받아 datasource를 찾습니다.
                LOOPAD_ENV: 'aggregation-perf',
                LOOPAD_SERVICE_ID: 'ad-context-projector',
                LOOPAD_RUNTIME: 'go',
                LOOPAD_COMPUTE_TARGET: 'ecs-ec2',
                LOOPAD_AGGREGATION_PERF_TARGET_RPS: String(LOOP_AD_AGGREGATION_PERF_TARGET_RPS),
                LOOPAD_MSK_TOPIC: 'aggregation-events',
                LOOPAD_MSK_TOPIC_PARTITIONS: String(AGGREGATION_PERF_MSK_TOPIC_PARTITIONS),
                LOOPAD_MSK_ENDPOINT_PARAMETER: mskEndpoint.parameterName,
                LOOPAD_REDIS_ENDPOINT_PARAMETER: redisEndpoint.parameterName,
                LOOPAD_CLICKHOUSE_ENDPOINT_PARAMETER: clickhouseEndpoint.parameterName,
                LOOPAD_AGGREGATION_PERF_RESULTS_BUCKET_PARAMETER: resultsBucketName.parameterName,
                LOOPAD_AGGREGATION_PERF_RESULTS_S3_PREFIX: 'aggregation-perf-runs/',
            },
        });
        projectorContainer.addPortMappings({ containerPort: 80, hostPort: 0, protocol: ecs.Protocol.TCP });
        // projector는 public ingress가 필요 없으므로 NLB에는 붙이지 않습니다.
        // 같은 capacity provider 안에서 collector와 함께 스케줄되어 실제 집계 경로만 검증합니다.
        const projectorService = new ecs.Ec2Service(this, 'AdContextProjectorService', {
            cluster,
            taskDefinition: projectorTask,
            serviceName: 'aggregation-perf-ad-context-projector',
            desiredCount: AGGREGATION_PERF_PROJECTOR_DESIRED_TASKS,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            placementStrategies: [
                ecs.PlacementStrategy.spreadAcross(ecs.BuiltInAttributes.AVAILABILITY_ZONE),
                ecs.PlacementStrategy.spreadAcrossInstances(),
            ],
            capacityProviderStrategies: [
                {
                    capacityProvider: capacityProvider.capacityProviderName,
                    weight: 1,
                },
            ],
        });
        projectorService.autoScaleTaskCount({
            minCapacity: AGGREGATION_PERF_PROJECTOR_MIN_TASKS,
            maxCapacity: AGGREGATION_PERF_PROJECTOR_MAX_TASKS,
        }).scaleOnCpuUtilization('AdContextProjectorCpuScaling', {
            targetUtilizationPercent: AGGREGATION_PERF_CPU_SCALE_TARGET_PERCENT,
        });
    }
}

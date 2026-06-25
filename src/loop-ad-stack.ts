import { Duration, RemovalPolicy, Stack, type StackProps } from 'aws-cdk-lib';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';

export const LOOP_AD_REGION = 'ap-northeast-2';

export class LoopAdDevStack extends Stack {
    public constructor(scope: Construct, id: string, props: StackProps & { readonly enableNatGateway: boolean }) {
        super(scope, id, props);

        const vpc = new ec2.Vpc(this, 'Vpc', {
            vpcName: 'dev-loop-ad-vpc',
            maxAzs: 2,
            natGateways: props.enableNatGateway ? 1 : 0,
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

        const privateSubnets = vpc.selectSubnets({ subnetGroupName: 'private' });

        const endpointSecurityGroup = new ec2.SecurityGroup(this, 'VpcEndpointSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Private AWS API endpoint SG.',
        });

        vpc.addGatewayEndpoint('S3GatewayEndpoint', {
            service: ec2.GatewayVpcEndpointAwsService.S3,
            subnets: [privateSubnets],
        });

        vpc.addInterfaceEndpoint('EcrApiEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.ECR,
            securityGroups: [endpointSecurityGroup],
            subnets: privateSubnets,
        });
        vpc.addInterfaceEndpoint('EcrDockerEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            securityGroups: [endpointSecurityGroup],
            subnets: privateSubnets,
        });
        vpc.addInterfaceEndpoint('CloudWatchLogsEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            securityGroups: [endpointSecurityGroup],
            subnets: privateSubnets,
        });
        vpc.addInterfaceEndpoint('SecretsManagerEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            securityGroups: [endpointSecurityGroup],
            subnets: privateSubnets,
        });
        vpc.addInterfaceEndpoint('SsmEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.SSM,
            securityGroups: [endpointSecurityGroup],
            subnets: privateSubnets,
        });
        vpc.addInterfaceEndpoint('EcsEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.ECS,
            securityGroups: [endpointSecurityGroup],
            subnets: privateSubnets,
        });
        vpc.addInterfaceEndpoint('EcsAgentEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.ECS_AGENT,
            securityGroups: [endpointSecurityGroup],
            subnets: privateSubnets,
        });
        vpc.addInterfaceEndpoint('EcsTelemetryEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.ECS_TELEMETRY,
            securityGroups: [endpointSecurityGroup],
            subnets: privateSubnets,
        });

        const cluster = new ecs.Cluster(this, 'Cluster', {
            vpc,
            clusterName: 'dev-loop-ad-cluster',
            containerInsightsV2: ecs.ContainerInsights.ENABLED,
            defaultCloudMapNamespace: {
                name: 'dev.loop-ad.local',
            },
        });

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

        albSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public HTTP to dev ALB.');
        nlbSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public ingest to dev NLB.');
        albSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'ALB may reach dev servers.');
        nlbSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'NLB may reach dev servers.');
        serverSecurityGroup.addIngressRule(albSecurityGroup, ec2.Port.allTraffic(), 'ALB may enter dev servers.');
        serverSecurityGroup.addIngressRule(nlbSecurityGroup, ec2.Port.allTraffic(), 'NLB may enter dev servers.');
        serverSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may call each other.');
        serverSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may call each other.');
        serverSecurityGroup.addEgressRule(dataSourceSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may reach internal datasources.');
        dataSourceSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may enter internal datasources.');
        serverSecurityGroup.addEgressRule(endpointSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may call private AWS APIs.');
        endpointSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may use VPC endpoints.');

        if (props.enableNatGateway) {
            serverSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Dev servers may use external HTTPS through NAT.');
        }

        const eventCollectorRepository = new ecr.Repository(this, 'EventCollectorRepository', {
            repositoryName: 'loopad/event-collector',
            imageScanOnPush: true,
            lifecycleRules: [{ maxImageCount: 20 }],
            removalPolicy: RemovalPolicy.RETAIN,
        });
        const projectorRepository = new ecr.Repository(this, 'AdContextProjectorRepository', {
            repositoryName: 'loopad/ad-context-projector',
            imageScanOnPush: true,
            lifecycleRules: [{ maxImageCount: 20 }],
            removalPolicy: RemovalPolicy.RETAIN,
        });
        const decisionRepository = new ecr.Repository(this, 'AdDecisionApiRepository', {
            repositoryName: 'loopad/ad-decision-api',
            imageScanOnPush: true,
            lifecycleRules: [{ maxImageCount: 20 }],
            removalPolicy: RemovalPolicy.RETAIN,
        });
        const dashboardRepository = new ecr.Repository(this, 'DashboardApiRepository', {
            repositoryName: 'loopad/dashboard-api',
            imageScanOnPush: true,
            lifecycleRules: [{ maxImageCount: 20 }],
            removalPolicy: RemovalPolicy.RETAIN,
        });
        const recommendationRepository = new ecr.Repository(this, 'RecommendationRepository', {
            repositoryName: 'loopad/recommendation',
            imageScanOnPush: true,
            lifecycleRules: [{ maxImageCount: 20 }],
            removalPolicy: RemovalPolicy.RETAIN,
        });

        const auroraEndpoint = new ssm.StringParameter(this, 'AuroraEndpointParameter', {
            parameterName: '/loop-ad/dev/aurora/endpoint',
            stringValue: 'pending://dev/aurora',
            description: 'Dev Aurora PostgreSQL endpoint contract.',
        });
        const redisEndpoint = new ssm.StringParameter(this, 'RedisEndpointParameter', {
            parameterName: '/loop-ad/dev/redis/endpoint',
            stringValue: 'pending://dev/redis',
            description: 'Dev Redis endpoint contract.',
        });
        const clickhouseEndpoint = new ssm.StringParameter(this, 'ClickHouseEndpointParameter', {
            parameterName: '/loop-ad/dev/clickhouse/endpoint',
            stringValue: 'pending://dev/clickhouse',
            description: 'Dev ClickHouse endpoint contract.',
        });
        const mskEndpoint = new ssm.StringParameter(this, 'MskEndpointParameter', {
            parameterName: '/loop-ad/dev/msk/bootstrap-brokers',
            stringValue: 'pending://dev/msk',
            description: 'Dev MSK bootstrap broker contract.',
        });

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
            desiredCount: 1,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'event-collector' },
            healthCheckGracePeriod: Duration.seconds(60),
        });
        eventCollectorService.autoScaleTaskCount({ minCapacity: 0, maxCapacity: 2 }).scaleOnCpuUtilization('EventCollectorCpuScaling', {
            targetUtilizationPercent: 70,
        });

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
            desiredCount: 1,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'ad-context-projector' },
        });
        projectorService.autoScaleTaskCount({ minCapacity: 0, maxCapacity: 2 }).scaleOnCpuUtilization('AdContextProjectorCpuScaling', {
            targetUtilizationPercent: 70,
        });

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
            desiredCount: 1,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'ad-decision-api' },
            healthCheckGracePeriod: Duration.seconds(60),
        });
        decisionService.autoScaleTaskCount({ minCapacity: 0, maxCapacity: 2 }).scaleOnCpuUtilization('AdDecisionApiCpuScaling', {
            targetUtilizationPercent: 70,
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
            desiredCount: 1,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'dashboard-api' },
            healthCheckGracePeriod: Duration.seconds(60),
        });
        dashboardService.autoScaleTaskCount({ minCapacity: 0, maxCapacity: 2 }).scaleOnCpuUtilization('DashboardApiCpuScaling', {
            targetUtilizationPercent: 70,
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
            desiredCount: 1,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'recommendation' },
        });
        recommendationService.autoScaleTaskCount({ minCapacity: 0, maxCapacity: 2 }).scaleOnCpuUtilization('RecommendationCpuScaling', {
            targetUtilizationPercent: 70,
        });

        new cdk.CfnOutput(this, 'VpcId', {
            value: vpc.vpcId,
            exportName: 'loop-ad-dev-vpc-id',
        });
        new cdk.CfnOutput(this, 'VpcAvailabilityZones', {
            value: cdk.Fn.join(',', vpc.availabilityZones),
            exportName: 'loop-ad-dev-vpc-availability-zones',
        });
        new cdk.CfnOutput(this, 'PublicSubnetIds', {
            value: cdk.Fn.join(',', vpc.publicSubnets.map((subnet) => subnet.subnetId)),
            exportName: 'loop-ad-dev-public-subnet-ids',
        });
        new cdk.CfnOutput(this, 'PublicSubnetRouteTableIds', {
            value: cdk.Fn.join(',', vpc.publicSubnets.map((subnet) => subnet.routeTable.routeTableId)),
            exportName: 'loop-ad-dev-public-subnet-route-table-ids',
        });
        new cdk.CfnOutput(this, 'PrivateSubnetIds', {
            value: cdk.Fn.join(',', privateSubnets.subnets.map((subnet) => subnet.subnetId)),
            exportName: 'loop-ad-dev-private-subnet-ids',
        });
        new cdk.CfnOutput(this, 'PrivateSubnetRouteTableIds', {
            value: cdk.Fn.join(',', privateSubnets.subnets.map((subnet) => subnet.routeTable.routeTableId)),
            exportName: 'loop-ad-dev-private-subnet-route-table-ids',
        });
        new cdk.CfnOutput(this, 'EndpointSecurityGroupId', {
            value: endpointSecurityGroup.securityGroupId,
            exportName: 'loop-ad-dev-vpc-endpoint-security-group-id',
        });
    }
}

export class LoopAdPerfStack extends Stack {
    public constructor(scope: Construct, id: string, props: StackProps) {
        super(scope, id, props);

        const availabilityZones = cdk.Fn.importListValue('loop-ad-dev-vpc-availability-zones', 2);
        const publicSubnetIds = cdk.Fn.importListValue('loop-ad-dev-public-subnet-ids', 2);
        const publicSubnetRouteTableIds = cdk.Fn.importListValue('loop-ad-dev-public-subnet-route-table-ids', 2);
        const privateSubnetIds = cdk.Fn.importListValue('loop-ad-dev-private-subnet-ids', 2);
        const privateSubnetRouteTableIds = cdk.Fn.importListValue('loop-ad-dev-private-subnet-route-table-ids', 2);
        const vpc = ec2.Vpc.fromVpcAttributes(this, 'DevVpc', {
            vpcId: cdk.Fn.importValue('loop-ad-dev-vpc-id'),
            availabilityZones,
            publicSubnetIds: [],
            privateSubnetIds: [],
            isolatedSubnetIds: [],
        });
        const publicSubnets = [
            ec2.Subnet.fromSubnetAttributes(this, 'PublicSubnet1', {
                subnetId: publicSubnetIds[0],
                availabilityZone: availabilityZones[0],
                routeTableId: publicSubnetRouteTableIds[0],
            }),
            ec2.Subnet.fromSubnetAttributes(this, 'PublicSubnet2', {
                subnetId: publicSubnetIds[1],
                availabilityZone: availabilityZones[1],
                routeTableId: publicSubnetRouteTableIds[1],
            }),
        ];
        const privateSubnets = [
            ec2.Subnet.fromSubnetAttributes(this, 'PrivateSubnet1', {
                subnetId: privateSubnetIds[0],
                availabilityZone: availabilityZones[0],
                routeTableId: privateSubnetRouteTableIds[0],
            }),
            ec2.Subnet.fromSubnetAttributes(this, 'PrivateSubnet2', {
                subnetId: privateSubnetIds[1],
                availabilityZone: availabilityZones[1],
                routeTableId: privateSubnetRouteTableIds[1],
            }),
        ];

        const endpointSecurityGroup = ec2.SecurityGroup.fromSecurityGroupId(
            this,
            'VpcEndpointSecurityGroup',
            cdk.Fn.importValue('loop-ad-dev-vpc-endpoint-security-group-id'),
            {
                mutable: true,
            },
        );

        const cluster = new ecs.Cluster(this, 'Cluster', {
            vpc,
            clusterName: 'perf-loop-ad-cluster',
            containerInsightsV2: ecs.ContainerInsights.ENABLED,
            defaultCloudMapNamespace: {
                name: 'perf.loop-ad.local',
            },
        });

        const autoScalingGroup = new autoscaling.AutoScalingGroup(this, 'EcsEc2AutoScalingGroup', {
            vpc,
            vpcSubnets: { subnets: privateSubnets },
            instanceType: new ec2.InstanceType('t4g.small'),
            machineImage: ecs.EcsOptimizedImage.amazonLinux2023(ecs.AmiHardwareType.ARM),
            minCapacity: 0,
            maxCapacity: 2,
        });
        const capacityProvider = new ecs.AsgCapacityProvider(this, 'EcsEc2CapacityProvider', {
            autoScalingGroup,
            enableManagedScaling: true,
            enableManagedTerminationProtection: false,
        });
        cluster.addAsgCapacityProvider(capacityProvider);

        const nlbSecurityGroup = new ec2.SecurityGroup(this, 'NlbSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Perf NLB event ingestion ingress only.',
        });
        const serverSecurityGroup = new ec2.SecurityGroup(this, 'ServerSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Perf ECS server SG shared by test services.',
        });
        const dataSourceSecurityGroup = new ec2.SecurityGroup(this, 'DataSourceSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Perf datasource SG shared by internal data endpoints.',
        });

        nlbSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public ingest to perf NLB.');
        nlbSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'NLB may reach perf servers.');
        serverSecurityGroup.addIngressRule(nlbSecurityGroup, ec2.Port.allTraffic(), 'NLB may enter perf servers.');
        serverSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Perf servers may call each other.');
        serverSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Perf servers may call each other.');
        serverSecurityGroup.addEgressRule(dataSourceSecurityGroup, ec2.Port.allTraffic(), 'Perf servers may reach internal datasources.');
        dataSourceSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Perf servers may enter internal datasources.');
        serverSecurityGroup.addEgressRule(endpointSecurityGroup, ec2.Port.allTraffic(), 'Perf servers may call private AWS APIs.');
        endpointSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Perf servers may use VPC endpoints.');

        const eventCollectorRepository = ecr.Repository.fromRepositoryName(
            this,
            'EventCollectorRepository',
            'loopad/event-collector',
        );
        const projectorRepository = ecr.Repository.fromRepositoryName(
            this,
            'AdContextProjectorRepository',
            'loopad/ad-context-projector',
        );

        const redisEndpoint = new ssm.StringParameter(this, 'RedisEndpointParameter', {
            parameterName: '/loop-ad/perf/redis/endpoint',
            stringValue: 'pending://perf/redis',
            description: 'Perf Redis endpoint contract.',
        });
        const clickhouseEndpoint = new ssm.StringParameter(this, 'ClickHouseEndpointParameter', {
            parameterName: '/loop-ad/perf/clickhouse/endpoint',
            stringValue: 'pending://perf/clickhouse',
            description: 'Perf ClickHouse endpoint contract.',
        });
        const mskEndpoint = new ssm.StringParameter(this, 'MskEndpointParameter', {
            parameterName: '/loop-ad/perf/msk/bootstrap-brokers',
            stringValue: 'pending://perf/msk',
            description: 'Perf MSK bootstrap broker contract.',
        });

        const nlb = new elbv2.NetworkLoadBalancer(this, 'NetworkLoadBalancer', {
            vpc,
            internetFacing: true,
            securityGroups: [nlbSecurityGroup],
            vpcSubnets: { subnets: publicSubnets },
        });

        const eventCollectorTask = new ecs.Ec2TaskDefinition(this, 'EventCollectorTaskDefinition', {
            networkMode: ecs.NetworkMode.AWS_VPC,
        });
        mskEndpoint.grantRead(eventCollectorTask.taskRole);
        const eventCollectorLogGroup = new logs.LogGroup(this, 'EventCollectorLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const eventCollectorContainer = eventCollectorTask.addContainer('EventCollectorContainer', {
            containerName: 'event-collector',
            image: ecs.ContainerImage.fromEcrRepository(eventCollectorRepository, 'latest'),
            memoryReservationMiB: 1024,
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'event-collector',
                logGroup: eventCollectorLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'perf',
                LOOPAD_SERVICE_ID: 'event-collector',
                LOOPAD_RUNTIME: 'go',
                LOOPAD_COMPUTE_TARGET: 'ecs-ec2',
                LOOPAD_MSK_ENDPOINT_PARAMETER: mskEndpoint.parameterName,
            },
        });
        eventCollectorContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const eventCollectorService = new ecs.Ec2Service(this, 'EventCollectorService', {
            cluster,
            taskDefinition: eventCollectorTask,
            serviceName: 'perf-event-collector',
            desiredCount: 1,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: { subnets: privateSubnets },
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'event-collector' },
            capacityProviderStrategies: [
                {
                    capacityProvider: capacityProvider.capacityProviderName,
                    weight: 1,
                },
            ],
        });
        eventCollectorService.autoScaleTaskCount({ minCapacity: 0, maxCapacity: 2 }).scaleOnCpuUtilization('EventCollectorCpuScaling', {
            targetUtilizationPercent: 70,
        });

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

        const projectorTask = new ecs.Ec2TaskDefinition(this, 'AdContextProjectorTaskDefinition', {
            networkMode: ecs.NetworkMode.AWS_VPC,
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
            memoryReservationMiB: 1024,
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'ad-context-projector',
                logGroup: projectorLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'perf',
                LOOPAD_SERVICE_ID: 'ad-context-projector',
                LOOPAD_RUNTIME: 'go',
                LOOPAD_COMPUTE_TARGET: 'ecs-ec2',
                LOOPAD_MSK_ENDPOINT_PARAMETER: mskEndpoint.parameterName,
                LOOPAD_REDIS_ENDPOINT_PARAMETER: redisEndpoint.parameterName,
                LOOPAD_CLICKHOUSE_ENDPOINT_PARAMETER: clickhouseEndpoint.parameterName,
            },
        });
        projectorContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const projectorService = new ecs.Ec2Service(this, 'AdContextProjectorService', {
            cluster,
            taskDefinition: projectorTask,
            serviceName: 'perf-ad-context-projector',
            desiredCount: 1,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: { subnets: privateSubnets },
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'ad-context-projector' },
            capacityProviderStrategies: [
                {
                    capacityProvider: capacityProvider.capacityProviderName,
                    weight: 1,
                },
            ],
        });
        projectorService.autoScaleTaskCount({ minCapacity: 0, maxCapacity: 2 }).scaleOnCpuUtilization('AdContextProjectorCpuScaling', {
            targetUtilizationPercent: 70,
        });
    }
}

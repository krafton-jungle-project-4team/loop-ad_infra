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
          name: 'private-app',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
        {
          name: 'private-data',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
    });

    const appSubnets = vpc.selectSubnets({ subnetGroupName: 'private-app' });
    const dataSubnets = vpc.selectSubnets({ subnetGroupName: 'private-data' });

    const endpointSecurityGroup = new ec2.SecurityGroup(this, 'VpcEndpointSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Private AWS API endpoint SG.',
    });

    vpc.addGatewayEndpoint('S3GatewayEndpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
      subnets: [appSubnets, dataSubnets],
    });

    vpc.addInterfaceEndpoint('EcrApiEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR,
      securityGroups: [endpointSecurityGroup],
      subnets: appSubnets,
    });
    vpc.addInterfaceEndpoint('EcrDockerEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
      securityGroups: [endpointSecurityGroup],
      subnets: appSubnets,
    });
    vpc.addInterfaceEndpoint('CloudWatchLogsEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
      securityGroups: [endpointSecurityGroup],
      subnets: appSubnets,
    });
    vpc.addInterfaceEndpoint('SecretsManagerEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
      securityGroups: [endpointSecurityGroup],
      subnets: appSubnets,
    });
    vpc.addInterfaceEndpoint('SsmEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.SSM,
      securityGroups: [endpointSecurityGroup],
      subnets: appSubnets,
    });
    vpc.addInterfaceEndpoint('EcsEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECS,
      securityGroups: [endpointSecurityGroup],
      subnets: appSubnets,
    });
    vpc.addInterfaceEndpoint('EcsAgentEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECS_AGENT,
      securityGroups: [endpointSecurityGroup],
      subnets: appSubnets,
    });
    vpc.addInterfaceEndpoint('EcsTelemetryEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECS_TELEMETRY,
      securityGroups: [endpointSecurityGroup],
      subnets: appSubnets,
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
    const eventCollectorSecurityGroup = new ec2.SecurityGroup(this, 'EventCollectorSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Dev Event Collector ECS task SG.',
    });
    const projectorSecurityGroup = new ec2.SecurityGroup(this, 'AdContextProjectorSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Dev Ad Context Projector ECS task SG.',
    });
    const decisionSecurityGroup = new ec2.SecurityGroup(this, 'AdDecisionApiSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Dev Ad Decision API ECS task SG.',
    });
    const dashboardSecurityGroup = new ec2.SecurityGroup(this, 'DashboardApiSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Dev Dashboard API ECS task SG.',
    });
    const recommendationSecurityGroup = new ec2.SecurityGroup(this, 'RecommendationSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Dev Recommendation ECS task SG.',
    });
    const auroraSecurityGroup = new ec2.SecurityGroup(this, 'AuroraSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Dev Aurora endpoint contract SG.',
    });
    const redisSecurityGroup = new ec2.SecurityGroup(this, 'RedisSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Dev Redis endpoint contract SG.',
    });
    const clickhouseSecurityGroup = new ec2.SecurityGroup(this, 'ClickHouseSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Dev ClickHouse endpoint contract SG.',
    });
    const mskSecurityGroup = new ec2.SecurityGroup(this, 'MskSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Dev MSK endpoint contract SG.',
    });

    albSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public HTTP to dev ALB.');
    nlbSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public ingest to dev NLB.');
    albSecurityGroup.addEgressRule(decisionSecurityGroup, ec2.Port.tcp(80), 'ALB to Ad Decision API.');
    albSecurityGroup.addEgressRule(dashboardSecurityGroup, ec2.Port.tcp(80), 'ALB to Dashboard API.');
    nlbSecurityGroup.addEgressRule(eventCollectorSecurityGroup, ec2.Port.tcp(80), 'NLB to Event Collector.');
    decisionSecurityGroup.addIngressRule(albSecurityGroup, ec2.Port.tcp(80), 'ALB may enter Ad Decision API.');
    dashboardSecurityGroup.addIngressRule(albSecurityGroup, ec2.Port.tcp(80), 'ALB may enter Dashboard API.');
    eventCollectorSecurityGroup.addIngressRule(nlbSecurityGroup, ec2.Port.tcp(80), 'NLB may enter Event Collector.');
    dashboardSecurityGroup.addEgressRule(recommendationSecurityGroup, ec2.Port.tcp(80), 'Dashboard API calls Recommendation.');
    recommendationSecurityGroup.addIngressRule(dashboardSecurityGroup, ec2.Port.tcp(80), 'Dashboard API may call Recommendation.');

    eventCollectorSecurityGroup.addEgressRule(mskSecurityGroup, ec2.Port.tcp(9098), 'Event Collector publishes to MSK.');
    mskSecurityGroup.addIngressRule(eventCollectorSecurityGroup, ec2.Port.tcp(9098), 'Event Collector may publish to MSK.');
    projectorSecurityGroup.addEgressRule(mskSecurityGroup, ec2.Port.tcp(9098), 'Projector consumes from MSK.');
    mskSecurityGroup.addIngressRule(projectorSecurityGroup, ec2.Port.tcp(9098), 'Projector may consume from MSK.');
    projectorSecurityGroup.addEgressRule(clickhouseSecurityGroup, ec2.Port.tcp(8123), 'Projector writes ClickHouse HTTP.');
    clickhouseSecurityGroup.addIngressRule(projectorSecurityGroup, ec2.Port.tcp(8123), 'Projector may write ClickHouse HTTP.');
    projectorSecurityGroup.addEgressRule(clickhouseSecurityGroup, ec2.Port.tcp(9000), 'Projector writes ClickHouse native.');
    clickhouseSecurityGroup.addIngressRule(projectorSecurityGroup, ec2.Port.tcp(9000), 'Projector may write ClickHouse native.');
    projectorSecurityGroup.addEgressRule(redisSecurityGroup, ec2.Port.tcp(6379), 'Projector writes Redis.');
    redisSecurityGroup.addIngressRule(projectorSecurityGroup, ec2.Port.tcp(6379), 'Projector may write Redis.');
    decisionSecurityGroup.addEgressRule(redisSecurityGroup, ec2.Port.tcp(6379), 'Ad Decision API reads Redis.');
    redisSecurityGroup.addIngressRule(decisionSecurityGroup, ec2.Port.tcp(6379), 'Ad Decision API may read Redis.');
    decisionSecurityGroup.addEgressRule(auroraSecurityGroup, ec2.Port.tcp(5432), 'Ad Decision API reads Aurora.');
    auroraSecurityGroup.addIngressRule(decisionSecurityGroup, ec2.Port.tcp(5432), 'Ad Decision API may read Aurora.');
    dashboardSecurityGroup.addEgressRule(auroraSecurityGroup, ec2.Port.tcp(5432), 'Dashboard API reads Aurora.');
    auroraSecurityGroup.addIngressRule(dashboardSecurityGroup, ec2.Port.tcp(5432), 'Dashboard API may read Aurora.');
    dashboardSecurityGroup.addEgressRule(clickhouseSecurityGroup, ec2.Port.tcp(8123), 'Dashboard API reads ClickHouse HTTP.');
    clickhouseSecurityGroup.addIngressRule(dashboardSecurityGroup, ec2.Port.tcp(8123), 'Dashboard API may read ClickHouse HTTP.');
    dashboardSecurityGroup.addEgressRule(clickhouseSecurityGroup, ec2.Port.tcp(9000), 'Dashboard API reads ClickHouse native.');
    clickhouseSecurityGroup.addIngressRule(dashboardSecurityGroup, ec2.Port.tcp(9000), 'Dashboard API may read ClickHouse native.');
    recommendationSecurityGroup.addEgressRule(auroraSecurityGroup, ec2.Port.tcp(5432), 'Recommendation reads Aurora.');
    auroraSecurityGroup.addIngressRule(recommendationSecurityGroup, ec2.Port.tcp(5432), 'Recommendation may read Aurora.');
    recommendationSecurityGroup.addEgressRule(clickhouseSecurityGroup, ec2.Port.tcp(8123), 'Recommendation reads ClickHouse HTTP.');
    clickhouseSecurityGroup.addIngressRule(recommendationSecurityGroup, ec2.Port.tcp(8123), 'Recommendation may read ClickHouse HTTP.');
    recommendationSecurityGroup.addEgressRule(clickhouseSecurityGroup, ec2.Port.tcp(9000), 'Recommendation reads ClickHouse native.');
    clickhouseSecurityGroup.addIngressRule(recommendationSecurityGroup, ec2.Port.tcp(9000), 'Recommendation may read ClickHouse native.');

    for (const serviceSecurityGroup of [
      eventCollectorSecurityGroup,
      projectorSecurityGroup,
      decisionSecurityGroup,
      dashboardSecurityGroup,
      recommendationSecurityGroup,
    ]) {
      serviceSecurityGroup.addEgressRule(endpointSecurityGroup, ec2.Port.tcp(443), 'ECS task calls private AWS APIs.');
      endpointSecurityGroup.addIngressRule(serviceSecurityGroup, ec2.Port.tcp(443), 'ECS task may use VPC endpoints.');
    }

    if (props.enableNatGateway) {
      dashboardSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Dashboard external HTTPS egress through NAT.');
      recommendationSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Recommendation external HTTPS egress through NAT.');
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
      description: 'Dev Aurora PostgreSQL endpoint contract. Port: 5432.',
    });
    const redisEndpoint = new ssm.StringParameter(this, 'RedisEndpointParameter', {
      parameterName: '/loop-ad/dev/redis/endpoint',
      stringValue: 'pending://dev/redis',
      description: 'Dev Redis endpoint contract. Port: 6379.',
    });
    const clickhouseEndpoint = new ssm.StringParameter(this, 'ClickHouseEndpointParameter', {
      parameterName: '/loop-ad/dev/clickhouse/endpoint',
      stringValue: 'pending://dev/clickhouse',
      description: 'Dev ClickHouse endpoint contract. Ports: 8123,9000.',
    });
    const mskEndpoint = new ssm.StringParameter(this, 'MskEndpointParameter', {
      parameterName: '/loop-ad/dev/msk/bootstrap-brokers',
      stringValue: 'pending://dev/msk',
      description: 'Dev MSK bootstrap broker contract. Port: 9098.',
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
      securityGroups: [eventCollectorSecurityGroup],
      vpcSubnets: appSubnets,
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
      securityGroups: [projectorSecurityGroup],
      vpcSubnets: appSubnets,
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
      securityGroups: [decisionSecurityGroup],
      vpcSubnets: appSubnets,
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
      securityGroups: [dashboardSecurityGroup],
      vpcSubnets: appSubnets,
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
      securityGroups: [recommendationSecurityGroup],
      vpcSubnets: appSubnets,
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
    new cdk.CfnOutput(this, 'PrivateAppSubnetIds', {
      value: cdk.Fn.join(',', appSubnets.subnets.map((subnet) => subnet.subnetId)),
      exportName: 'loop-ad-dev-private-app-subnet-ids',
    });
    new cdk.CfnOutput(this, 'PrivateAppSubnetRouteTableIds', {
      value: cdk.Fn.join(',', appSubnets.subnets.map((subnet) => subnet.routeTable.routeTableId)),
      exportName: 'loop-ad-dev-private-app-subnet-route-table-ids',
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
    const privateAppSubnetIds = cdk.Fn.importListValue('loop-ad-dev-private-app-subnet-ids', 2);
    const privateAppSubnetRouteTableIds = cdk.Fn.importListValue('loop-ad-dev-private-app-subnet-route-table-ids', 2);
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
    const privateAppSubnets = [
      ec2.Subnet.fromSubnetAttributes(this, 'PrivateAppSubnet1', {
        subnetId: privateAppSubnetIds[0],
        availabilityZone: availabilityZones[0],
        routeTableId: privateAppSubnetRouteTableIds[0],
      }),
      ec2.Subnet.fromSubnetAttributes(this, 'PrivateAppSubnet2', {
        subnetId: privateAppSubnetIds[1],
        availabilityZone: availabilityZones[1],
        routeTableId: privateAppSubnetRouteTableIds[1],
      }),
    ];
    const appSubnets = { subnets: privateAppSubnets };

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
      vpcSubnets: appSubnets,
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
    const eventCollectorSecurityGroup = new ec2.SecurityGroup(this, 'EventCollectorSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Perf Event Collector ECS task SG.',
    });
    const projectorSecurityGroup = new ec2.SecurityGroup(this, 'AdContextProjectorSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Perf Ad Context Projector ECS task SG.',
    });
    const redisSecurityGroup = new ec2.SecurityGroup(this, 'RedisSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Perf Redis endpoint contract SG.',
    });
    const clickhouseSecurityGroup = new ec2.SecurityGroup(this, 'ClickHouseSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Perf ClickHouse endpoint contract SG.',
    });
    const mskSecurityGroup = new ec2.SecurityGroup(this, 'MskSecurityGroup', {
      vpc,
      allowAllOutbound: false,
      description: 'Perf MSK endpoint contract SG.',
    });

    nlbSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public ingest to perf NLB.');
    nlbSecurityGroup.addEgressRule(eventCollectorSecurityGroup, ec2.Port.tcp(80), 'NLB to perf Event Collector.');
    eventCollectorSecurityGroup.addIngressRule(nlbSecurityGroup, ec2.Port.tcp(80), 'NLB may enter perf Event Collector.');
    eventCollectorSecurityGroup.addEgressRule(mskSecurityGroup, ec2.Port.tcp(9098), 'Event Collector publishes perf MSK.');
    mskSecurityGroup.addIngressRule(eventCollectorSecurityGroup, ec2.Port.tcp(9098), 'Event Collector may publish perf MSK.');
    projectorSecurityGroup.addEgressRule(mskSecurityGroup, ec2.Port.tcp(9098), 'Projector consumes perf MSK.');
    mskSecurityGroup.addIngressRule(projectorSecurityGroup, ec2.Port.tcp(9098), 'Projector may consume perf MSK.');
    projectorSecurityGroup.addEgressRule(redisSecurityGroup, ec2.Port.tcp(6379), 'Projector writes perf Redis.');
    redisSecurityGroup.addIngressRule(projectorSecurityGroup, ec2.Port.tcp(6379), 'Projector may write perf Redis.');
    projectorSecurityGroup.addEgressRule(clickhouseSecurityGroup, ec2.Port.tcp(8123), 'Projector writes perf ClickHouse HTTP.');
    clickhouseSecurityGroup.addIngressRule(projectorSecurityGroup, ec2.Port.tcp(8123), 'Projector may write perf ClickHouse HTTP.');
    projectorSecurityGroup.addEgressRule(clickhouseSecurityGroup, ec2.Port.tcp(9000), 'Projector writes perf ClickHouse native.');
    clickhouseSecurityGroup.addIngressRule(projectorSecurityGroup, ec2.Port.tcp(9000), 'Projector may write perf ClickHouse native.');

    eventCollectorSecurityGroup.addEgressRule(endpointSecurityGroup, ec2.Port.tcp(443), 'Perf Event Collector calls private AWS APIs.');
    endpointSecurityGroup.addIngressRule(eventCollectorSecurityGroup, ec2.Port.tcp(443), 'Perf Event Collector may use VPC endpoints.');
    projectorSecurityGroup.addEgressRule(endpointSecurityGroup, ec2.Port.tcp(443), 'Perf Projector calls private AWS APIs.');
    endpointSecurityGroup.addIngressRule(projectorSecurityGroup, ec2.Port.tcp(443), 'Perf Projector may use VPC endpoints.');

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
      description: 'Perf Redis endpoint contract. Port: 6379.',
    });
    const clickhouseEndpoint = new ssm.StringParameter(this, 'ClickHouseEndpointParameter', {
      parameterName: '/loop-ad/perf/clickhouse/endpoint',
      stringValue: 'pending://perf/clickhouse',
      description: 'Perf ClickHouse endpoint contract. Ports: 8123,9000.',
    });
    const mskEndpoint = new ssm.StringParameter(this, 'MskEndpointParameter', {
      parameterName: '/loop-ad/perf/msk/bootstrap-brokers',
      stringValue: 'pending://perf/msk',
      description: 'Perf MSK bootstrap broker contract. Port: 9098.',
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
      securityGroups: [eventCollectorSecurityGroup],
      vpcSubnets: appSubnets,
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
      securityGroups: [projectorSecurityGroup],
      vpcSubnets: appSubnets,
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

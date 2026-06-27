import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import {
    DEV_ECS_LOG_GROUP_PREFIX,
    DEV_LOG_RETENTION,
    DEV_SERVICE_DESIRED_TASKS,
    DEV_SERVICE_MAX_TASKS,
    DEV_SERVICE_MIN_TASKS,
    SERVICE_CPU_SCALE_TARGET_PERCENT,
} from './dev-config';

// ECS log group naming/retention is centralized so cost and retention reviews stay consistent.
export function createEcsServiceLogGroup(scope: Construct, id: string, serviceId: string): logs.LogGroup {
    return new logs.LogGroup(scope, id, {
        logGroupName: `${DEV_ECS_LOG_GROUP_PREFIX}/${serviceId}`,
        retention: DEV_LOG_RETENTION,
    });
}

// Construct IDs are caller-supplied to keep CloudFormation logical IDs stable when this helper changes.
export interface FargateHttpServiceConfig {
    readonly taskDefinitionId: string;
    readonly logGroupId: string;
    readonly containerId: string;
    readonly serviceConstructId: string;
    readonly cpuScalingId: string;
    readonly serviceId: string;
    readonly image: ecs.ContainerImage;
    readonly cluster: ecs.ICluster;
    readonly securityGroup: ec2.ISecurityGroup;
    readonly vpcSubnets: ec2.SubnetSelection;
    readonly environment: Record<string, string>;
    readonly secrets?: Record<string, ecs.Secret>;
    readonly healthCheckGracePeriod?: Duration;
    readonly grantTaskRole?: (taskDefinition: ecs.FargateTaskDefinition) => void;
}

export interface FargateHttpService {
    readonly taskDefinition: ecs.FargateTaskDefinition;
    readonly container: ecs.ContainerDefinition;
    readonly service: ecs.FargateService;
}

export function createFargateHttpService(scope: Construct, config: FargateHttpServiceConfig): FargateHttpService {
    // Dev runtime services share the same small ARM64 task shape to stay inside the monthly cost model.
    const taskDefinition = new ecs.FargateTaskDefinition(scope, config.taskDefinitionId, {
        cpu: 256,
        memoryLimitMiB: 512,
        runtimePlatform: {
            cpuArchitecture: ecs.CpuArchitecture.ARM64,
            operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
    });

    // Service-specific grants stay as a callback so storage ownership remains in the stack that wires the service.
    config.grantTaskRole?.(taskDefinition);

    const logGroup = createEcsServiceLogGroup(scope, config.logGroupId, config.serviceId);
    const container = taskDefinition.addContainer(config.containerId, {
        containerName: config.serviceId,
        image: config.image,
        logging: ecs.LogDrivers.awsLogs({
            streamPrefix: config.serviceId,
            logGroup,
        }),
        environment: config.environment,
        secrets: config.secrets,
    });
    container.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });

    // Cloud Map is the private service contract; ALB/NLB public exposure is attached explicitly in the stack.
    const service = new ecs.FargateService(scope, config.serviceConstructId, {
        cluster: config.cluster,
        taskDefinition,
        serviceName: `dev-${config.serviceId}`,
        desiredCount: DEV_SERVICE_DESIRED_TASKS,
        assignPublicIp: false,
        securityGroups: [config.securityGroup],
        vpcSubnets: config.vpcSubnets,
        circuitBreaker: { rollback: true },
        minHealthyPercent: 100,
        maxHealthyPercent: 200,
        cloudMapOptions: { name: config.serviceId },
        healthCheckGracePeriod: config.healthCheckGracePeriod,
    });

    // Scaling bounds are centralized because they directly cap the dev Fargate spend.
    service.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization(config.cpuScalingId, {
        targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
    });

    return {
        taskDefinition,
        container,
        service,
    };
}

export interface StaticFrontendSiteConfig {
    readonly idPrefix: string;
    readonly siteName: string;
    readonly bucketName: string;
    readonly recordName: string;
    readonly domainName: string;
    readonly certificate: acm.ICertificate;
    readonly publicHostedZone: route53.IHostedZone;
}

// Static frontend sites share the same secure S3, CloudFront, DNS, and SSM output contract.
export function createStaticFrontendSite(scope: Construct, config: StaticFrontendSiteConfig): void {
    const bucket = new s3.Bucket(scope, `${config.idPrefix}Bucket`, {
        bucketName: config.bucketName,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        encryption: s3.BucketEncryption.S3_MANAGED,
        enforceSSL: true,
        versioned: true,
        objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        removalPolicy: RemovalPolicy.RETAIN,
    });
    const distribution = new cloudfront.Distribution(scope, `${config.idPrefix}Distribution`, {
        domainNames: [config.domainName],
        certificate: config.certificate,
        comment: `Dev ${config.siteName} frontend for ${config.domainName}`,
        defaultRootObject: 'index.html',
        priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
        defaultBehavior: {
            origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
            viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
            cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
            cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
            compress: true,
        },
        errorResponses: [
            {
                httpStatus: 403,
                responseHttpStatus: 200,
                responsePagePath: '/index.html',
                ttl: Duration.seconds(0),
            },
            {
                httpStatus: 404,
                responseHttpStatus: 200,
                responsePagePath: '/index.html',
                ttl: Duration.seconds(0),
            },
        ],
    });
    new route53.ARecord(scope, `${config.idPrefix}DnsRecord`, {
        zone: config.publicHostedZone,
        recordName: config.recordName,
        target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(distribution)),
    });

    for (const parameter of [
        {
            id: `${config.idPrefix}BucketNameParameter`,
            name: 'bucket-name',
            value: config.bucketName,
            description: `Dev ${config.siteName} frontend S3 bucket name.`,
        },
        {
            id: `${config.idPrefix}CloudFrontDistributionIdParameter`,
            name: 'cloudfront-distribution-id',
            value: distribution.distributionId,
            description: `Dev ${config.siteName} frontend CloudFront distribution ID.`,
        },
    ] as const) {
        new ssm.StringParameter(scope, parameter.id, {
            parameterName: `/loop-ad/dev/frontend/${config.siteName}/${parameter.name}`,
            stringValue: parameter.value,
            description: parameter.description,
        });
    }
}

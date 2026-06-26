import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
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
} from './dev-config';

export function createEcsServiceLogGroup(scope: Construct, id: string, serviceId: string): logs.LogGroup {
    return new logs.LogGroup(scope, id, {
        logGroupName: `${DEV_ECS_LOG_GROUP_PREFIX}/${serviceId}`,
        retention: DEV_LOG_RETENTION,
    });
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

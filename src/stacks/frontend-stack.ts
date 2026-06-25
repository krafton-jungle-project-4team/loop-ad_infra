import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import type { EnvironmentMode } from '../config/loop-ad-config';
import type { FrontendResources } from './stack-interfaces';

export interface FrontendStackProps extends cdk.StackProps {
  readonly mode: EnvironmentMode;
}

export class FrontendStack extends cdk.Stack implements FrontendResources {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;

  public constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, props);

    this.bucket = new s3.Bucket(this, 'FrontendBucket', {
      bucketName: `${props.mode.name}-loop-ad-frontend-media`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: false,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.distribution = new cloudfront.Distribution(this, 'FrontendDistribution', {
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
        cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
      },
    });
  }
}

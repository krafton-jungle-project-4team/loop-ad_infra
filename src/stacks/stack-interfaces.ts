import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import type { DataStoreId, ServiceId } from '../config/loop-ad-config';

export type ServiceSecurityGroupMap = Partial<Record<ServiceId, ec2.SecurityGroup>>;
export type DataStoreSecurityGroupMap = Partial<Record<DataStoreId, ec2.SecurityGroup>>;
export type RepositoryMap = Partial<Record<ServiceId, ecr.Repository>>;
export type EndpointParameterMap = Partial<Record<DataStoreId, ssm.StringParameter>>;

export interface EdgeSecurityGroups {
  readonly alb: ec2.SecurityGroup;
  readonly nlb: ec2.SecurityGroup;
}

export interface NetworkResources {
  readonly vpc: ec2.Vpc;
  readonly cluster: ecs.Cluster;
  readonly appSubnets: ec2.SubnetSelection;
  readonly dataSubnets: ec2.SubnetSelection;
  readonly serviceSecurityGroups: ServiceSecurityGroupMap;
  readonly dataStoreSecurityGroups: DataStoreSecurityGroupMap;
  readonly edgeSecurityGroups: EdgeSecurityGroups;
  readonly ec2CapacityProvider?: ecs.AsgCapacityProvider;
}

export interface EdgeResources {
  readonly alb: elbv2.ApplicationLoadBalancer;
  readonly albListener: elbv2.ApplicationListener;
  readonly nlb: elbv2.NetworkLoadBalancer;
}

export interface FrontendResources {
  readonly bucket: s3.Bucket;
  readonly distribution: cloudfront.Distribution;
}

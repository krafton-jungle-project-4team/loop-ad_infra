import * as cdk from 'aws-cdk-lib';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import { Construct } from 'constructs';
import {
  dataStoreById,
  dataStoresFor,
  ENVIRONMENT_MODES,
  resolveComputeTarget,
  routesFor,
  serviceById,
  servicesFor,
  type EnvironmentMode,
} from '../config/loop-ad-config';
import type { DataStoreSecurityGroupMap, NetworkResources, ServiceSecurityGroupMap } from './stack-interfaces';

export interface NetworkStackProps extends cdk.StackProps {
  readonly mode: EnvironmentMode;
  readonly enableNatGateway: boolean;
  readonly enableVpcEndpoints: boolean;
}

export class NetworkStack extends cdk.Stack implements NetworkResources {
  public readonly vpc: ec2.Vpc;
  public readonly cluster: ecs.Cluster;
  public readonly appSubnets: ec2.SubnetSelection;
  public readonly dataSubnets: ec2.SubnetSelection;
  public readonly serviceSecurityGroups: ServiceSecurityGroupMap;
  public readonly dataStoreSecurityGroups: DataStoreSecurityGroupMap;
  public readonly edgeSecurityGroups: NetworkResources['edgeSecurityGroups'];
  public readonly ec2CapacityProvider?: ecs.AsgCapacityProvider;

  public constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    const services = servicesFor(props.mode.name);
    const dataStores = dataStoresFor(props.mode.name);
    const hasEc2Workloads = services.some((service) => resolveComputeTarget(service, props.mode.name) === 'ecs-ec2');

    this.appSubnets = { subnetGroupName: 'private-app' };
    this.dataSubnets = { subnetGroupName: 'private-data' };

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: props.mode.maxAzs,
      natGateways: props.enableNatGateway ? props.mode.natGatewayCountWhenEnabled : 0,
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

    this.edgeSecurityGroups = {
      alb: this.createSecurityGroup('AlbSecurityGroup', 'ALB public HTTP ingress only.'),
      nlb: this.createSecurityGroup('NlbSecurityGroup', 'NLB event ingestion ingress only.'),
    };
    this.serviceSecurityGroups = Object.fromEntries(
      services.map((service) => [
        service.id,
        this.createSecurityGroup(`${pascalCase(service.id)}ServiceSecurityGroup`, `${service.displayName} ECS task SG.`),
      ]),
    ) as ServiceSecurityGroupMap;
    this.dataStoreSecurityGroups = Object.fromEntries(
      dataStores.map((store) => [
        store.id,
        this.createSecurityGroup(`${pascalCase(store.id)}DataSecurityGroup`, `${store.displayName} endpoint contract SG.`),
      ]),
    ) as DataStoreSecurityGroupMap;

    this.cluster = new ecs.Cluster(this, 'Cluster', {
      vpc: this.vpc,
      clusterName: `${props.mode.name}-loop-ad-cluster`,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
      defaultCloudMapNamespace: {
        name: `${props.mode.name}.loop-ad.local`,
      },
    });

    if (hasEc2Workloads) {
      this.ec2CapacityProvider = this.createEc2CapacityProvider(props.mode);
    }

    this.applyEdgeRules(props.mode);
    this.applyServiceToServiceRules(props.mode);
    this.applyServiceToDataRules(props.mode);
    this.applyPrivateAwsAccessRules(props);
    this.applyExternalEgressRules(props);
  }

  private createSecurityGroup(id: string, description: string): ec2.SecurityGroup {
    return new ec2.SecurityGroup(this, id, {
      vpc: this.vpc,
      allowAllOutbound: false,
      description,
    });
  }

  private createEc2CapacityProvider(mode: EnvironmentMode): ecs.AsgCapacityProvider {
    const autoScalingGroup = new autoscaling.AutoScalingGroup(this, 'EcsEc2AutoScalingGroup', {
      vpc: this.vpc,
      vpcSubnets: this.appSubnets,
      instanceType: new ec2.InstanceType(mode.ecsOnEc2.instanceType),
      machineImage: ecs.EcsOptimizedImage.amazonLinux2023(ecs.AmiHardwareType.ARM),
      minCapacity: mode.ecsOnEc2.minCapacity,
      maxCapacity: mode.ecsOnEc2.maxCapacity,
    });

    const capacityProvider = new ecs.AsgCapacityProvider(this, 'EcsEc2CapacityProvider', {
      autoScalingGroup,
      enableManagedScaling: true,
      enableManagedTerminationProtection: false,
    });

    this.cluster.addAsgCapacityProvider(capacityProvider);
    return capacityProvider;
  }

  private applyEdgeRules(mode: EnvironmentMode): void {
    // Public ingress는 LB 80 포트에서만 끝난다. ECS task SG는 인터넷 CIDR를 직접 받지 않는다.
    this.edgeSecurityGroups.alb.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public HTTP to ALB only.');
    this.edgeSecurityGroups.nlb.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public event ingest to NLB only.');

    for (const route of routesFor(mode.name)) {
      const service = serviceById(route.targetServiceId);
      const serviceSecurityGroup = this.requiredServiceSecurityGroup(service.id);
      const edgeSecurityGroup = route.loadBalancer === 'alb' ? this.edgeSecurityGroups.alb : this.edgeSecurityGroups.nlb;

      edgeSecurityGroup.addEgressRule(serviceSecurityGroup, ec2.Port.tcp(service.port), `${route.loadBalancer.toUpperCase()} to ${service.id}.`);
      serviceSecurityGroup.addIngressRule(edgeSecurityGroup, ec2.Port.tcp(service.port), `${route.loadBalancer.toUpperCase()} may enter ${service.id}.`);
    }
  }

  private applyServiceToServiceRules(mode: EnvironmentMode): void {
    for (const source of servicesFor(mode.name)) {
      const sourceSecurityGroup = this.requiredServiceSecurityGroup(source.id);
      for (const targetId of source.callsServices ?? []) {
        const target = serviceById(targetId);
        const targetSecurityGroup = this.requiredServiceSecurityGroup(target.id);

        sourceSecurityGroup.addEgressRule(targetSecurityGroup, ec2.Port.tcp(target.port), `${source.id} calls ${target.id}.`);
        targetSecurityGroup.addIngressRule(sourceSecurityGroup, ec2.Port.tcp(target.port), `${source.id} may call ${target.id}.`);
      }
    }
  }

  private applyServiceToDataRules(mode: EnvironmentMode): void {
    for (const service of servicesFor(mode.name)) {
      const sourceSecurityGroup = this.requiredServiceSecurityGroup(service.id);
      for (const access of service.dataAccess ?? []) {
        const store = dataStoreById(access.store);
        const dataSecurityGroup = this.requiredDataStoreSecurityGroup(store.id);

        for (const port of store.ports) {
          sourceSecurityGroup.addEgressRule(dataSecurityGroup, ec2.Port.tcp(port), `${service.id} ${access.mode} ${store.id}.`);
          dataSecurityGroup.addIngressRule(sourceSecurityGroup, ec2.Port.tcp(port), `${service.id} ${access.mode} ${store.id}.`);
        }
      }
    }
  }

  private applyPrivateAwsAccessRules(props: NetworkStackProps): void {
    if (!props.enableVpcEndpoints) {
      return;
    }

    const endpointSecurityGroup = this.createSecurityGroup('VpcEndpointSecurityGroup', 'Private AWS API endpoint SG.');
    for (const service of servicesFor(props.mode.name)) {
      const serviceSecurityGroup = this.requiredServiceSecurityGroup(service.id);
      serviceSecurityGroup.addEgressRule(endpointSecurityGroup, ec2.Port.tcp(443), `${service.id} calls AWS APIs through endpoints.`);
      endpointSecurityGroup.addIngressRule(serviceSecurityGroup, ec2.Port.tcp(443), `${service.id} may use VPC endpoints.`);
    }

    // S3는 Gateway Endpoint로 두어 endpoint ENI 비용을 줄이고, ECS/ECR/log/secret은 Interface Endpoint를 사용한다.
    this.vpc.addGatewayEndpoint('S3GatewayEndpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
      subnets: [this.appSubnets, this.dataSubnets],
    });

    for (const [id, service] of [
      ['EcrApiEndpoint', ec2.InterfaceVpcEndpointAwsService.ECR],
      ['EcrDockerEndpoint', ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER],
      ['CloudWatchLogsEndpoint', ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS],
      ['SecretsManagerEndpoint', ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER],
      ['SsmEndpoint', ec2.InterfaceVpcEndpointAwsService.SSM],
      ['EcsEndpoint', ec2.InterfaceVpcEndpointAwsService.ECS],
      ['EcsAgentEndpoint', ec2.InterfaceVpcEndpointAwsService.ECS_AGENT],
      ['EcsTelemetryEndpoint', ec2.InterfaceVpcEndpointAwsService.ECS_TELEMETRY],
    ] as const) {
      this.vpc.addInterfaceEndpoint(id, {
        service,
        securityGroups: [endpointSecurityGroup],
        subnets: this.appSubnets,
      });
    }
  }

  private applyExternalEgressRules(props: NetworkStackProps): void {
    if (!props.enableNatGateway) {
      return;
    }

    for (const service of servicesFor(props.mode.name)) {
      if ((service.externalEgressProviders ?? []).length === 0) {
        continue;
      }

      // 외부 SaaS는 직접 구성하지 않고, NAT가 켜진 경우 HTTPS egress 계약만 연다.
      this.requiredServiceSecurityGroup(service.id).addEgressRule(
        ec2.Peer.anyIpv4(),
        ec2.Port.tcp(443),
        `${service.id} external HTTPS egress through NAT only.`,
      );
    }
  }

  private requiredServiceSecurityGroup(serviceId: string): ec2.SecurityGroup {
    const securityGroup = this.serviceSecurityGroups[serviceId as keyof ServiceSecurityGroupMap];
    if (securityGroup === undefined) {
      throw new Error(`Missing service security group: ${serviceId}`);
    }

    return securityGroup;
  }

  private requiredDataStoreSecurityGroup(storeId: string): ec2.SecurityGroup {
    const securityGroup = this.dataStoreSecurityGroups[storeId as keyof DataStoreSecurityGroupMap];
    if (securityGroup === undefined) {
      throw new Error(`Missing datastore security group: ${storeId}`);
    }

    return securityGroup;
  }
}

function pascalCase(value: string): string {
  return value
    .split(/[^a-zA-Z0-9]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('');
}

export function networkStackName(mode: keyof typeof ENVIRONMENT_MODES): string {
  return `LoopAd${pascalCase(mode)}NetworkStack`;
}

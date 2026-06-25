import { Duration } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import {
  EXTERNAL_PROVIDER_DEFINITIONS,
  externalSecretParameterName,
  resolveComputeTarget,
  serviceById,
  type ComputeTarget,
  type DataStoreId,
  type EnvironmentMode,
  type ServiceDefinition,
} from '../config/loop-ad-config';

export interface LoopAdEcsServiceProps {
  readonly mode: EnvironmentMode;
  readonly service: ServiceDefinition;
  readonly cluster: ecs.Cluster;
  readonly repository: ecr.IRepository;
  readonly securityGroup: ec2.ISecurityGroup;
  readonly appSubnets: ec2.SubnetSelection;
  readonly endpointParameters: Readonly<Partial<Record<DataStoreId, ssm.IStringParameter>>>;
  readonly ec2CapacityProvider?: ecs.AsgCapacityProvider;
}

export class LoopAdEcsService extends Construct {
  public readonly ecsService: ecs.FargateService | ecs.Ec2Service;
  public readonly taskDefinition: ecs.FargateTaskDefinition | ecs.Ec2TaskDefinition;
  public readonly computeTarget: ComputeTarget;

  public constructor(scope: Construct, id: string, props: LoopAdEcsServiceProps) {
    super(scope, id);

    this.computeTarget = resolveComputeTarget(props.service, props.mode.name);
    this.taskDefinition = this.createTaskDefinition(props);
    this.addApplicationContainer(props);
    this.ecsService = this.createService(props);
    this.configureScaling(props);
  }

  private createTaskDefinition(props: LoopAdEcsServiceProps): ecs.FargateTaskDefinition | ecs.Ec2TaskDefinition {
    if (this.computeTarget === 'fargate') {
      return new ecs.FargateTaskDefinition(this, 'TaskDefinition', {
        cpu: props.mode.fargate.cpu,
        memoryLimitMiB: props.mode.fargate.memoryMiB,
        runtimePlatform: {
          cpuArchitecture: ecs.CpuArchitecture.ARM64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
      });
    }

    if (props.ec2CapacityProvider === undefined) {
      throw new Error(`${props.service.id} selected ecs-ec2 but no EC2 capacity provider was supplied.`);
    }

    return new ecs.Ec2TaskDefinition(this, 'TaskDefinition', {
      networkMode: ecs.NetworkMode.AWS_VPC,
    });
  }

  private addApplicationContainer(props: LoopAdEcsServiceProps): void {
    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      retention: logRetention(props.mode.logRetentionDays),
    });

    const container = this.taskDefinition.addContainer('AppContainer', {
      containerName: props.service.containerName,
      image: ecs.ContainerImage.fromEcrRepository(props.repository, 'latest'),
      memoryReservationMiB: this.computeTarget === 'ecs-ec2' ? props.mode.fargate.memoryMiB : undefined,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: props.service.id,
        logGroup,
      }),
      environment: this.environmentFor(props),
    });

    container.addPortMappings({
      containerPort: props.service.port,
      protocol: ecs.Protocol.TCP,
    });
  }

  private createService(props: LoopAdEcsServiceProps): ecs.FargateService | ecs.Ec2Service {
    if (this.computeTarget === 'fargate') {
      return new ecs.FargateService(this, 'Service', {
        cluster: props.cluster,
        taskDefinition: this.taskDefinition as ecs.FargateTaskDefinition,
        serviceName: `${props.mode.name}-${props.service.id}`,
        desiredCount: props.mode.fargate.desiredCount,
        assignPublicIp: false,
        securityGroups: [props.securityGroup],
        vpcSubnets: props.appSubnets,
        circuitBreaker: {
          rollback: true,
        },
        minHealthyPercent: 100,
        maxHealthyPercent: 200,
        cloudMapOptions: {
          name: props.service.id,
        },
        healthCheckGracePeriod: props.service.ingress !== undefined ? Duration.seconds(60) : undefined,
      });
    }

    return new ecs.Ec2Service(this, 'Service', {
      cluster: props.cluster,
      taskDefinition: this.taskDefinition as ecs.Ec2TaskDefinition,
      serviceName: `${props.mode.name}-${props.service.id}`,
      desiredCount: props.mode.fargate.desiredCount,
      securityGroups: [props.securityGroup],
      vpcSubnets: props.appSubnets,
      circuitBreaker: {
        rollback: true,
      },
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      cloudMapOptions: {
        name: props.service.id,
      },
      capacityProviderStrategies: [
        {
          capacityProvider: props.ec2CapacityProvider?.capacityProviderName ?? 'missing-capacity-provider',
          weight: 1,
        },
      ],
    });
  }

  private configureScaling(props: LoopAdEcsServiceProps): void {
    this.ecsService
      .autoScaleTaskCount({
        minCapacity: props.mode.fargate.minTasks,
        maxCapacity: props.mode.fargate.maxTasks,
      })
      .scaleOnCpuUtilization('CpuScaling', {
        targetUtilizationPercent: 70,
      });
  }

  private environmentFor(props: LoopAdEcsServiceProps): Record<string, string> {
    const environment: Record<string, string> = {
      LOOPAD_ENV: props.mode.name,
      LOOPAD_SERVICE_ID: props.service.id,
      LOOPAD_SERVICE_NAME: props.service.displayName,
      LOOPAD_SOURCE_REPOSITORY: props.service.sourceRepository,
      LOOPAD_RUNTIME: props.service.runtime,
      LOOPAD_COMPUTE_TARGET: this.computeTarget,
    };

    for (const access of props.service.dataAccess ?? []) {
      const parameter = props.endpointParameters[access.store];
      if (parameter === undefined) {
        throw new Error(`${props.service.id} references ${access.store}, but no endpoint parameter was provided.`);
      }

      parameter.grantRead(this.taskDefinition.taskRole);
      environment[`LOOPAD_${constantName(access.store)}_ENDPOINT_PARAMETER`] = parameter.parameterName;
    }

    for (const targetId of props.service.callsServices ?? []) {
      const target = serviceById(targetId);
      environment[`LOOPAD_${constantName(target.id)}_URL`] = `http://${target.id}.${props.mode.name}.loop-ad.local:${target.port}`;
    }

    for (const providerId of props.service.externalEgressProviders ?? []) {
      const provider = EXTERNAL_PROVIDER_DEFINITIONS.find((candidate) => candidate.id === providerId);
      if (provider === undefined) {
        throw new Error(`Unknown external provider: ${providerId}`);
      }

      // 외부 서비스 자체는 이 repo의 책임이 아니다. 앱에는 secret parameter 이름만 계약으로 넘긴다.
      environment[`LOOPAD_${constantName(provider.id)}_SECRET_PARAMETER`] = externalSecretParameterName(provider, props.mode.name);
    }

    return environment;
  }
}

function logRetention(days: 3 | 7): logs.RetentionDays {
  return days === 7 ? logs.RetentionDays.ONE_WEEK : logs.RetentionDays.THREE_DAYS;
}

function constantName(value: string): string {
  return value.replace(/[^a-zA-Z0-9]+/g, '_').toUpperCase();
}

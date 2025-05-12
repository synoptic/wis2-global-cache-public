from aws_cdk import (
    Stack,
    aws_iam as iam,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_secretsmanager as sm,
    aws_ecs_patterns as ecs_patterns,
    RemovalPolicy,
    aws_route53 as r53,
    aws_route53_targets as r53_targets,
    aws_elasticloadbalancingv2 as elbv2, CfnOutput, SymlinkFollowMode,
    aws_logs as alogs
)
from constructs import Construct
from aws_cdk.aws_ecr_assets import DockerImageAsset, Platform


class Wis2BrokerStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, zone_domain_name: str, zone_id: str, a_record_name: str,
                 cert_arn: str, memory_footprint: int = 512,
                 **kwargs) -> None:
        super().__init__(scope, construct_id,
                         description="Broker to publish messages for WMO/WIS2.0 Global Cache.",
                         **kwargs)
        vpc = ec2.Vpc.from_lookup(self, "vpc", is_default=True)

        # Open port 2049 on EFS to allow container access
        efs_security_group = ec2.SecurityGroup(self, f"{construct_id}-efs-security",
                                               vpc=vpc,
                                               allow_all_outbound=True,
                                               description="Allow EFS access",
                                               )
        efs_security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(2049), "allow efs access")
        # todo - use cidr instead of any ipv4 (may or may not work)

        # Create file system for persistence
        file_system = efs.FileSystem(
            self, f"{construct_id}-filesystem",
            vpc=vpc,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.BURSTING,
            removal_policy=RemovalPolicy.DESTROY,
            security_group=efs_security_group
        )

        # Create volume for container
        wis2_broker_volume = ecs.Volume(
            name=f"{construct_id}-volume",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                root_directory='/',
                file_system_id=file_system.file_system_id
            ),
        )

        # Create mount point for container that corresponds to mosquitto persistence location
        mosquitto_volume_mount_point = ecs.MountPoint(
            read_only=False,
            container_path="/mosquitto/data",
            source_volume=wis2_broker_volume.name
        )

        # Create execution role
        execution_role = iam.Role(self, f"{construct_id}-task-role",
                                  assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))

        execution_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy"))

        # Add policy to allow ECS to pull from secrets manager
        execution_role.add_managed_policy(iam.ManagedPolicy.from_managed_policy_arn(self, "secrets-manager-policy",
                                                                                    "arn:aws:iam::aws:policy"
                                                                                    "/SecretsManagerReadWrite"))

        fargate_task = ecs.FargateTaskDefinition(self,
                                                 f"{construct_id}-task",
                                                 volumes=[wis2_broker_volume],
                                                 execution_role=execution_role,
                                                 memory_limit_mib=memory_footprint,
                                                 cpu=memory_footprint / 2
                                                 )

        # Build broker docker image
        asset = DockerImageAsset(self, f"{construct_id}-image",
                                 directory="../broker",
                                 follow_symlinks=SymlinkFollowMode.ALWAYS,
                                 platform=Platform.LINUX_AMD64,
                                 )

        # get wis2.0 admin credentials from secrets manager
        wis2_admin_creds = sm.Secret.from_secret_attributes(self, f"{construct_id}-admin-creds",
                                                            secret_complete_arn="arn:aws:secretsmanager:us-west-2"
                                                                                ":841149120145:secret:wis2-broker-admin"
                                                                                "-credentials-QrMmpv")

        if 'dev' in construct_id:
            # get wis2.0 read only credentials from secrets manager
            wis2_general_creds = sm.Secret.from_secret_attributes(self, f"{construct_id}-read-only-creds",
                                                                  secret_complete_arn="arn:aws:secretsmanager:us-west-2:841149120145:secret:wis2-broker-everyone-sGmM63")
        else:
            # get wis2.0 read only credentials from secrets manager
            wis2_general_creds = sm.Secret.from_secret_attributes(self, f"{construct_id}-read-only-creds",
                                                                  secret_complete_arn="arn:aws:secretsmanager:us-west-2"
                                                                                      ":841149120145:secret:wis2-broker-read-only"
                                                                                      "-credentials-idpNOx")

        # Add container with Wis2.0 credentials
        app_container = fargate_task.add_container(
            f"{construct_id}-container",
            logging=ecs.AwsLogDriver(stream_prefix=construct_id, log_retention=alogs.RetentionDays.ONE_WEEK),
            image=ecs.ContainerImage.from_registry(asset.image_uri),
            secrets={
                "ADMIN_USER": ecs.Secret.from_secrets_manager(secret=wis2_admin_creds, field="user"),
                "ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(secret=wis2_admin_creds, field="password"),
                "WIS2_USER": ecs.Secret.from_secrets_manager(secret=wis2_general_creds, field="user"),
                "WIS2_PASSWORD": ecs.Secret.from_secrets_manager(secret=wis2_general_creds, field="password")
            }
        )

        # Add mosquitto volume to allow for persistence
        app_container.add_mount_points(mosquitto_volume_mount_point)

        app_container.add_port_mappings(
            ecs.PortMapping(container_port=1883),
            ecs.PortMapping(container_port=9001)
        )

        cluster = ecs.Cluster(
            self,
            f'{construct_id}-cluster',
            vpc=vpc
        )

        fargate = ecs_patterns.NetworkMultipleTargetGroupsFargateService(
            self,
            f"{construct_id}-service",
            # memory_limit_mib=memory_footprint,
            assign_public_ip=True,
            desired_count=1,
            task_definition=fargate_task,
            cluster=cluster,
            load_balancers=[
                ecs_patterns.NetworkLoadBalancerProps(
                    name=f"{construct_id}-load-balancer",
                    listeners=[
                        ecs_patterns.NetworkListenerProps(
                            name=f"{construct_id}-1883-listener",
                            port=1883
                        ),
                    ],
                )
            ],
            target_groups=[
                ecs_patterns.NetworkTargetProps(
                    container_port=1883
                ),
            ],
        )
        fargate.load_balancer.add_listener(
            id=f"{construct_id}-8883-tls-listener",
            port=8883,
            certificates=[elbv2.ListenerCertificate.from_arn(
                cert_arn
            )],
            protocol=elbv2.Protocol.TLS,
            default_target_groups=[fargate.target_group]
        )

        # Allow target group to connect on port 1883
        # todo - pull this out to support GB whitelisting
        fargate.service.connections.allow_from(ec2.Peer.any_ipv4(), ec2.Port.tcp(1883))

        # to share with other stacks/services
        self.broker_url = fargate.load_balancer.load_balancer_dns_name

        # add to existing hosted zone
        # create alias DNS record
        wis2_zone = r53.HostedZone.from_lookup(self, id=zone_id, domain_name=zone_domain_name)
        cache_lb_alias = r53.ARecord(self, f"{construct_id}-dns",
                                     delete_existing=True,
                                     zone=wis2_zone,
                                     record_name=a_record_name,
                                     target=r53.RecordTarget.from_alias(
                                         r53_targets.LoadBalancerTarget(fargate.load_balancer)))

        # Cfn output
        # CfnOutput(self, "WIS2User", value=wis2_general_creds.secret_value.to_string())
        # CfnOutput(self, "WIS2Password", value=wis2_general_creds.secret_value.to_string())
        # broker endpoint
        CfnOutput(self, "BrokerURL", value=f"https://{a_record_name}.{zone_domain_name}")

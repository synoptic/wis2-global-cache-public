import json
import os

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
    aws_servicediscovery as sd
)
from constructs import Construct
from aws_cdk.aws_ecr_assets import DockerImageAsset, Platform


class EmqxBrokerStack(Stack):

    def __init__(self, scope: Construct, construct_id: str,
                 zone_domain_name: str,
                 zone_id: str,
                 a_record_name: str,
                 cert_arn: str,
                 vpc_id: str = None,
                 admin_creds: str = None,
                 emqx_dash_admin_creds: str = None,
                 publisher_creds: str = None,
                 everyone_creds: str = None,
                 subscriber_creds: str = None,
                 memory_footprint: int = 512,
                 **kwargs) -> None:

        super().__init__(scope, construct_id,
                         description="WIS2 GC MQTT broker",
                         **kwargs)

        # Use provided VPC or fall back to default VPC
        if vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "vpc", vpc_id=vpc_id)
        else:
            vpc = ec2.Vpc.from_lookup(self, "vpc", is_default=True)

        env_tag = "dev" if 'dev' in construct_id else "prod"

        # Create execution role
        execution_role = iam.Role(self, f"{construct_id}-role",
                                  assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))

        execution_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy"))
        fargate_task = ecs.FargateTaskDefinition(self,
                                                 f"{construct_id}-task",
                                                 execution_role=execution_role,
                                                 memory_limit_mib=memory_footprint,
                                                 cpu=memory_footprint / 2
                                                 )

        # Build broker docker image
        asset = DockerImageAsset(self, f"{construct_id}-image",
                                 directory="../emqx_broker",
                                 follow_symlinks=SymlinkFollowMode.ALWAYS,
                                 platform=Platform.LINUX_AMD64,
                                 )

        # Get credentials from secrets manager using provided ARNs
        # mqtt_admin_creds = sm.Secret.from_secret_complete_arn(
        #     self, f"{construct_id}-admin-creds",
        #     secret_complete_arn=admin_creds_arn)

        # emqx_dash_admin_creds = sm.Secret.from_secret_complete_arn(
        #     self, f"{construct_id}-emqx-admin-creds",
        #     secret_complete_arn=emqx_dash_admin_creds_arn)

        # mqtt_pub_creds = sm.Secret.from_secret_complete_arn(
        #     self, f"{construct_id}-publisher-creds",
        #     secret_complete_arn=publisher_creds_arn)
        #
        # mqtt_everyone_creds = sm.Secret.from_secret_complete_arn(
        #     self, f"{construct_id}-everyone-creds",
        #     secret_complete_arn=everyone_creds_arn)

        # mqtt_sub_creds = sm.Secret.from_secret_complete_arn(
        #     self, f"{construct_id}-read-only-creds",
        #     secret_complete_arn=subscriber_creds_arn)

        # Parse credentials from JSON strings
        mqtt_admin_creds = json.loads(admin_creds)
        emqx_dash_admin_creds = json.loads(emqx_dash_admin_creds)
        mqtt_pub_creds = json.loads(publisher_creds)
        mqtt_everyone_creds = json.loads(everyone_creds)
        mqtt_sub_creds = json.loads(subscriber_creds)

        # Add container with WIS2 credentials
        discovery_namespace = ".".join([env_tag, "wis2.service"])
        discovery_a = "-".join([env_tag, "emqx-disco"])
        service_discovery = sd.PrivateDnsNamespace(self, f"{construct_id}-dns",
                                                   name=discovery_namespace,
                                                   vpc=vpc,
                                                   description="Service discovery for EMQX broker")
        app_container = fargate_task.add_container(
            f"{construct_id}-container",
            logging=ecs.AwsLogDriver(stream_prefix=construct_id),
            image=ecs.ContainerImage.from_registry(asset.image_uri),
            # secrets={
            #     "ADMIN_USER": ecs.Secret.from_secrets_manager(secret=mqtt_admin_creds, field="user"),
            #     "ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(secret=mqtt_admin_creds, field="password"),
            #     "SUB_USER": ecs.Secret.from_secrets_manager(secret=mqtt_sub_creds, field="user"),
            #     "SUB_PASSWORD": ecs.Secret.from_secrets_manager(secret=mqtt_sub_creds, field="password"),
            #     "PUB_USER": ecs.Secret.from_secrets_manager(secret=mqtt_pub_creds, field="user"),
            #     "PUB_PASSWORD": ecs.Secret.from_secrets_manager(secret=mqtt_pub_creds, field="password"),
            #     "EVERYONE_USER": ecs.Secret.from_secrets_manager(secret=mqtt_everyone_creds, field="user"),
            #     "EVERYONE_PASSWORD": ecs.Secret.from_secrets_manager(secret=mqtt_everyone_creds, field="password"),
            #     "EMQX_DASH_ADMIN_USER": ecs.Secret.from_secrets_manager(secret=emqx_dash_admin_creds, field="user"),
            #     "EMQX_DASH_ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(secret=emqx_dash_admin_creds, field="password")
            # },

            environment={
                "EMQX_CLUSTER__DISCOVERY_STRATEGY": "dns",
                "EMQX_CLUSTER__DNS__NAME": ".".join([discovery_a, discovery_namespace]),
                "EMQX_CLUSTER__DNS__RECORD_TYPE": "a",
                "ADMIN_USER": mqtt_admin_creds.get("user"),
                "ADMIN_PASSWORD": mqtt_admin_creds.get("password"),
                "SUB_USER": mqtt_sub_creds.get("user"),
                "SUB_PASSWORD": mqtt_sub_creds.get("password"),
                "PUB_USER": mqtt_pub_creds.get("user"),
                "PUB_PASSWORD": mqtt_pub_creds.get("password"),
                "EVERYONE_USER": mqtt_everyone_creds.get("user"),
                "EVERYONE_PASSWORD": mqtt_everyone_creds.get("password"),
                "EMQX_DASH_ADMIN_USER": emqx_dash_admin_creds.get("user"),
                "EMQX_DASH_ADMIN_PASSWORD": emqx_dash_admin_creds.get("password"),
            },
        )
        app_container.add_port_mappings(
            ecs.PortMapping(container_port=1883),
            ecs.PortMapping(container_port=9001),
            ecs.PortMapping(container_port=8083),
            ecs.PortMapping(container_port=8084),
            ecs.PortMapping(container_port=18083),
            ecs.PortMapping(container_port=4370),
            ecs.PortMapping(container_port=5370),
            ecs.PortMapping(container_port=5369),
            ecs.PortMapping(container_port=443),

        )

        self.cluster = ecs.Cluster(
            self,
            f'{construct_id}-cluster',
            vpc=vpc
        )

        self.service = ecs_patterns.NetworkMultipleTargetGroupsFargateService(
            self,
            f"{construct_id}-service",
            assign_public_ip=False,  # Place tasks in private subnets
            desired_count=3,
            # 3-task service - 67% allows 1 task to be replaced at a time
            min_healthy_percent=67,
            task_definition=fargate_task,
            cluster=self.cluster,
            load_balancers=[
                ecs_patterns.NetworkLoadBalancerProps(
                    name=f"{construct_id}-lb",
                    public_load_balancer=True,
                    listeners=[
                        ecs_patterns.NetworkListenerProps(
                            name=f"{construct_id}-default-listener",
                        )
                    ],
                )
            ],
            target_groups=[
                ecs_patterns.NetworkTargetProps(
                    container_port=18083
                ),
                ecs_patterns.NetworkTargetProps(
                    container_port=8083
                ),
                ecs_patterns.NetworkTargetProps(
                    container_port=1883
                ),
            ],
            cloud_map_options=ecs.CloudMapOptions(
                # Create A records - useful for AWS VPC network mode.
                dns_record_type=sd.DnsRecordType.A,
                cloud_map_namespace=service_discovery,
                name=discovery_a
            )

        )
        self.service.load_balancer.add_listener(
            id=f"{construct_id}-1883-tcp-listener",
            port=1883,
            protocol=elbv2.Protocol.TCP,
            default_target_groups=[self.service.target_groups[2]]
        )
        self.service.load_balancer.add_listener(
            id=f"{construct_id}-443-wss-listener",
            port=443,
            protocol=elbv2.Protocol.TLS,
            certificates=[elbv2.ListenerCertificate.from_arn(cert_arn)],
            default_target_groups=[self.service.target_groups[1]]
        )
        self.service.load_balancer.add_listener(
            id=f"{construct_id}-8883-tls-listener",
            port=8883,
            certificates=[elbv2.ListenerCertificate.from_arn(
                cert_arn
            )],
            protocol=elbv2.Protocol.TLS,
            default_target_groups=[self.service.target_groups[2]]
        )
        self.service.load_balancer.add_listener(
            id=f"{construct_id}-18083-tls-listener",
            port=18083,
            protocol=elbv2.Protocol.TLS,
            certificates=[elbv2.ListenerCertificate.from_arn(
                cert_arn
            )],
            default_target_groups=[self.service.target_groups[0]]
        )
        # Allow traffic from the load balancer
        self.service.service.connections.allow_from(self.service.load_balancer, ec2.Port.tcp(8083))
        self.service.service.connections.allow_from(self.service.load_balancer, ec2.Port.tcp(1883))
        self.service.service.connections.allow_from(self.service.load_balancer, ec2.Port.tcp(18083))

        # Allow tasks to communicate with each other
        # todo - is this necessary?
        security_group = self.service.service.connections.security_groups[0]
        # todo - pull this out to support GB whitelisting
        security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.all_traffic(),
            description="Allow all traffic between tasks"
        )
        # to share with other stacks/services
        # self.broker_url = fargate.load_balancer.load_balancer_dns_name

        # add to existing hosted zone
        # create alias DNS record
        wis2_zone = r53.HostedZone.from_lookup(self, id=zone_id, domain_name=zone_domain_name)
        # Use CfnRecordSet for better control over DNS record management
        cache_lb_alias = r53.CfnRecordSet(
            self, f"{construct_id}-dns-alias",
            hosted_zone_id=wis2_zone.hosted_zone_id,
            name=f"{a_record_name}.{zone_domain_name}",
            type="A",
            alias_target=r53.CfnRecordSet.AliasTargetProperty(
                dns_name=self.service.load_balancer.load_balancer_dns_name,
                hosted_zone_id=self.service.load_balancer.load_balancer_canonical_hosted_zone_id
            )
        )
        # get the lb alias host
        self.broker_url = ".".join([a_record_name, zone_domain_name])
        CfnOutput(self, "BrokerURL", value=self.broker_url)
        # admin console url
        CfnOutput(self, "AdminConsole", value=f"https://{a_record_name}.{zone_domain_name}:18083/#/dashboard")

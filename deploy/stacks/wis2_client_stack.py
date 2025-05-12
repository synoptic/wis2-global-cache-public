from os import path

from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_secretsmanager as sm,
    aws_logs as alogs
)
from constructs import Construct
from aws_cdk.aws_ecr_assets import DockerImageAsset, Platform


class Wis2ClientClusterStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, vpc_id: str = None, **kwargs) -> None:
        super().__init__(scope, construct_id,
                         description=f"Cluster for WMO/WIS2.0 Global Cache clients.",
                         **kwargs)

        # Use provided VPC ID or fall back to default VPC
        if vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "wis2-client-vpc", vpc_id=vpc_id)
        else:
            vpc = ec2.Vpc.from_lookup(self, "wis2-client-default-vpc", is_default=True)

        self.cluster = ecs.Cluster(
            self, f'{construct_id}-cluster',
            vpc=vpc
        )


class Wis2ClientStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, cluster: ecs.Cluster, broker_connection_secret_arn: str, queue_name: str, bucket_name:str, **kwargs) -> None:
        super().__init__(scope, construct_id,
                         description=f"Client to listen for messages from {broker_connection_secret_arn} for WMO/WIS2.0 Global Cache.",
                         **kwargs)
        vpc = cluster.vpc

        # Security group to allow MQTT traffic
        mqtt_security_group = ec2.SecurityGroup(self, f"{construct_id}-mqtt-security",
                                                vpc=vpc,
                                                description="Allow MQTT access",
                                                disable_inline_rules=True
                                                )

        # Build docker image
        asset = DockerImageAsset(self, f"{construct_id}-image",
                                 directory="../client",
                                 platform=Platform.LINUX_AMD64
                                 )

        execution_role = iam.Role(self, f"{construct_id}-execution-role",
                                  assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))

        execution_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy"))

        task_role = iam.Role(self, f"{construct_id}-task-role",
                             assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))

        # Allow task role access to secrets manager
        task_role.add_managed_policy(iam.ManagedPolicy.from_managed_policy_arn(self, "secrets-manager-policy",
                                                                               "arn:aws:iam::aws:policy"
                                                                               "/SecretsManagerReadWrite"))

        # Allow task role access to SQS
        task_role.add_managed_policy(iam.ManagedPolicy.from_managed_policy_arn(self, "sqs-policy",
                                                                               "arn:aws:iam::aws:policy"
                                                                               "/AmazonSQSFullAccess"))

        fargate_task = ecs.FargateTaskDefinition(self, f"{construct_id}-task", execution_role=execution_role,
                                                 cpu=512,
                                                 memory_limit_mib=1024,
                                                 task_role=task_role)

        # get wis2.0 global broker credentials from secrets manager
        global_broker_string = sm.Secret.from_secret_attributes(self, f"{construct_id}-broker-string",
                                                                    secret_complete_arn=broker_connection_secret_arn)

        app_container = fargate_task.add_container(
            f"{construct_id}-container",
            logging=ecs.AwsLogDriver(stream_prefix=construct_id, log_retention=alogs.RetentionDays.ONE_WEEK),

            image=ecs.ContainerImage.from_registry(asset.image_uri),
            essential=True,
            secrets={
                "WIS2_BROKER_CONNECTION": ecs.Secret.from_secrets_manager(secret=global_broker_string, field="connection_string"),
            }
        )

        # Set container env variables
        app_container.add_environment("QUEUE_NAME", queue_name)
        app_container.add_environment("BUCKET_NAME", bucket_name)

        self.service = fargate_service = ecs.FargateService(
            self, f"{construct_id}-service",
            assign_public_ip=True,
            desired_count=1,
            security_groups=[mqtt_security_group],
            task_definition=fargate_task,
            cluster=cluster
        )
        self.service_name = fargate_service.service_name

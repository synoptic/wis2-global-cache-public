from constructs import Construct
from aws_cdk import (
    Stack,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_sqs as sqs,
    aws_lambda_destinations as destinations,
    aws_lambda_event_sources as event_sources,
    aws_secretsmanager as sm,
    BundlingOptions,
    DockerImage, Duration,
    aws_ec2 as ec2,
    aws_logs as alogs,
    RemovalPolicy,
    aws_ssm as ssm
)


class Wis2ManagerLambdaStack(Stack):

    def __init__(self, scope: Construct, id: str,
                 broker_url: str,
                 queue_name: str,
                 queue_arn: str,
                 cache_bucket_name: str,
                 cache_bucket_region: str,
                 memory_footprint: int,
                 vpc_id: str = None,
                 subnet_ids: list = None,
                 publisher_secret_arn: str = None,
                 lambda_role_arn: str = None,
                 **kwargs) -> None:

        super().__init__(scope, id, **kwargs)
        mode = 'dev' if 'dev' in id else 'prod'

        # if dev in construct id, set dev mode
        if 'dev' in id:
            self.node.set_context('insights', False)
        else:
            self.node.set_context('insights', True)

        cache_endpoint = ssm.StringParameter.from_string_parameter_attributes(
            self, "redis-write-url",
            parameter_name=f"/{mode}/gc/redis/primary"
        ).string_value

        # Use provided secret ARN instead of hardcoding
        wis2_mqtt_publisher = sm.Secret.from_secret_attributes(
            self, "emqx_pub_creds",
            secret_complete_arn=publisher_secret_arn
        )

        # Get existing SQS queue from ARN
        wis2_lambda_queue = sqs.Queue.from_queue_arn(self, id=queue_name, queue_arn=queue_arn)

        # Use provided IAM Role ARN
        wis2_lambda_role = iam.Role.from_role_arn(
            self, 'WIS2ManagerLambdaRole',
            role_arn=lambda_role_arn
        )

        # Use provided VPC ID or default
        if vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "vpc", vpc_id=vpc_id)
        else:
            vpc = ec2.Vpc.from_lookup(self, "vpc", is_default=True)

        # Use provided subnet IDs
        private_subnets = []
        for i, subnet_id in enumerate(subnet_ids):
            private_subnet = ec2.Subnet.from_subnet_attributes(
                self, f"private-subnet-{i}",
                subnet_id=subnet_id
            )
            private_subnets.append(private_subnet)
        private_subnets = ec2.SubnetSelection(subnets=private_subnets)

        # Lambda function definition remains mostly the same
        wis2_lambda = _lambda.Function(
            self, 'WIS2ManagerLambda',
            runtime=_lambda.Runtime.PYTHON_3_10,
            memory_size=memory_footprint,
            architecture=_lambda.Architecture.ARM_64,
            code=_lambda.Code.from_asset('../manager_lambda',
                                         bundling=BundlingOptions(
                                             image=DockerImage.from_registry("python:3.10"),
                                             command=[
                                                 'bash', '-c',
                                                 'pip install -r requirements.txt -t /asset-output && cp -au . '
                                                 '/asset-output'
                                             ],
                                         )
                                         ),
            handler='wis2_lambda_consumer.msg_handler',
            vpc=vpc,
            vpc_subnets=private_subnets,
            role=wis2_lambda_role,
            environment={
                "dest_bucket_name": cache_bucket_name,
                "dest_bucket_region": cache_bucket_region,
                "MQTT_PUB_PASSWORD": wis2_mqtt_publisher.secret_value_from_json('password').unsafe_unwrap(),
                "MQTT_PUB_USER": wis2_mqtt_publisher.secret_value_from_json('user').unsafe_unwrap(),
                "MQTT_BROKER_HOST": broker_url,
                "CACHE_ENDPOINT": cache_endpoint
            },
            insights_version=_lambda.LambdaInsightsVersion.VERSION_1_0_119_0 if self.node.try_get_context(
                'insights') else None,
            dead_letter_queue_enabled=True,
            timeout=Duration.minutes(15),
            log_group=alogs.LogGroup(
                self, "Logs",
                log_group_name=f"/aws/lambda/{id}",
                retention=alogs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY
            )
        )

        event_source = event_sources.SqsEventSource(wis2_lambda_queue, batch_size=1,
                                                    report_batch_item_failures=True,
                                                    max_concurrency=500)
        wis2_lambda.add_event_source(event_source)
        self.lambda_function = wis2_lambda
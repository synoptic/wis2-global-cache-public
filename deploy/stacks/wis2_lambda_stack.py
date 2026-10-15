import json

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
                 report_by: str,
                 vpc_id: str = None,
                 subnet_ids: list = None,
                 publisher_secret: str = None,
                 lambda_role_arn: str = None,
                 include_insights: bool = False,
                 max_cache_bytes: int = None,
                 max_receive_count: int = None,
                 **kwargs) -> None:

        super().__init__(scope, id, **kwargs)
        mode = 'dev' if 'dev' in id else 'prod'

        cache_endpoint = ssm.StringParameter.from_string_parameter_attributes(
            self, "redis-write-url",
            parameter_name=f"/{mode}/gc/redis/primary"
        ).string_value

        # Use provided secret ARN instead of hardcoding
        # wis2_mqtt_publisher = sm.Secret.from_secret_complete_arn(
        #     self, "emqx_pub_creds",
        #     secret_complete_arn=publisher_secret_arn
        # )
        wis2_mqtt_publisher = json.loads(publisher_secret)

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

        log_group = alogs.LogGroup(
            self, "Logs",
            log_group_name=f"/aws/lambda/{id}",
            retention=alogs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY
        )
        # counts the GC_* markers logged by the manager Lambda error path (see README "Operations")
        self.log_metrics = {
            marker: log_group.add_metric_filter(
                marker, filter_pattern=alogs.FilterPattern.all_terms(marker),
                metric_namespace="WIS2GC", metric_name=marker, metric_value="1").metric(statistic="Sum")
            for marker in ("GC_RETRY", "GC_UNCLASSIFIED_FAILURE", "GC_TLS_VERIFY_FAILED")
        }

        # Lambda function definition remains mostly the same
        wis2_lambda = _lambda.Function(
            self, 'WIS2ManagerLambda',
            runtime=_lambda.Runtime.PYTHON_3_13,
            memory_size=memory_footprint,
            architecture=_lambda.Architecture.ARM_64,
            code=_lambda.Code.from_asset('../manager_lambda',
                                         bundling=BundlingOptions(
                                             image=DockerImage.from_registry("python:3.13"),
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
                "MQTT_PUB_PASSWORD": wis2_mqtt_publisher.get('password'),
                "MQTT_PUB_USER": wis2_mqtt_publisher.get('user'),
                "MQTT_BROKER_HOST": broker_url,
                "CACHE_ENDPOINT": cache_endpoint,
                "REPORT_BY": report_by,
                **({"MAX_CACHE_BYTES": str(max_cache_bytes)} if max_cache_bytes else {}),
                **({"SQS_MAX_RECEIVE_COUNT": str(max_receive_count)} if max_receive_count else {})
            },
            insights_version=_lambda.LambdaInsightsVersion.VERSION_1_0_498_0 if include_insights else None,
            dead_letter_queue_enabled=True,
            timeout=Duration.minutes(15),
            log_group=log_group
        )

        event_source = event_sources.SqsEventSource(wis2_lambda_queue, batch_size=1,
                                                    report_batch_item_failures=True,
                                                    max_concurrency=500)
        wis2_lambda.add_event_source(event_source)
        self.lambda_function = wis2_lambda
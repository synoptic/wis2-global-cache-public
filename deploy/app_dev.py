#!/usr/bin/env python3
import os
import aws_cdk as cdk
from dotenv import load_dotenv

from stacks.wis2_gc_dashboard import WIS2GCDashboardStack
from stacks.wis2_lambda_stack import Wis2ManagerLambdaStack
# from stacks.wis2_broker_stack import Wis2BrokerStack
from stacks.wis2_client_stack import Wis2ClientClusterStack
from stacks.wis2_client_stack import Wis2ClientStack
from stacks.wis2_sqs_stack import Wis2SQSStack
# from stacks.wis2_dynamodb_stack import Wis2DynamoDBStack
from stacks.wis2_s3_stack import Wis2S3Bucket
from stacks.wis2_redis_stack import RedisCacheStack
from stacks.wis2_metrics_lambda_stack import MetricsLambdaStack
from stacks.wis2_emqx_broker import EmqxBrokerStack

app = cdk.App()
env_tag = "dev"
cdk.Tags.of(app).add("cost-app", "wis2-gc-dev")

# Load Production environment variables
load_dotenv("dev.env")

app = cdk.App()

# Load environment variables
env = {
    'account': os.getenv('AWS_ACCOUNT_ID'),
    'region': os.getenv('AWS_REGION'),
}
dev_gb_connection_string = os.getenv('DEV_GB_CONNECTION_STRING')
lambda_role_arn = os.getenv('LAMBDA_ROLE_ARN')
hosted_zone_domain_name = os.getenv('HOSTED_ZONE_DOMAIN_NAME')
hosted_zone_id = os.getenv('HOSTED_ZONE_ID')
# destination_bucket_name = os.getenv('DESTINATION_BUCKET_NAME')
# dest_bucket_region = os.getenv('DEST_BUCKET_REGION')
a_record_name = os.getenv('A_RECORD_NAME')
static_broker_url = ".".join([a_record_name, hosted_zone_domain_name])
metrics_record_name = os.getenv('METRICS_RECORD_NAME')
redis_instance_type = os.getenv('REDIS_INSTANCE_TYPE')
cert_arn = os.getenv('CERT_ARN')
subnet_ids = [os.getenv('SUBNET_ID_1'), os.getenv('SUBNET_ID_2')]
vpc_id = os.getenv('VPC_ID')

redis_instance_type = "cache.t3.micro"
lambda_memory = 128
env_tag = "dev"

# bucket to cache messages in their entirety for dev/debug
# msg_cache_bucket = Wis2S3Bucket(app, f"{env_tag}-msg-cache", role_arn=lambda_role_arn, env=env)
# Destination bucket (for dev/testing)
dest_bucket_stack = Wis2S3Bucket(app, f"{env_tag}S3", role_arn=lambda_role_arn, public_read=True,
                                 ttl_days=1, env=env)

wis2_sqs_stack = Wis2SQSStack(app, f"{env_tag}-wis2-sqs-work", env=env)
# Create cluster for all clients to be part of
wis2_client_cluster = Wis2ClientClusterStack(app, f"{env_tag}-wis2-client-cluster", env=env)

# Create clients
dev_client_stack = Wis2ClientStack(app, f"{env_tag}-wis2-client-dev-test", cluster=wis2_client_cluster.cluster,
                                  broker_connection_string=dev_gb_connection_string,
                                  queue_name=wis2_sqs_stack.queue_name, bucket_name="cache-everything",
                                  env=env)
dev_client_stack.add_dependency(wis2_client_cluster)
dev_client_stack.add_dependency(wis2_sqs_stack)
# example for other global broker clients:
# fr_client_stack_2 = Wis2ClientStack(app, f"{env_tag}-wis2-client-france-2", cluster=wis2_client_cluster.cluster,
#                                     broker_connection_secret_arn=dev_fr_broker_secret_arn,
#                                     queue_name=wis2_sqs_stack.queue_name,
#                                     env=env)
# Broker
emqx_broker_stack = EmqxBrokerStack(
    app, f"{env_tag}-emqx-broker",
    zone_domain_name=hosted_zone_domain_name,
    zone_id=hosted_zone_id,
    a_record_name=a_record_name,
    cert_arn=cert_arn,
    vpc_id=vpc_id,
    admin_creds=os.getenv('ADMIN_CREDS'),
    emqx_dash_admin_creds=os.getenv('EMQX_DASH_ADMIN_CREDS'),
    publisher_creds=os.getenv('PUBLISHER_CREDS'),
    everyone_creds=os.getenv('EVERYONE_CREDS'),
    subscriber_creds=os.getenv('SUBSCRIBER_CREDS'),
    memory_footprint=512,
    env=env
)


# redis stack
redis_stack = RedisCacheStack(app, f"{env_tag}-cache-stack", vpc_id=vpc_id, subnet_ids=subnet_ids,
                              instance_type=redis_instance_type, read_replicas=0, env=env)

# Lambda stack
manager_lambda_stack = Wis2ManagerLambdaStack(
    app, f"{env_tag}-wis2-manager-lambda",
    static_broker_url,
    queue_name=wis2_sqs_stack.node.id,
    queue_arn=wis2_sqs_stack.queue_arn,
    cache_bucket_name=dest_bucket_stack.bucket.bucket_name,
    cache_bucket_region=env['region'],
    memory_footprint=lambda_memory,
    vpc_id=vpc_id,
    subnet_ids=subnet_ids,
    publisher_secret=os.getenv('PUBLISHER_CREDS'),
    lambda_role_arn=lambda_role_arn,
    include_insights=True,
    env=env
)
manager_lambda_stack.add_dependency(wis2_sqs_stack)
manager_lambda_stack.add_dependency(redis_stack)

# Metrics lambda
metrics_lambda_stack = MetricsLambdaStack(
    app, f"{env_tag}-wis2-metrics-lambda",
    hosted_zone_id=hosted_zone_id,
    hosted_zone_domain_name=hosted_zone_domain_name,
    metrics_record_name=metrics_record_name,
    private_subnet_ids=subnet_ids,
    cert_arn=cert_arn,
    vpc_id=vpc_id,
    memory_size=128,
    report_by=os.environ.get('REPORT_BY', 'data-metoffice-noaa-global-cache'),
    env=env
)

# Create dashboard stack with resource names
WIS2GCDashboardStack(
    app, 'WIS2GCDashboard',
    manager_lambda_name=manager_lambda_stack.lambda_function.function_name,
    work_queue_name=wis2_sqs_stack.queue_name,
    dlq_name=wis2_sqs_stack.dlq_name,
    client_services = [dev_client_stack.service],
    broker_ecs_service_name=emqx_broker_stack.service.service.service_name,
    broker_ecs_cluster_name=emqx_broker_stack.cluster.cluster_name,
    env=env
)

cdk.Tags.of(app).add("cost-app", "wis2-gc-dev")

app.synth()

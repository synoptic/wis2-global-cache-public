#!/usr/bin/env python3
import os
import aws_cdk as cdk
from dotenv import load_dotenv
from stacks.wis2_emqx_broker import EmqxBrokerStack
from stacks.wis2_lambda_stack import Wis2ManagerLambdaStack
from stacks.wis2_client_stack import Wis2ClientClusterStack
from stacks.wis2_client_stack import Wis2ClientStack
from stacks.wis2_sqs_stack import Wis2SQSStack
from stacks.wis2_redis_stack import RedisCacheStack
from stacks.wis2_metrics_lambda_stack import MetricsLambdaStack
from stacks.wis2_gc_dashboard import WIS2GCDashboardStack

# Load Production environment variables
load_dotenv("prod.env")

app = cdk.App()

env = {
    'account': os.getenv('AWS_ACCOUNT_ID'),
    'region': os.getenv('AWS_REGION'),
}

fr_broker_secret_arn = os.getenv('FR_BROKER_SECRET_ARN')
br_broker_secret_arn = os.getenv('BR_BROKER_SECRET_ARN')
nws_noaa_broker_secret_arn = os.getenv('NWS_NOAA_BROKER_SECRET_ARN')
dev_gb_broker_secret_arn = os.getenv('DEV_GB_BROKER_SECRET_ARN')

lambda_role_arn = os.getenv('LAMBDA_ROLE_ARN')
hosted_zone_domain_name = os.getenv('HOSTED_ZONE_DOMAIN_NAME')
hosted_zone_id = os.getenv('HOSTED_ZONE_ID')
destination_bucket_name = os.getenv('DESTINATION_BUCKET_NAME')
dest_bucket_region = os.getenv('DEST_BUCKET_REGION')
a_record_name = os.getenv('A_RECORD_NAME')
static_broker_url = ".".join([a_record_name, hosted_zone_domain_name])
metrics_record_name = os.getenv('METRICS_RECORD_NAME')
redis_instance_type = os.getenv('REDIS_INSTANCE_TYPE')
lambda_memory = int(os.getenv('LAMBDA_MEMORY'))

cert_arn = os.getenv('CERT_ARN')
subnet_ids = [os.getenv('SUBNET_ID_1'), os.getenv('SUBNET_ID_2')]
vpc_id = os.getenv('VPC_ID')

# SQS stack for queueing
wis2_sqs_stack = Wis2SQSStack(app, "wis2-sqs-work", env=env)

# redis stack for metrics and deduplication
redis_stack = RedisCacheStack(app, "wis2-redis-cache", vpc_id=vpc_id, subnet_ids=subnet_ids,
                              instance_type=redis_instance_type, read_replicas=1, env=env)

# Create cluster for all clients to be part of
wis2_client_cluster = Wis2ClientClusterStack(app, "wis2-client-cluster", vpc_id=vpc_id, env=env)

# Global Broker - France
fr_client_stack = Wis2ClientStack(app, "wis2-client-france", cluster=wis2_client_cluster.cluster,
                                  broker_connection_secret_arn=fr_broker_secret_arn,
                                  queue_name=wis2_sqs_stack.queue_name, bucket_name=destination_bucket_name,
                                  env=env)
fr_client_stack.add_dependency(wis2_client_cluster)
fr_client_stack.add_dependency(wis2_sqs_stack)

# Global Broker - Brazil
br_client_stack = Wis2ClientStack(app, "wis2-client-brazil", cluster=wis2_client_cluster.cluster,
                                  broker_connection_secret_arn=br_broker_secret_arn,
                                  queue_name=wis2_sqs_stack.queue_name, bucket_name=destination_bucket_name,
                                  env=env)
br_client_stack.add_dependency(wis2_client_cluster)
br_client_stack.add_dependency(wis2_sqs_stack)

# Global Broker - NWS NOAA
nws_client_stack = Wis2ClientStack(app, "wis2-client-nws-noaa", cluster=wis2_client_cluster.cluster,
                                  broker_connection_secret_arn=nws_noaa_broker_secret_arn,
                                  queue_name=wis2_sqs_stack.queue_name, bucket_name=destination_bucket_name,
                                  env=env)
nws_client_stack.add_dependency(wis2_client_cluster)
nws_client_stack.add_dependency(wis2_sqs_stack)

# Broker
emqx_broker_stack = EmqxBrokerStack(
    app, "prod-emqx-broker",
    zone_domain_name=hosted_zone_domain_name,
    zone_id=hosted_zone_id,
    a_record_name=a_record_name,
    cert_arn=cert_arn,
    vpc_id=vpc_id,
    admin_creds_arn=os.getenv('ADMIN_CREDS_ARN'),
    emqx_dash_admin_creds_arn=os.getenv('EMQX_DASH_ADMIN_CREDS_ARN'),
    publisher_creds_arn=os.getenv('PUBLISHER_CREDS_ARN'),
    everyone_creds_arn=os.getenv('EVERYONE_CREDS_ARN'),
    subscriber_creds_arn=os.getenv('SUBSCRIBER_CREDS_ARN'),
    memory_footprint=512,
    env=env
)

# Lambda stack
manager_lambda_stack = Wis2ManagerLambdaStack(
    app, "wis2-manager-lambda",
    static_broker_url,
    queue_name=wis2_sqs_stack.node.id,
    queue_arn=wis2_sqs_stack.queue_arn,
    cache_bucket_name=destination_bucket_name,
    cache_bucket_region=dest_bucket_region,
    memory_footprint=lambda_memory,
    vpc_id=vpc_id,
    subnet_ids=subnet_ids,
    publisher_secret_arn=os.getenv('PUBLISHER_CREDS_ARN'),
    lambda_role_arn=lambda_role_arn,
    env=env
)
manager_lambda_stack.add_dependency(wis2_sqs_stack)
manager_lambda_stack.add_dependency(redis_stack)

# Metrics lambda
metrics_lambda_stack = MetricsLambdaStack(
    app, "wis2-metrics-lambda",
    hosted_zone_id=hosted_zone_id,
    hosted_zone_domain_name=hosted_zone_domain_name,
    metrics_record_name=metrics_record_name,
    private_subnet_ids=subnet_ids,
    cert_arn=cert_arn,
    vpc_id=vpc_id,
    memory_size=128,
    env=env
)

# Create dashboard stack with resource names
WIS2GCDashboardStack(
    app, 'WIS2GCDashboard',
    manager_lambda_name=manager_lambda_stack.lambda_function.function_name,
    work_queue_name=wis2_sqs_stack.queue_name,
    dlq_name=wis2_sqs_stack.dlq_name,
    client_services = [fr_client_stack.service, br_client_stack.service, nws_client_stack.service],
    broker_ecs_service_name=emqx_broker_stack.service.service.service_name,
    broker_ecs_cluster_name=emqx_broker_stack.cluster.cluster_name,
    env=env
)
cdk.Tags.of(app).add("cost-app", "wis2-gc-prod")

app.synth()
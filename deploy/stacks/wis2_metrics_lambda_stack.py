from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_lambda as lambda_, BundlingOptions,
    aws_apigateway as apigateway, CfnOutput,
    aws_route53 as route53, aws_route53_targets as r53_targets,
    aws_certificatemanager as acm, Duration,
    aws_ssm as ssm
)
from constructs import Construct


class MetricsLambdaStack(Stack):
    def __init__(self, scope: Construct, construct_id: str,
                 hosted_zone_id: str,
                 hosted_zone_domain_name: str,
                 metrics_record_name: str,
                 private_subnet_ids: list,
                 cert_arn: str,
                 vpc_id: str = None,
                 memory_size: int = 128,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        get_resource = "metrics"
        mode = 'dev' if 'dev' in construct_id else 'prod'
        redis_endpoint = ssm.StringParameter.from_string_parameter_attributes(
            self, "redis-read-url",
            parameter_name=f"/{mode}/gc/redis/read"
        ).string_value

        # Use provided subnet IDs
        private_subnets = []
        for i, subnet_id in enumerate(private_subnet_ids):
            private_subnet = ec2.Subnet.from_subnet_attributes(
                self, f"private-subnet-{i}",
                subnet_id=subnet_id
            )
            private_subnets.append(private_subnet)
        private_subnets = ec2.SubnetSelection(subnets=private_subnets)

        # Use the provided VPC ID or default
        vpc = ec2.Vpc.from_lookup(self, "vpc",
                                 vpc_id=vpc_id)

        metrics_function = lambda_.Function(
            self, f"{construct_id}-function",
            code=lambda_.Code.from_asset(
                '../metrics_lambda',
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_10.bundling_image,
                    command=[
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output"
                    ]
                )
            ),
            runtime=lambda_.Runtime.PYTHON_3_10,
            handler="gc_metrics_handler.handler",
            vpc=vpc,
            vpc_subnets=private_subnets,
            environment={'REDIS_ENDPOINT': redis_endpoint},
            memory_size=memory_size
        )

        api = apigateway.LambdaRestApi(
            self,
            f"{construct_id}-api",
            handler=metrics_function,
            proxy=False,
            deploy_options=apigateway.StageOptions(
                throttling_rate_limit=5,
                throttling_burst_limit=10,
                caching_enabled=True,
                cache_ttl=Duration.seconds(60),
                cache_data_encrypted=False
            )
        )

        hello_resource = api.root.add_resource(get_resource)
        hello_resource.add_method("GET")

        wis2_zone = route53.HostedZone.from_lookup(
            self, id=hosted_zone_id, domain_name=hosted_zone_domain_name)

        # Use provided certificate ARN
        certificate = acm.Certificate.from_certificate_arn(
            self, 'existing-cert', cert_arn)

        api.add_domain_name(
            id=f"{construct_id}-wis2-gc-metrics-domain",
            certificate=certificate,
            domain_name=f"{metrics_record_name}.{hosted_zone_domain_name}"
        )

        route53.ARecord(
            self, f"{construct_id}-metrics-record",
            zone=wis2_zone,
            target=route53.RecordTarget.from_alias(r53_targets.ApiGateway(api)),
            record_name=metrics_record_name,
            delete_existing=True
        )

        CfnOutput(
            self, "MetricsAPI",
            value=f"https://{metrics_record_name}.{hosted_zone_domain_name}/{get_resource}"
        )
from aws_cdk import Stack
from constructs import Construct
# import ec2
from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
)

class Wis2NetworkingStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create VPC to host all application components
        self.vpc = ec2.Vpc(
            self, "WIS2VPC",
            vpc_name="wis2-vpc",
            max_azs=2,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24
                )
            ],
            nat_gateways=1
        )

        # Create VPC Endpoints
        # S3 Gateway Endpoint
        self.s3_endpoint = self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3
        )

        # CloudWatch Logs Interface Endpoint
        self.logs_endpoint = self.vpc.add_interface_endpoint(
            "CloudWatchLogsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS
        )

        # Create Lambda security group
        self.lambda_sg = ec2.SecurityGroup(
            self, "LambdaSecurityGroup",
            vpc=self.vpc,
            description="Security group for Lambda functions"
        )
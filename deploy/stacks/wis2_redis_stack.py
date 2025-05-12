from aws_cdk import (
    # Duration,
    Stack,
    # aws_sqs as sqs,
    aws_s3 as s3,
    RemovalPolicy,
    aws_elasticache as elasticache,
    aws_ec2 as ec2, CfnOutput,
    aws_ssm as ssm
)
from constructs import Construct


class RedisCacheStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, vpc_id: str, subnet_ids: list, instance_type:str, read_replicas: int=1, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        subnet_group = elasticache.CfnSubnetGroup(
            scope=self,
            id=f"{construct_id}-subnet-group",
            description="Subnet group for Redis cluster",
            subnet_ids=subnet_ids,
        )
        mode = 'dev' if 'dev' in construct_id else 'prod'

        # get vpc from vpc_id
        vpc = ec2.Vpc.from_lookup(self, "vpc", vpc_id=vpc_id)

        # create source security group for access
        source_security_group = ec2.SecurityGroup(self, f"{construct_id}-WalledGarden",
                                                  vpc=vpc,
                                                  description="Allow access to Redis cluster",
                                                  allow_all_outbound=True
                                                  )
        source_security_group.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(6379),
            'allow access to redis port')

        # Elasticache for Redis cluster (replication enabled)
        self.cluster = elasticache.CfnReplicationGroup(
            scope=self,
            replication_group_description=f"{mode} Redis cache for deduplication and metrics",
            replication_group_id=f"{mode}-gc-cache",
            id=f"{construct_id}-redis",
            engine="redis",
            cache_node_type=instance_type,
            replicas_per_node_group=read_replicas,
            cache_subnet_group_name=subnet_group.ref,
            at_rest_encryption_enabled=False,
            transit_encryption_enabled=False,
            cluster_mode="disabled",
            automatic_failover_enabled=True if read_replicas >= 1 else False,
            multi_az_enabled=True if read_replicas >= 1 else False,
            security_group_ids=[source_security_group.security_group_id],
        )
        self.redis_primary = CfnOutput(
            scope=self,
            id=f"{construct_id}-redis-primary-output",
            value=self.cluster.attr_primary_end_point_address,
        ).value

        if read_replicas >= 1:
            self.redis_read = CfnOutput(
                scope=self,
                id=f"{construct_id}-redis-read-output",
                value=self.cluster.attr_reader_end_point_address,
            ).value
        else:
            self.redis_read = self.redis_primary
        # Save redis url in SSM for other stacks to reference
        ssm.StringParameter(
            self,
            f"{construct_id}-redis-url-primary",
            parameter_name=f"/{mode}/gc/redis/primary",
            description="Redis primary url for wis2 global cache stack",
            string_value=self.redis_primary,
        )

        ssm.StringParameter(
            self,
            f"{construct_id}-redis-url-read",
            parameter_name=f"/{mode}/gc/redis/read",
            description="Redis read only url for wis2 global cache stack",
            string_value=self.redis_read,
        )
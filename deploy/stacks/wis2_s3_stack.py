import os

from constructs import Construct
from aws_cdk import (
    Stack,
    aws_s3 as s3,
    RemovalPolicy,
    aws_iam as iam,
    Duration
)


class Wis2S3Bucket(Stack):
    def __init__(self, scope: Construct, id: str, role_arn:str, public_read=False, ttl_days:int=1, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)
        # Create an S3 bucket shared publicly to cache WIS2.0 data
        self.bucket = s3.Bucket(self, id,
                                versioned=True,
                                removal_policy=RemovalPolicy.DESTROY,
                                auto_delete_objects=True,
                                block_public_access=s3.BlockPublicAccess(block_public_acls=False,
                                                                         block_public_policy=False,
                                                                         ignore_public_acls=False,
                                                                         restrict_public_buckets=False),
                                public_read_access=public_read
                                )
        # Add a bucket policy to allow access from the WIS2.0 Manager Lambda role via role_arn
        self.bucket.grant_put(iam.Role.from_role_arn(self, "wis2-lambda-manager-role", role_arn))

        # allow put and get objects for any authenticated user in the account
        self.bucket.grant_read(iam.AccountPrincipal(f"{os.getenv('CDK_DEFAULT_ACCOUNT')}"))
        self.bucket.grant_write(iam.AccountPrincipal(f"{os.getenv('CDK_DEFAULT_ACCOUNT')}"))


        # add ttl to the bucket objects to delete after 24 hours
        self.bucket.add_lifecycle_rule(
            expiration=Duration.days(ttl_days),
        )
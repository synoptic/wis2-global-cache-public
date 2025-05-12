from constructs import Construct
from aws_cdk import (
    Stack,
    aws_sqs as sqs,
    Duration, RemovalPolicy,
)


class Wis2SQSStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        wis2_work_dlq = sqs.Queue(self, "WIS2GlobalCacheDLQ",
                                  removal_policy=RemovalPolicy.RETAIN,
                                  fifo=True,
                                  content_based_deduplication=True,
                                  encryption=sqs.QueueEncryption.UNENCRYPTED,
                                  retention_period=Duration.days(2))

        wis2_work_queue = sqs.Queue(self, "WIS2GlobalCacheQueue",
                                    # this removal policy migrates queued msgs to the new queue on redeployment
                                    removal_policy=RemovalPolicy.RETAIN,
                                    fifo=True,
                                    retention_period=Duration.hours(24),
                                    receive_message_wait_time = Duration.seconds(2),
                                    # visibility cannot be less than lambda function timeout
                                    visibility_timeout=Duration.minutes(15),
                                    dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=2, queue=wis2_work_dlq),
                                    content_based_deduplication=True,
                                    # setting deduplication scope to message group (this is specified at time of queueing by the mqtt client)
                                    deduplication_scope=sqs.DeduplicationScope.MESSAGE_GROUP,
                                    fifo_throughput_limit=sqs.FifoThroughputLimit.PER_MESSAGE_GROUP_ID,
                                    encryption=sqs.QueueEncryption.UNENCRYPTED,
                                    )

        self.queue_arn = wis2_work_queue.queue_arn
        self.queue_name = wis2_work_queue.queue_name
        self.dlq_name = wis2_work_dlq.queue_name
        self.queue_url = wis2_work_queue.queue_url

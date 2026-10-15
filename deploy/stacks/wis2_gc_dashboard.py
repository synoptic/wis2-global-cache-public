from aws_cdk import (
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    Duration
)
from constructs import Construct


class WIS2GCDashboardStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        # Extract resource names from kwargs
        manager_lambda_name = kwargs.pop("manager_lambda_name", None)
        work_queue_name = kwargs.pop("work_queue_name", None)
        dlq_name = kwargs.pop("dlq_name", None)
        broker_ecs_service_name = kwargs.pop("broker_ecs_service_name", None)
        broker_ecs_cluster_name = kwargs.pop("broker_ecs_cluster_name", None)
        client_services = kwargs.pop("client_services", None)
        manager_log_metrics = kwargs.pop("manager_log_metrics", None)
        alarm_email = kwargs.pop("alarm_email", None)

        super().__init__(scope, id, **kwargs)
        period_minutes = 5

        # Create dashboard
        dashboard = cloudwatch.Dashboard(self, 'WIS2OperationsDashboard',
                                         dashboard_name='WIS2-Operations-Dashboard')

        # Add ECS service metrics for CPU and Memory utilization
        if client_services:
            cpu_metrics = []
            memory_metrics = []

            for service in client_services:
                service_name = service.service_name
                cluster_name = service.cluster.cluster_name

                cpu_metric = cloudwatch.Metric(
                    namespace="AWS/ECS",
                    metric_name="CPUUtilization",
                    dimensions_map={
                        "ServiceName": service_name,
                        "ClusterName": cluster_name
                    },
                    statistic="Average",
                    period=Duration.minutes(period_minutes),
                    label=f"{service_name} CPU"
                )
                cpu_metrics.append(cpu_metric)

                memory_metric = cloudwatch.Metric(
                    namespace="AWS/ECS",
                    metric_name="MemoryUtilization",
                    dimensions_map={
                        "ServiceName": service_name,
                        "ClusterName": cluster_name
                    },
                    statistic="Average",
                    period=Duration.minutes(period_minutes),
                    label=f"{service_name} Memory"
                )
                memory_metrics.append(memory_metric)

            # Add CPU and Memory widgets
            dashboard.add_widgets(
                cloudwatch.GraphWidget(
                    title="ECS Services CPU Utilization",
                    width=12,
                    height=6,
                    left=cpu_metrics
                ),
                cloudwatch.GraphWidget(
                    title="ECS Services Memory Utilization",
                    width=12,
                    height=6,
                    left=memory_metrics
                )
            )

            # SQS queue metrics
            if work_queue_name:
                queue_messages_visible = cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="ApproximateNumberOfMessagesVisible",
                    dimensions_map={"QueueName": work_queue_name},
                    statistic="Average",
                    period=Duration.minutes(period_minutes)
                )

                queue_messages_sent = cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="NumberOfMessagesSent",
                    dimensions_map={"QueueName": work_queue_name},
                    statistic="Sum",
                    period=Duration.minutes(period_minutes),
                    label="Messages Sent"
                )

                queue_messages_received = cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="NumberOfMessagesReceived",
                    dimensions_map={"QueueName": work_queue_name},
                    statistic="Sum",
                    period=Duration.minutes(period_minutes),
                    label="Messages Received"
                )

                queue_deduplicated_messages = cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="NumberOfDeduplicatedSentMessages",
                    dimensions_map={"QueueName": work_queue_name},
                    statistic="Sum",
                    period=Duration.minutes(period_minutes),
                    label="Deduplicated Messages"
                )

                dashboard.add_widgets(
                    cloudwatch.GraphWidget(
                        title="SQS Approximate Number Of Messages Visible",
                        width=12,
                        height=6,
                        left=[queue_messages_visible]
                    ),
                    cloudwatch.GraphWidget(
                        title="SQS Deduplicated Messages",
                        width=12,
                        height=6,
                        left=[queue_deduplicated_messages]
                    )

                )

                dashboard.add_widgets(
                    cloudwatch.GraphWidget(
                        title="SQS Messages Sent vs Received",
                        width=24,
                        height=6,
                        left=[queue_messages_sent, queue_messages_received]
                    )
                )

        # Lambda invocations widget using function name
        if manager_lambda_name:
            lambda_invocations = cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Invocations",
                dimensions_map={"FunctionName": manager_lambda_name},
                statistic="Sum",
                period=Duration.minutes(period_minutes)
            )

            lambda_errors = cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Errors",
                dimensions_map={"FunctionName": manager_lambda_name},
                statistic="Sum",
                period=Duration.minutes(period_minutes)
            )

            # Add both Lambda widgets in the same row
            dashboard.add_widgets(
                cloudwatch.GraphWidget(
                    title="WIS2 Manager Lambda Invocations",
                    width=12,
                    height=6,
                    left=[lambda_invocations]
                ),
                cloudwatch.GraphWidget(
                    title="Lambda Error count and success rate (%)",
                    width=12,
                    height=6,
                    left=[lambda_errors],
                    right=[
                        cloudwatch.MathExpression(
                            expression="100 - 100 * errors / MAX([errors, invocations])",
                            label="Success rate (%)",
                            using_metrics={
                                "errors": lambda_errors,
                                "invocations": lambda_invocations
                            }
                        )
                    ]
                )
            )

            # Lambda duration metric for cost calculation
            lambda_duration = cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Duration",
                dimensions_map={"FunctionName": manager_lambda_name},
                statistic="Average",
                period=Duration.minutes(period_minutes),
                label="Duration (ms)"
            )

            # Lambda Insights Memory Utilization metrics
            lambda_memory_util_avg = cloudwatch.Metric(
                namespace="LambdaInsights",
                metric_name="memory_utilization",
                dimensions_map={"function_name": manager_lambda_name},
                statistic="Average",
                period=Duration.minutes(period_minutes),
                label="Memory Util Avg (%)"
            )

            lambda_memory_util_max = cloudwatch.Metric(
                namespace="LambdaInsights",
                metric_name="memory_utilization",
                dimensions_map={"function_name": manager_lambda_name},
                statistic="Maximum",
                period=Duration.minutes(period_minutes),
                label="Memory Util Max (%)"
            )

            # Total memory from Lambda Insights
            lambda_total_memory = cloudwatch.Metric(
                namespace="LambdaInsights",
                metric_name="total_memory",
                dimensions_map={"function_name": manager_lambda_name},
                statistic="Maximum",
                period=Duration.minutes(period_minutes),
                label="Total Memory (MB)"
            )

            # Calculate GB-seconds cost
            gb_seconds_expression = cloudwatch.MathExpression(
                expression="(duration / 1000) * (memory / 1024)",
                label="GB-seconds (Cost metric)",
                using_metrics={
                    "duration": lambda_duration,
                    "memory": lambda_total_memory
                }
            )
            # Lambda invocations metric for cost calculation
            lambda_invocations = cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Invocations",
                dimensions_map={"FunctionName": manager_lambda_name},
                statistic="Sum",
                period=Duration.minutes(period_minutes),
                label="Invocations"
            )

            # Calculate total GB-seconds cost including invocations
            total_gb_seconds_expression = cloudwatch.MathExpression(
                expression="(duration / 1000) * (memory / 1024) * invocations",
                label="Total GB-seconds (Cost)",
                using_metrics={
                    "duration": lambda_duration,
                    "memory": lambda_total_memory,
                    "invocations": lambda_invocations
                }
            )
            # Updated widget to show both per-invocation and total GB-seconds
            dashboard.add_widgets(
                cloudwatch.GraphWidget(
                    title="Lambda Cost (GB/Sec)",
                    width=12,
                    height=6,
                    left=[
                        lambda_duration,
                        total_gb_seconds_expression  # Total GB-seconds (actual cost)
                    ]
                ),
                cloudwatch.GraphWidget(
                    title="Lambda Memory Utilization",
                    width=12,
                    height=6,
                    left=[lambda_total_memory,  # Total memory (MB),
                          lambda_memory_util_avg,
                          lambda_memory_util_max],
                )
            )

        if manager_log_metrics or dlq_name:
            widgets = []
            if manager_log_metrics:
                widgets.append(cloudwatch.GraphWidget(
                    title="Manager Lambda failures (GC_* log markers)",
                    width=12,
                    height=6,
                    left=[m.with_(period=Duration.minutes(period_minutes), label=name)
                          for name, m in manager_log_metrics.items()]
                ))
            if dlq_name:
                # records still failing after the SQS retry land here
                widgets.append(cloudwatch.GraphWidget(
                    title="DLQ messages visible (retries exhausted)",
                    width=12,
                    height=6,
                    left=[cloudwatch.Metric(
                        namespace="AWS/SQS",
                        metric_name="ApproximateNumberOfMessagesVisible",
                        dimensions_map={"QueueName": dlq_name},
                        statistic="Maximum",
                        period=Duration.minutes(period_minutes)
                    )]
                ))
            dashboard.add_widgets(*widgets)

        if broker_ecs_service_name and broker_ecs_cluster_name:
            # Add ECS service metrics for CPU and Memory utilization
            if client_services:
                cpu_metrics = []
                memory_metrics = []

                cpu_metric = cloudwatch.Metric(
                    namespace="AWS/ECS",
                    metric_name="CPUUtilization",
                    dimensions_map={
                        "ServiceName": broker_ecs_service_name,
                        "ClusterName": broker_ecs_cluster_name
                    },
                    statistic="Average",
                    period=Duration.minutes(period_minutes),
                    label=f"{broker_ecs_service_name} CPU"
                )
                cpu_metrics.append(cpu_metric)

                memory_metric = cloudwatch.Metric(
                    namespace="AWS/ECS",
                    metric_name="MemoryUtilization",
                    dimensions_map={
                        "ServiceName": broker_ecs_service_name,
                        "ClusterName": broker_ecs_cluster_name
                    },
                    statistic="Average",
                    period=Duration.minutes(period_minutes),
                    label=f"{broker_ecs_service_name} Memory"
                )
                memory_metrics.append(memory_metric)

            # Add CPU and Memory widgets
            dashboard.add_widgets(
                cloudwatch.GraphWidget(
                    title="MQTT Broker ECS Services CPU Utilization",
                    width=12,
                    height=6,
                    left=cpu_metrics
                ),
                cloudwatch.GraphWidget(
                    title="MQTT Broker ECS Services Memory Utilization",
                    width=12,
                    height=6,
                    left=memory_metrics
                )
            )

        if alarm_email:
            # unencrypted on purpose: CloudWatch cannot publish to a topic that uses the AWS-managed SNS key
            alarm_topic = sns.Topic(self, "AlarmTopic", display_name="WIS2 GC alarms")
            alarm_topic.add_subscription(subs.EmailSubscription(alarm_email))
            def sqs_metric(queue, name):
                return cloudwatch.Metric(namespace="AWS/SQS", metric_name=name, dimensions_map={"QueueName": queue},
                                         statistic="Maximum", period=Duration.minutes(period_minutes))
            hour = Duration.hours(1)
            # (name, description, metric, threshold, missing data, email on clear)
            alarm_specs = [
                # SQS stops publishing metrics for an idle queue, and nothing reads the DLQ, so keep the last state
                ("DLQ-not-empty", "Records failed every SQS attempt and were not cached (see README 4.4)",
                 sqs_metric(dlq_name, "ApproximateNumberOfMessagesVisible"), 1,
                 cloudwatch.TreatMissingData.IGNORE, True),
                # handled record failures are not Lambda Errors; these are crashes, timeouts and out-of-memory
                ("Manager-Lambda-errors", "Manager Lambda invocations crashed, timed out or ran out of memory",
                 cloudwatch.Metric(namespace="AWS/Lambda", metric_name="Errors",
                                   dimensions_map={"FunctionName": manager_lambda_name},
                                   statistic="Sum", period=Duration.minutes(period_minutes)), 1,
                 cloudwatch.TreatMissingData.NOT_BREACHING, True),
                # retried records wait 15-30 min in their FIFO group, so ages below 1 h are normal
                ("Work-queue-stalled", "Oldest work queue message is over 1 hour old",
                 sqs_metric(work_queue_name, "ApproximateAgeOfOldestMessage"), 3600,
                 cloudwatch.TreatMissingData.NOT_BREACHING, True),
                # metric filters publish nothing when no line matches. 1 h period and no clear email so a data
                # server that fails intermittently does not send an alarm/clear pair per item
                ("Unclassified-failures", "GC_UNCLASSIFIED_FAILURE logged: unexpected error, record not retried",
                 manager_log_metrics["GC_UNCLASSIFIED_FAILURE"].with_(period=hour), 1,
                 cloudwatch.TreatMissingData.NOT_BREACHING, False),
                ("TLS-verify-failures", "GC_TLS_VERIFY_FAILED logged: data server TLS check failed, item not cached",
                 manager_log_metrics["GC_TLS_VERIFY_FAILED"].with_(period=hour), 1,
                 cloudwatch.TreatMissingData.NOT_BREACHING, False),
            ]
            for name, description, metric, threshold, missing, notify_ok in alarm_specs:
                alarm = metric.create_alarm(
                    self, f"Alarm{name.replace('-', '')}",
                    alarm_name=f"WIS2GC-{name}",
                    alarm_description=description,
                    threshold=threshold,
                    evaluation_periods=1,
                    comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                    treat_missing_data=missing)
                alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))
                if notify_ok:
                    alarm.add_ok_action(cw_actions.SnsAction(alarm_topic))

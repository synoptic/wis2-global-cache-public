from aws_cdk import (
    Stack,
    aws_cloudwatch as cloudwatch,
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


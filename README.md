# WIS2.0 Global Cache

## 1. Introduction
This repository contains the WIS2.0 Global Cache service, a caching solution composed of multiple components. The service is deployed using AWS CDK, ensuring a scalable and efficient caching infrastructure.
![architecture diagram](deploy/diagram.png)


## 2. Components

### 2.1. MQTT Client Service (paho MQTT - Fargate)
Uses a Fargate cluster to deploy one mqtt listenner client per global broker. Global Broker connection strings are stored in Secrets Manager. The client listens for incoming messages on the appropriate topics using paho MQTT and then queues the messages on SQS.  
`Stack File: deploy/stacks/wis2_client_stack.py`

### 2.2. Queue (SQS)
Deploys an SQS queue to handle message queuing. It ensures reliable message delivery and processing. The queue service includes a dead-letter queue for failed messages and a queue policy for access control.  
`Stack File: deploy/stacks/wis2_queue_stack.py`

### 2.3. Lambda Manager (python Lambda)
The Manager Lambda is responsible for processing messages from the queue. It reads messages from the queue, processes them, stores them in the cache, and publishes the cache messages if successful. 

### 2.4. Metrics and duplicate detection (redis elasticache)
The cache is implemented using redis Elasticache replication enabled cluster. The cache is used to store message data_ids, and their datetimes and to detect duplicate WIS2 notification messages. The cache is also used to store metrics data, such as the number of messages processed, the number of failures, and the datetime of the last successfully downloaded message that are returned by the metrics lambda.

### 2.5. MQTT Broker (EMQX - Fargate)
The local MQTT broker for the GC service. The broker is used to publish cache messages to subscribed clients (other WIS2 global services). An admin dashboard is also available to monitor the broker.

### 2.6. Metrics Lambda (python Lambda)
The Metrics Lambda is responsible for returning metrics data from the redis cache. It reads metrics data from the cache and returns the data to the client. This is coordinated by the API Gateway.

## 3. Deployment
This service is deployed using AWS CDK. The deployment is done in two steps, first deploying the infrastructure and then deploying the services.

### 3.1. Prerequisites

#### 3.1.1. Foundational Components
A number of foundational components are required to deploy the WIS2.0 Global Cache service that are not managed by this repository/CDK. These include:
* An AWS account
* An AWS VPC
* Domain Name System (DNS) configuration 
  * A Route 53 hosted zone
  * A registered domain name
  * A cert for ssl/tls with associated records in the hosted zone
* NAT Gateways (2) for static IP addresses
* An S3 bucket where the assets are stored
  * this is currently managed by the UKMet office and provided by ASDI
* the necessary role to which the necessary permissions are attached. Currently, the `WIS2ManagerLambdaRole` referenced in the wis2_lambda_stack.py.

#### 3.1.2. AWS CDK (Cloud Development Kit)

Follow the [AWS CDK Guide](https://docs.aws.amazon.com/cdk/v2/guide/home.html) for prerequisites and installation
instructions.

### 3.2. Deployment Steps

The best way to log in to the AWS account is to use the AWS CLI. The following command will open a browser window and
ask you to log in to the AWS account.

```bash
aws configure sso
```

or if you already have your profile set up, you can use the following command:

```bash
aws sso login --profile <profile_name>
```

Then you can deploy the stack using the _deploy_ command, ie:

```bash
cdk deploy --all --profile wis2-gc
```

#### 3.2.1. Development deployment

Under the _deploy_ directory there is a corresponding app_dev.py file that can be used to deploy a similar app, to the
development environment. Environment variables and parameters are set in this file and passed to the same stack components used in the
prod app.py file.

```bash
cdk deploy --all --profile wis2-gc --app "python app_dev.py"
```

## 4. Operations (test environment)

The commands in this section use the test environment app, `deploy/app_test_env.py`, with `deploy/wis2test.env`.

### 4.1. Deploying manager Lambda changes

Deploy the broker first, because its ACL is built into the image. The broker drops publishes the ACL denies without
returning an error, so a Lambda deployed first would log events as published while they are lost.

```bash
cd deploy
cdk deploy test-emqx-broker --exclusively --app "python app_test_env.py" --profile <test-profile>
cdk deploy test-wis2-sqs-work test-wis2-manager-lambda WIS2GCDashboard --exclusively --app "python app_test_env.py" --profile <test-profile>
```

`--exclusively` deploys only the named stacks. Without it, CDK also deploys their dependencies, and the dashboard
depends on the client and broker stacks. Run `cdk diff` with the same arguments first.

Then check end to end: subscribe as `everyone` to `monitor/#` and `cache/#` on the GC broker.

### 4.2. Cache size limit

The manager Lambda decides whether an item is too large to cache before it reads the body. It uses `Content-Length`
when present and counts bytes while downloading otherwise.

- The limit is the smaller of free `/tmp` space and `MAX_CACHE_BYTES`.
- When `MAX_CACHE_BYTES` is unset, it defaults to 25% of the Lambda memory size (32 MB at the test env's 128 MB).
- An item over the limit is not cached. It is republished on `cache/` with its origin links, and an `item.cache` INFO
  monitoring event is published.

The 25% figure is an estimate, not a measurement. The body is held in memory, and the S3 multipart upload adds
its own buffers. If the estimate is too high, a large item runs the Lambda out of memory, and that is not reported
as an event. To check it, compare "Max Memory Used" in the Lambda `REPORT` log lines, or the "Lambda Memory
Utilization" dashboard widget, against item sizes near the limit. To override it, set `MAX_CACHE_BYTES` through the
stack's `max_cache_bytes` parameter. See `docs/adr/0002-cache-size-limit.md`.

### 4.3. TLS verification of data server downloads

Downloads from WIS2 Nodes verify TLS certificates (commit 00f9cf7). Before that commit, an inverted `DEV-MODE` check
turned verification off. A data server with an expired, self-signed or wrong-host certificate now fails. Its items
are no longer cached, and each failure:

- logs `GC_TLS_VERIFY_FAILED` with the data server and data_id;
- publishes an `item.download` ERROR monitoring event with description `SSLError`, for `origin/` notifications;
- increments `wmo_wis2_gc_downloaded_errors_total` for that data server;
- is not retried, because a certificate problem will fail the same way on retry.

To find affected data servers, use CloudWatch Logs Insights on `/aws/lambda/test-wis2-manager-lambda`:

```
filter @message like /GC_TLS_VERIFY_FAILED/
| parse @message "dataserver='*'" as dataserver
| stats count() by dataserver
```

**Rollback.** Turn verification off for downloads only. In `manager_lambda/wis2_message.py`, `download_file`, change
`verify=not dev_mode` to `verify=False`, then redeploy the Lambda:

```bash
cdk deploy test-wis2-manager-lambda --exclusively --app "python app_test_env.py" --profile <test-profile>
```

Do not roll back by setting `DEV-MODE=true`. Dev mode also skips the S3 upload, so `cache/` notifications would
point at objects that do not exist. A plain `git revert 00f9cf7` no longer applies cleanly.

### 4.4. Failure handling and retries

Each SQS record that fails is either returned to SQS for one retry or dropped. The work queue is FIFO, with a
15-minute visibility timeout and `maxReceiveCount` 2, so a record that fails twice moves to the dead-letter queue.

| Failure | Retried | Log marker |
|---|---|---|
| Network error, timeout, HTTP 5xx (after the download session's own retries), 408, 429 | yes | `GC_RETRY` |
| S3 upload, Redis, or `cache/` publish error | yes | `GC_RETRY` |
| TLS/certificate error | no | `GC_TLS_VERIFY_FAILED` |
| Other HTTP 4xx (for example 404), integrity failure | no | none (see `error/` and metrics) |
| Anything else (malformed message, unexpected exception) | no | `GC_UNCLASSIFIED_FAILURE` |

For a retried download failure, the `item.download` monitoring event is published only on the final attempt
(`SQS_MAX_RECEIVE_COUNT`, set from the queue stack), so a failure that the retry recovers from is not reported to the
WIS2 Node or the Global Monitor. Failures that are not retried publish the event straight away.

Every failure still publishes to the internal `error/` topic with a full traceback. A retried record can therefore
publish to `error/` and count in `downloaded_errors_total` twice.

A retried record holds up its FIFO message group, which is the data_id, for 15 minutes per attempt. Copies of the same
item from other Global Caches (`cache/` notifications) share that group, so they wait behind it. A transient failure
on the origin therefore delays caching that item from another cache by 15 to 30 minutes. Before retries were enabled,
the failed record was dropped and the next copy was processed straight away.

### 4.5. Monitoring

- **Metrics:** CloudWatch metric filters count the three log markers as metrics in the `WIS2GC` namespace.
- **Dashboard:** the `WIS2-Operations-Dashboard` graphs those metrics and the dead-letter queue depth.
- **Alarms:** none are configured, because this repository defines no notification target (SNS topic).

Logs Insights query for all markers:

```
fields @timestamp, @message
| filter @message like /GC_(RETRY|UNCLASSIFIED_FAILURE|TLS_VERIFY_FAILED)/
| sort @timestamp desc
```

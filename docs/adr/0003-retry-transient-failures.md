# 3. Retry only transient failures, and alarm on what is left

Date: 2026-10-15

## Status

Accepted

## Context

The manager Lambda reads the FIFO work queue one record at a time. The queue has a 15-minute visibility timeout,
`maxReceiveCount` 2 and a dead-letter queue. The handler returned an empty `batchItemFailures` list for every batch,
so every failed record was deleted from the queue. A broker outage or a short WIS2 Node outage lost the item from
this cache without a retry, and nothing alerted anyone.

Some failures will fail the same way on any retry: HTTP 404, a certificate that fails verification, data that fails
its integrity check, or a malformed notification. Retrying them only delays the record and the other copies of the
same item in its FIFO message group.

A WIS2 Monitoring Event (ADR 0001) goes to the WIS2 Node and the Global Monitor and cannot be withdrawn.

## Decision

- **Retry once, transient failures only.** The handler returns the record in `batchItemFailures` for network errors,
  timeouts, HTTP 5xx (after the download session's own retries), 408 and 429, and for S3, Redis and broker publish
  errors. Everything else is dropped after it is logged and published to `error/`. The rules are in `is_transient`.
- **A failed `cache/` publish releases the dedup key**, so the retry is not skipped as a duplicate.
- **Report a retried download failure only on the final attempt.** The `item.download` ERROR event for a transient
  failure is published only when `ApproximateReceiveCount` reaches the queue's `maxReceiveCount`, which the CDK app
  passes to the Lambda as `SQS_MAX_RECEIVE_COUNT`. Failures that are not retried publish the event at once.
- **Log markers.** `GC_RETRY`, `GC_UNCLASSIFIED_FAILURE` and `GC_TLS_VERIFY_FAILED` are logged with the data
  server and data_id. CloudWatch metric filters count them in the `WIS2GC` namespace.
- **Alarms with email.** An SNS topic emails `ALARM_EMAIL` for: dead-letter queue not empty, Lambda errors (crash,
  timeout, out of memory), oldest work queue message over 1 hour, unclassified failures, TLS verification failures,
  and the Global Broker client service not running. README section 4.5 lists periods and missing-data handling.

## Alternatives considered

- **Retry every failure.** Rejected. Permanent failures would wait 15 minutes, fail again, and fill the dead-letter
  queue. Each retry also blocks the item's FIFO message group, delaying copies from other Global Caches.
- **Retry nothing (the previous behaviour).** Rejected. A broker restart or a short network failure lost items
  silently.
- **Retry inside the Lambda with backoff.** Rejected. The download session already retries 5xx twice. Longer waits
  inside the Lambda hold concurrency and memory while doing nothing; SQS visibility does the waiting for free.
- **Publish the download ERROR event on the first attempt.** Rejected. A failure the retry recovers from would still
  tell the Node and the Global Monitor that the download failed, and the event cannot be corrected.
- **Alarm on low ingest volume** to detect a stopped Global Broker client. Rejected for now. Test traffic is
  intermittent, so any threshold would either miss outages or fire during quiet periods. The client alarm uses the
  ECS service's CPU metric, which stops when no task runs.
- **Container Insights `RunningTaskCount` for the client alarm.** Not used. It is direct but billed per metric; the
  missing CPU metric gives the same signal for a stopped task at no cost.

## Consequences

- A transient failure on the WIS2 Node delays that item, and copies of it from other Global Caches, by 15 to 30
  minutes. Before, the next copy was processed at once.
- A retried download failure can publish to `error/` and count in `downloaded_errors_total` twice.
- If the Lambda crashes or times out on the final attempt, no `item.download` event is published for that item.
- Records that fail twice wait on the dead-letter queue for 2 days. Nothing reads them; README section 4.7 has the
  inspect, purge and redrive commands. The dead-letter alarm stays on until the queue is purged or redriven.
- The alarm email must be confirmed once after the first deploy, or nothing is delivered.
- The client alarm does not detect a client that runs but is disconnected from the Global Broker.
- Changing `maxReceiveCount` in `wis2_sqs_stack.py` changes when events are published. The value reaches the
  Lambda through the CDK app, so both change together.

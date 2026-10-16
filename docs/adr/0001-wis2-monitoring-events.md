# 1. Publish WIS2 Monitoring Events from the manager Lambda

Date: 2026-10-13

## Status

Accepted

## Context

The WIS2 Guide (INFCOM-4), section 2.7.4.1, says a Global Cache may decide not to cache data, for example when it is
too large. When it does, it must publish a WIS2 Monitoring Event (WME) on the `monitor/` topic, one per data instance,
and republish the notification as if `properties.cache` were false. The same section says that embedded content that
fails validation should fall back to downloading from the URL. It also says that a republished notification changes
only `id`, the `global-cache` link and the canonical `link`.

The cache had no `monitor/` publishing. Download and integrity failures were visible only on the internal `error/`
topic and in logs, so WIS2 Nodes and the Global Monitor could not see them.

Constraints:

- The WME standard defines a CloudEvents 1.0 message on `monitor/a/wis2/<centre-id>` (exactly four levels), at most
  8192 bytes, with severity DEBUG, INFO, WARNING, ERROR or CRITICAL.
- The manager Lambda already has Redis (dedup, metrics) and publishes to the EMQX broker with `paho.mqtt.publish.single`.
- The Lambda handles one SQS batch per invocation, at high concurrency. The same notification often arrives from
  several Global Brokers.
- Lambda package size and cold start matter. The current runtime is Python 3.10.
- Event text is built partly from exception messages and URLs, which come from outside and can contain presigned tokens
  or control characters.

## Decision

Add a small stdlib-only module, `manager_lambda/wis2_monitor.py`, that builds, sizes and validates WMEs. Call it from
the existing handler at three points.

| Situation | Event type | Severity | Rate limited |
|---|---|---|---|
| Item larger than the cache limit, not cached | `item.cache` | INFO | no (required by the Guide) |
| Download from the WIS2 Node fails | `item.download` | ERROR | yes |
| Downloaded data fails the integrity check | `item.integrity` | ERROR | yes |
| Embedded content fails integrity, URL download used instead | `item.integrity` | WARNING | yes |

Details:

- **Topic and fields.** The topic is `monitor/a/wis2/<subject>`. `subject` is the centre-id at level 4 of the
  notification topic. `source` is this cache's centre-id from `REPORT_BY`. Centre-ids are checked against the WIS2
  centre-id pattern before use. WMO confirmed this topic, `subject` and `source` rule for events about a node's
  data on 2026-10-16. Download and integrity events are published only for notifications received on
  `origin/`. A failure fetching another cache's copy says nothing reliable about the origin node, so it goes to the
  metric and `error/` only. `item.cache` events are published for both channels. An invalid subject or `REPORT_BY` means no event is published, and an error is logged.
- **Not caching.** The size decision is made before the body is read: from `Content-Length` when present, and from a
  running byte count while streaming otherwise. The limit is the smaller of free `/tmp` space and `MAX_CACHE_BYTES`
  (optional, set through the CDK stack). When `MAX_CACHE_BYTES` is unset, the limit defaults to 25% of the Lambda
  memory size (`AWS_LAMBDA_FUNCTION_MEMORY_SIZE`), because the body is held in memory and the S3 multipart upload adds
  its own buffers. The 25% figure is an estimate. If it is too high, large items still run the Lambda out of memory
  and are not reported. See ADR 0002. The notification is republished with its origin links unchanged.
  `properties.cache` is not rewritten.
- **Failures as typed exceptions.** `wis2_message.py` raises `TooLargeToCache`, `DownloadError` and `IntegrityError`.
  The handler maps each to an event.
- **Once per data instance.** Before publishing, the Lambda claims a Redis key `wme|<type>|<data_id>|<pubtime>[|suffix]`
  with `SET NX EX`, using the same TTL as notification dedup. If the publish fails, the claim is deleted so a later copy
  of the notification can retry.
- **Retried failures report once final.** A download failure that SQS will retry (see README "Failure handling")
  publishes its `item.download` event only on the final attempt, when `ApproximateReceiveCount` reaches the queue's
  `maxReceiveCount` (passed to the Lambda as `SQS_MAX_RECEIVE_COUNT`). An event cannot be withdrawn, so a short outage
  that the retry recovers from is not reported. If the count is missing, the attempt is treated as final.
- **Rate limit.** A fixed-window counter in Redis per (subject, event type) is applied to download and integrity events
  only. The defaults are 60 per 300 s (`WME_RATE_LIMIT`, `WME_RATE_WINDOW_SECONDS`). One warning is logged when the
  limit is reached.
- **Size and content.** Title and channel are capped. The description is truncated so the encoded message fits
  8192 bytes. All text has URL query strings and fragments removed and control characters replaced.
- **Never blocks caching.** `publish_wme` catches all errors, logs them, and returns False. A failed event never changes
  the cache result for the record.
- **Broker ACL.** `wis2_publisher` may publish to `monitor/#`. `everyone` may subscribe to `monitor/#`. Other Global
  Services connect as `everyone` (confirmed 2026-10-14).
- **`error/` unchanged.** It stays as the internal debug topic with full tracebacks.

## Alternatives considered

- **Set `properties.cache = false` on the republished notification.** Rejected. The Guide lists the only fields a
  cache changes on republish, and this is not one of them. Setting it would also tell downstream caches not to cache
  the item, which is a decision for the WIS2 Node, not for us. This reading of the Guide still needs confirming.
- **Validate each event against the WME JSON schema at runtime with `jsonschema`.** Rejected for the Lambda. It adds a
  dependency and per-event cost for a message whose shape is fixed in code. The builder checks every field the schema
  constrains. Schema validation belongs in CI instead.
- **No dedup.** Rejected. Each notification arrives from several Global Brokers and may be retried, so events would be
  multiplied. The Guide asks for one event per data instance. Redis is already used for notification dedup, so this
  adds no new service.
- **No rate limit.** Rejected for download and integrity events. A WIS2 Node that goes down produces one event per
  data item, which can flood `monitor/` and the Global Monitor. `item.cache` events are required by the Guide and are
  bounded by dedup, so they are not limited.
- **A persistent MQTT client per Lambda container instead of `publish.single`.** Deferred. It would save a TLS
  handshake per event, but it needs connection lifecycle handling across freeze and thaw. Event volume is low, and the
  existing `cache/` publish already uses `publish.single`.
- **Detect failure types by matching exception text.** Rejected. It is fragile across `requests`/`urllib3` versions.
  Typed exceptions make the mapping explicit.

## Consequences

- WIS2 Nodes and the Global Monitor can see items we did not cache and failures to fetch or verify their data.
- **Deploy order matters.** The ACL is baked into the broker image, and EMQX is set to `deny_action = ignore`. If the
  Lambda is deployed before the broker, every event is dropped by the broker without an error, while the Lambda logs
  it as published. The broker must be deployed first.
- **Publish success is not confirmed.** `publish.single` does not check MQTT v5 PUBACK reason codes, so an ACL denial
  looks like success. End-to-end checks must use a subscriber, not Lambda logs.
- **Failure modes:**
  - Broker down: events are lost, caching is unaffected, the claim is released, and an error is logged.
  - Redis down: the claim raises, no event is published, and an error is logged. Notification dedup already depends on
    Redis, so the record fails anyway.
  - Rolling broker update (3 tasks): events published through an old task during rollout are denied and lost.
  - Partial publish with several brokers: if one broker accepts and the next fails, the claim is released and a later
    retry may send a duplicate to the first. Accepted.
  - Rate limited events are not sent later. Accepted. The limit warning is in the logs.
  - Unset `REPORT_BY`: no events, with one error log per event. Every environment must set it to its own centre-id.
- New configuration: `REPORT_BY` (required for events), `MAX_CACHE_BYTES`, `WME_RATE_LIMIT`, `WME_RATE_WINDOW_SECONDS`.
- Open items are tracked separately:
  - the subject rule for items from another cache's `cache/` topic (mitigated by the `origin/`-only rule above; the
    WMO confirmation of 2026-10-16 covers only items received on `origin/`);
  - the final `dataschema` URL (clause 8 value adopted; check it resolves once published);
  - which username Global Services use (confirmed 2026-10-14: Global Brokers connect to this GC as `everyone`, and
    this GC connects to the Global Broker as `everyone`);
  - rate-limit values (defaults accepted; tune from logs);
  - the 25% memory fraction for the cache size limit (check "Max Memory Used" for a large item).

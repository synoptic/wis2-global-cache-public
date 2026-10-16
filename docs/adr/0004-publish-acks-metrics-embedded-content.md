# 4. Confirmed broker publishes, data-server-only download metrics, and embedded-content fallback

Date: 2026-10-15

## Status

Accepted

## Context

Four problems in the manager Lambda showed up in the spec audit and the code review of the retry change (ADR 0003):

- **Publishes were not confirmed.** `paho.mqtt.publish.single` does not check MQTT v5 PUBACK reason codes. EMQX
  answers an ACL-denied publish with "Not authorized", which looked like success, so a `cache/` notification or a
  monitoring event could be lost while the Lambda logged it as published (ADR 0001). The broker certificate chain was
  checked, but its hostname was not (`insecure: True`).
- **Download metrics blamed data servers for our faults.** Every failure incremented
  `wmo_wis2_gc_downloaded_errors_total` and set `wmo_wis2_gc_dataserver_status_flag` to 0, including integrity
  failures and our own S3, Redis and broker errors. The Guide (2.7.4.1) defines separate download and integrity
  counters per data server, and the Global Monitor reads them.
- **Bad embedded content dropped the item.** A notification with an unknown `properties.content.encoding`, invalid
  base64, or the `gzip` encoding raised a plain exception and the item was not cached. The Guide (2.7.4.1) says a
  Global Cache "will fall back" to the URL when embedded content is invalid, and that using embedded content at all
  is optional ("may attempt").
- **Presigned tokens reached logs.** Download errors, tracebacks and urllib3 retry warnings printed full source URLs,
  including query strings such as `X-Amz-Signature`.

The Lambda now runs on Python 3.13 with urllib3 2.x, which also tightens TLS checks on data servers (TLS 1.2
minimum and strict X.509 rules).

## Decision

- **Publish with a short-lived paho client that waits for acknowledgements.** It waits for CONNACK and PUBACK and
  raises on a failure reason code or no answer. Each step (TCP connect, TLS handshake, CONNACK, PUBACK) is limited
  to 10 seconds. It verifies the broker certificate's hostname, and it does not reconnect. A failed `cache/` publish
  is retried through SQS (ADR 0003). A failed monitoring event is logged and does not affect caching.
- **Download metrics count only download failures.** Integrity failures increment only the integrity counter. Our
  own errors update neither; they show in logs, the `GC_*` metrics, alarms and `error/`. Success metrics are written
  after the `cache/` publish, so a publish retry counts once.
- **Fall back to the URL for any invalid embedded content.** Unknown encoding, invalid base64 or utf-8, a non-string
  value, or a decoded length different from `size` publishes an `item.integrity` WARNING event and downloads the
  item. `gzip` content is valid WIS2 (WNM 1.15: RFC 1952, then base64) but is not decoded; the item is downloaded
  without an event.
- **Remove URL query strings** from log lines, tracebacks and the `error` field of `error/` messages, and raise the
  urllib3 logger to ERROR.
- **Keep the stricter data server TLS checks** that come with Python 3.13 and urllib3 2.x for when certificate
  verification is on. Verification itself is off for now (`VERIFY_DATA_SERVER_TLS = False`, README 4.3), because
  several test-environment nodes have invalid certificates; turning it on is a tracked follow-up.

## Alternatives considered

- **Keep `publish.single` and check delivery with a subscriber.** Rejected. It only shows a failure after the fact,
  for one topic, and gives no per-record retry.
- **A persistent MQTT client per Lambda container.** Deferred again (see ADR 0001). It saves a TLS handshake per
  publish but needs reconnect handling across freeze and thaw.
- **Let the paho client reconnect.** Rejected. A reconnect resends the QoS 1 message and can hold the Lambda for the
  socket timeout. SQS already retries the record.
- **Count integrity failures as download errors too** (Q-36, reading 2). Rejected for now. The Guide defines two
  counters, and the double count hides which kind of failure happened. Revisit if WMO says otherwise.
- **Add a counter for our own errors now.** Deferred. The log-marker metrics and alarms cover them for operators; a
  Global Monitor metric for them is not defined.
- **Decode gzip embedded content.** Rejected. It would decompress untrusted input inside the Lambda, which needs a
  bound on the decompressed size, to save one download of an item under 4 KB.
- **Relax urllib3's strict X.509 flag for data servers.** Rejected by the operator. Data servers should meet current
  certificate rules; failures are visible through `GC_TLS_VERIFY_FAILED` and its alarm.

## Consequences

- A refused or unanswered `cache/` publish is now retried and, if it fails twice, lands on the dead-letter queue and
  alarms. Before, it was lost silently.
- A lost PUBACK can produce a second `cache/` notification for the same item with a different `id` after the SQS
  retry. Accepted; consumers deduplicate on `data_id`.
- If the broker certificate stops matching its hostname, every `cache/` publish fails. The rollback is one line
  (README 4.6).
- The Global Monitor sees fewer download errors: integrity failures and our own errors no longer count. There is no
  metric for our own errors.
- Items with gzip or invalid embedded content now cost a download. Invalid content also sends a WARNING event to the
  WIS2 Node.
- The original notification inside `error/` messages keeps its links, query strings included. It is the notification
  as published on the Global Broker.
- While verification is off, data servers are not authenticated, and `GC_TLS_VERIFY_FAILED` and its alarm never
  fire. When it is turned on, data servers that only offer TLS 1.0 or 1.1, or whose certificates are invalid or break
  strict X.509 rules, fail. A 2026-10-15 check found none that failed only because of the strict rules.

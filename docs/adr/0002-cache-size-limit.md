# 2. Default the cache size limit to 25% of Lambda memory

Date: 2026-10-14

## Status

Accepted

## Context

The WIS2 Guide (INFCOM-4) section 2.7.4.1 lets a Global Cache decide not to cache data, for example when it is too
large. When it does, it publishes an `item.cache` monitoring event and republishes the notification with origin links
(ADR 0001).

The manager Lambda decides this before reading the body. It uses `Content-Length` when present and counts bytes
while streaming otherwise. The limit was the smaller of free `/tmp` space and an optional `MAX_CACHE_BYTES`. No
deployment set `MAX_CACHE_BYTES`, so in practice the limit was free `/tmp` space, about 512 MB by default.

The Lambda holds the whole body in memory: it reads the downloaded file into bytes, hashes it, and passes it to the
S3 upload, whose multipart upload adds part buffers above 8 MB. The test environment runs the Lambda at 128 MB. An item
between the memory ceiling and the `/tmp` size runs the Lambda out of memory. When that happens, nothing is reported:
no monitoring event, no `error/` message, no metric. SQS delivers the record again and then moves it to the
dead-letter queue.

## Decision

When `MAX_CACHE_BYTES` is unset, the limit defaults to 25% of the Lambda memory size, read from
`AWS_LAMBDA_FUNCTION_MEMORY_SIZE`, which Lambda sets in every function. Free `/tmp` space still applies.
`MAX_CACHE_BYTES` overrides the default when set.

At the test environment's 128 MB this is 32 MB.

The 25% figure is an estimate, not a measurement. It leaves room for the Python runtime and libraries, one copy of the
body, and S3 multipart buffers.

## Alternatives considered

- **Keep free `/tmp` space as the only limit.** Rejected. It allows items far larger than memory, which fail silently
  with an out-of-memory error.
- **Require `MAX_CACHE_BYTES` per environment.** Rejected as the only mechanism. Nothing set it before, and a value
  that is not tied to memory goes stale when memory is changed. It stays available as an override.
- **Stream from `/tmp` to S3 and hash in chunks, so memory no longer limits item size.** Deferred. It removes the
  memory ceiling, but it rewrites `cache_msg_data`, integrity validation and the upload path. The limit would then be
  `/tmp` size, which needs ephemeral storage set in the stack.
- **Raise Lambda memory.** Possible later, and the default follows automatically. It costs more GB-seconds per
  invocation for every item, not just large ones.

## Consequences

- Items over 25% of memory are not cached. Users get them from the WIS2 Node, and the Node and Global Monitor see an
  `item.cache` INFO event per item.
- If 25% is too low, the GC passes through data it could have cached.
- If 25% is too high, large items still run out of memory without being reported.
- To check the estimate, compare "Max Memory Used" in the Lambda `REPORT` log lines, or the Lambda Insights memory
  widget on the operations dashboard, for items near the limit. Then adjust with `MAX_CACHE_BYTES` or Lambda memory.
- Changing Lambda memory changes the cache limit. This is intended, but it is easy to miss when tuning for cost.

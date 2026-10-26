# 6. Give every cached data instance a unique download URL

Date: 2026-10-26

## Status

Proposed

## Context

The manager Lambda stores each cached item in S3 at `data/<topic below wis2>/<filename>`, where the filename is the
last path segment of the notification's `update` or `canonical` link. The `cache/` notification's `canonical` and
`update` links are rewritten to that object's URL.

An update to an item has the same topic and usually the same filename as the original, so it overwrote the original
object and reused its URL. A client holding the first notification could then download data that no longer matched
that notification's integrity value. The same happened when an item was published again after its dedup key had
expired in Redis.

WIS2 Notification Message issue
[wis2-notification-message#180](https://github.com/wmo-im/wis2-notification-message/issues/180) led to new text for
FT2027-1 ([wis2-notification-message#192](https://github.com/wmo-im/wis2-notification-message/pull/192)):

> The `href` property for the link relation value of `canonical` or `update` SHALL uniquely identify the data
> instance the notification is related to.

Although the text applies from FT2027-1, the Global Cache operators agreed to implement it "reasonably rapidly". The
work is tracked in [et-w2it#72](https://github.com/wmo-im/et-w2it/issues/72). The approaches other Global Caches
reported there:

- de-dwd-global-cache adds the pubtime to the filename (format `20260928T090111Z`) when it receives an update.
- cn-cma-global-cache adds the pubtime to the filename (format `_R-20260716093713Z`) when it receives an update.
- jp-jma-global-cache already includes the pubtime in its URLs.

## Decision

Add the notification's `pubtime` to every S3 key as a path segment just before the filename:

```
before: data/de-dwd/data/core/weather/surface/file.bufr
after:  data/de-dwd/data/core/weather/surface/20260928T090111Z/file.bufr
        data/de-dwd/data/core/weather/surface/20260928T090111.123Z/file.bufr   (fractional seconds)
```

- The segment is added for every item, not only for updates.
- Fractional seconds are kept to milliseconds, and left out when they are zero.
- The segment is built from the parsed pubtime (`pubtime_epoch`), not from the raw `pubtime` string. The existing
  parser accepts trailing text after the first three fractional digits, so the raw string could put characters such
  as `/` or `..` into the key.

The change is in `format_s3_key` in `manager_lambda/wis2_message.py`. The download URL and the rewritten `canonical`
and `update` links follow from the key.

Two related fixes to the filename part of the URL, in the same file:

- The download URL is percent-encoded from the key. Before, the decoded filename went into the URL as is, so
  filenames with spaces, `#`, `?`, `%` or non-ASCII characters gave broken or ambiguous URLs.
- `get_source_link` replaces `/` and NUL in the decoded filename with `_`, and a filename of `.` or `..` with
  underscores, then logs `GC_UNSAFE_FILENAME`. A `%2F` in the source URL otherwise put `../` into the key, which
  clients resolve to a different object, and into the `/tmp` download path. For example, `..%2F..%2Fevil.bufr` is
  stored as `.._.._evil.bufr`.

  The Guide (Global Services, Global Cache) allows a Global Cache to decide not to cache an item, as long as it
  republishes the notification unchanged and sends a WME. That was the main alternative for these filenames. It was
  not chosen because no WIS2 specification requires the cached filename to match the origin's, and replacing the
  characters keeps the item cached. Filenames like these are not expected from working data servers.

## Alternatives considered

- **Add the pubtime to the filename, only for updates (DWD, CMA).** Not chosen. Changing the filename can break
  clients that use the filename or extension, and doing it only for updates leaves the re-publish case above, where a
  later `canonical` notification for the same item overwrites the earlier object.
- **Add the pubtime to the filename for every item.** Not chosen, for the same filename and extension reason. A path
  segment gives the same uniqueness and keeps the filename the publisher chose.
- **Use a random or hash-based path segment.** Not chosen. The pubtime is already part of the dedup key
  (`data_id` + `pubtime`), and it makes the URL readable and easy to match to its notification.

## Consequences

- Each data instance, including updates, has its own URL, and earlier URLs keep serving the data their
  notifications describe until the 1-day lifecycle rule expires them.
- Two instances with the same topic, filename and pubtime to the millisecond still share a key. This is accepted.
  For the same `data_id`, the dedup check already drops a notification whose pubtime is equal to or earlier than the
  cached one. Different `data_id`s with the same filename and pubtime under one topic are not expected.
- An update stores a second object instead of replacing the first, so S3 storage grows by the size of updated items
  for up to a day.
- An item whose filename had characters replaced is cached under a name that differs from the origin's. The WIS2
  Node is not told; the `GC_UNSAFE_FILENAME` log line is the only record.
- No migration is needed. Objects stored under the old keys expire under the lifecycle rule. Rolling back is a
  revert of `format_s3_key`.

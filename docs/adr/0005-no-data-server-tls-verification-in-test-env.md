# 5. Do not verify data server TLS certificates in the test environment

Date: 2026-10-16

## Status

Accepted

## Context

The manager Lambda downloads each item from the URL in the WIS2 notification, which points at the WIS2 Node's data
server. The Guide (2.5.2) prefers HTTPS and says that if HTTPS is used, the certificate must be valid. Plain HTTP is
allowed.

This cache runs only in the WIS2 test environment. A check on 2026-10-15 and 2026-10-16 of the data servers it had
downloaded from found:

- several test nodes failing verification on both HTTPS (443) and MQTT over TLS (8883): four with Let's Encrypt
  certificates that expired on 23-24 Sep 2026, one serving a self-signed default certificate, and one expired and
  not sending its intermediate certificate;
- the test Global Broker, the test GDC and the wis2box test node with valid certificates;
- a few production WIS2 Nodes reachable through the test Global Broker, one of which also fails verification;
- no data server that failed only because of the stricter checks in Python 3.13 and urllib3 2.x.

With verification on, an item from a server that fails verification is not cached. It is passed through with its
origin link, and the node receives an `item.download` ERROR event (ADR 0001, ADR 0003).

The cache had not been verifying certificates before this work, because of an inverted `DEV-MODE` check. ADR 0004
kept the strict checks for when verification is on and turned verification off pending this decision.

On 2026-10-16 the test-environment collaborators concluded that test nodes are not expected to have valid TLS
certificates.

## Decision

Do not verify data server certificates. `VERIFY_DATA_SERVER_TLS` in `manager_lambda/wis2_message.py` stays `False`,
and urllib3's per-request `InsecureRequestWarning` stays silenced. Downloads still use HTTPS where the URL says so;
only certificate and hostname checks are skipped.

The other TLS connections are unchanged and verified:

- the Lambda's publishes to this cache's broker (ADR 0004);
- the client's connection to the Global Broker;
- clients connecting to this cache's broker, metrics endpoint and S3 downloads, which use ACM- and AWS-managed
  certificates.

## Alternatives considered

- **Verify, as in production.** Rejected for the test environment. Test nodes would stop being cached whenever their
  certificates lapse, and each item would send an ERROR event about something the test environment has agreed not
  to require. Test traffic would then exercise certificate handling instead of the cache.
- **Verify, and download without verification when the check fails.** Not chosen. It keeps visibility through
  `GC_TLS_VERIFY_FAILED` and its metric, but adds code and a second connection per affected item to report
  something the test environment does not require.
- **Verify every host except an allow-list of test nodes.** Not chosen. The list would need maintaining as test
  nodes come and go, and the production nodes reached through the test Global Broker are not this cache's
  responsibility to police in a test environment.

## Consequences

- Data servers are not authenticated. A download could come from a server impersonating the node, for example
  through DNS spoofing. Notifications that carry an integrity value are still checked against it, which catches
  tampered content but not a spoofed notification.
- Test nodes with lapsed or self-signed certificates are cached normally, and no event is sent about their
  certificates.
- `GC_TLS_VERIFY_FAILED`, its metric filter and the `WIS2GC-TLS-verify-failures` alarm never fire. They are kept so
  that turning verification on needs no other change.
- If this cache, or code derived from it, is run against production WIS2 Nodes, revisit this decision.
  Production nodes are expected to have valid certificates (Guide 2.5.2), and turning verification on is one line
  (README 4.3).

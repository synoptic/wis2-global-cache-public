"""WIS2 Monitoring Event (WME) construction for the Global Cache.

Events are CloudEvents 1.0 messages published on ``monitor/a/wis2/<subject centre-id>``
as defined by the WIS2 Monitoring Events standard. ``source`` is this Global Cache's
centre-id and ``subject`` is the centre the event is about.
"""
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

logger = logging.getLogger(__name__)

EVENT_ITEM_CACHE = "int.wmo.wis.wme.event.item.cache"
EVENT_ITEM_DOWNLOAD = "int.wmo.wis.wme.event.item.download"
EVENT_ITEM_INTEGRITY = "int.wmo.wis.wme.event.item.integrity"

# four dataschema values appear in the WME draft; this is the normative clause 8 example (audit C-08)
WME_DATASCHEMA = "https://schemas.wmo.int/wme/1.0.0/schemas/wis2-event-message-bundled.json"
WME_CONFORMANCE = "http://wis.wmo.int/spec/wme/1/conf/monitoring-event-message-core"
WME_MAX_BYTES = 8192
SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_CENTRE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
# urllib3 errors render scheme-less paths, e.g. "url: /path?X-Amz-Signature=..."
_BARE_QUERY = re.compile(r"\?[^\s'\"<>()]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TITLE_MAX_CHARS = 256


def is_centre_id(value: str | None) -> bool:
    """Checks a value is a plausible WIS2 centre-id (``tld-centre-name``, lowercase, dashes).

    Args:
        value: Candidate centre-id.

    Returns:
        True if the value can be used as a topic level and CloudEvents source/subject.
    """
    return isinstance(value, str) and bool(_CENTRE_ID.match(value))


def monitor_topic(subject: str) -> str:
    """Builds the four-level WME topic for a subject centre.

    Args:
        subject: Centre-id the event is about.

    Returns:
        ``monitor/a/wis2/<subject>``.

    Raises:
        ValueError: If subject is not a valid centre-id.
    """
    if not is_centre_id(subject):
        raise ValueError(f"invalid subject centre-id: {subject!r}")
    return f"monitor/a/wis2/{subject}"


def strip_url_query(url: str) -> str:
    """Removes query string and fragment, which may carry presigned tokens.

    Args:
        url: URL to clean.

    Returns:
        The URL without query or fragment.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sanitize_text(text: str, max_chars: int) -> str:
    """Makes externally derived text safe to publish: strips URL queries and control characters.

    Args:
        text: Raw text, e.g. an exception message.
        max_chars: Maximum length of the result.

    Returns:
        Sanitized, length-bounded text.
    """
    cleaned = _URL.sub(lambda m: strip_url_query(m.group(0)), str(text))
    cleaned = _BARE_QUERY.sub("", cleaned)
    cleaned = _CONTROL.sub(" ", cleaned)
    return cleaned[:max_chars]


def build_wme(event_type: str, severity: str, source: str, subject: str, title: str,
              description: str | None = None, channel: str | None = None) -> dict:
    """Builds a WME as a dict.

    Args:
        event_type: Event type from the WME event-type code list.
        severity: One of SEVERITIES.
        source: Centre-id of this Global Cache.
        subject: Centre-id the event is about.
        title: Short human-readable summary.
        description: Optional longer explanation.
        channel: Optional topic the event concerns.

    Returns:
        The WME message.

    Raises:
        ValueError: If severity, source or subject is invalid.
    """
    if severity not in SEVERITIES:
        raise ValueError(f"invalid severity: {severity!r}")
    if not is_centre_id(source):
        raise ValueError(f"invalid source centre-id: {source!r}")
    if not is_centre_id(subject):
        raise ValueError(f"invalid subject centre-id: {subject!r}")
    content = {"title": sanitize_text(title, _TITLE_MAX_CHARS)}
    if description:
        content["description"] = sanitize_text(description, WME_MAX_BYTES)
    if channel:
        content["channel"] = sanitize_text(channel, _TITLE_MAX_CHARS)
    return {
        "id": str(uuid4()),
        "specversion": "1.0",
        "type": event_type,
        "source": source,
        "subject": subject,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datacontenttype": "application/json",
        "dataschema": WME_DATASCHEMA,
        "data": {
            "conformsTo": [WME_CONFORMANCE],
            "severity": severity,
            "content": content,
        },
    }


def encode_wme(wme: dict) -> bytes:
    """Serializes a WME, truncating the description so the message fits WME_MAX_BYTES.

    Args:
        wme: Message from build_wme.

    Returns:
        UTF-8 encoded JSON of at most WME_MAX_BYTES.

    Raises:
        ValueError: If the message is still too large without a description.
    """
    payload = json.dumps(wme, ensure_ascii=False).encode("utf-8")
    if len(payload) <= WME_MAX_BYTES:
        return payload
    content = wme["data"]["content"]
    description = content.get("description", "")
    overflow = len(payload) - WME_MAX_BYTES
    marker = "..."
    encoded = description.encode("utf-8")
    keep = len(encoded) - overflow - len(marker.encode("utf-8"))
    if keep > 0:
        content["description"] = encoded[:keep].decode("utf-8", errors="ignore") + marker
    else:
        content.pop("description", None)
    payload = json.dumps(wme, ensure_ascii=False).encode("utf-8")
    if len(payload) > WME_MAX_BYTES:
        raise ValueError(f"WME is {len(payload)} bytes without description; limit is {WME_MAX_BYTES}")
    return payload


def claim_event(redis_client, key: str, ttl_seconds: int) -> bool:
    """Atomically claims an event key so concurrent invocations publish it once.

    Args:
        redis_client: Redis client.
        key: Dedup key for the event instance.
        ttl_seconds: How long the claim is held.

    Returns:
        True if this caller should publish the event.
    """
    return bool(redis_client.set(key, 1, ex=ttl_seconds, nx=True))


def within_rate_limit(redis_client, subject: str, event_type: str, limit: int, window_seconds: int,
                      now: float) -> bool:
    """Fixed-window rate limit per (subject, event type).

    Args:
        redis_client: Redis client.
        subject: Subject centre-id.
        event_type: WME event type.
        limit: Maximum events per window.
        window_seconds: Window length.
        now: Current epoch seconds.

    Returns:
        True if the event may be published.
    """
    window = int(now // window_seconds)
    key = f"wme_rate|{subject}|{event_type}|{window}"
    count = redis_client.incr(key)
    if count == 1:
        redis_client.expire(key, window_seconds * 2)
    if count == limit + 1:
        logger.warning("WME rate limit reached; suppressing further events this window: subject=%r type=%r",
                       subject, event_type,
                       extra={"subject": subject, "event_type": event_type, "limit": limit,
                              "window_seconds": window_seconds})
    return count <= limit

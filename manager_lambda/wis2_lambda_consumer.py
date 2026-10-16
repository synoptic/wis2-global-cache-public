import json
import os
import traceback
from copy import deepcopy
from functools import wraps
from random import randint
# from aws_embedded_metrics import metric_scope
import redis
import gc
import glob
import boto3
import requests as requests
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError
from datetime import datetime as dt
from urllib.parse import urlsplit
import paho.mqtt.client as mqtt
import time
import logging
from enum import Enum
import ssl
import threading
from wis2_message import Wis2Message, IntegrityError, DownloadError, TooLargeToCache, ContentDecodeError
import wis2_monitor

logger = logging.getLogger()
logger.setLevel(logging.WARN)
# urllib3 retry warnings print the full request URL, including presigned query strings; the download outcome is
# logged by this module instead
logging.getLogger("urllib3").setLevel(logging.ERROR)

# bucket info
s3_bucket_region = os.environ.get('dest_bucket_region')
s3_bucket_name = os.environ.get('dest_bucket_name')
s3_client_obj = boto3.client('s3')
# mqtt broker info
broker_host = os.environ.get('MQTT_BROKER_HOST')
broker_user = os.environ.get('MQTT_PUB_USER')
broker_pw = os.environ.get('MQTT_PUB_PASSWORD')
env = {'s3_bucket_name': s3_bucket_name, 's3_bucket_region': s3_bucket_region}
brokers = [
    {"host": broker_host, "port": 8883, "username": broker_user, "password": broker_pw},
]
redis_endpoint = os.environ.get('CACHE_ENDPOINT')
# redis cache
redis_host = redis.Redis(redis_endpoint, port=6379, decode_responses=True)
ttl_minutes = 360
global dev_mode
dev_mode = os.environ.get('DEV-MODE', 'False') in ['True', 'true', '1']
logging.info(f"dev mode: {dev_mode}")
report_by = os.environ.get('REPORT_BY')
# applies to the non-normative download/integrity events only; gs:182 not-cached events are never limited
wme_rate_limit = int(os.environ.get('WME_RATE_LIMIT', '60'))
wme_rate_window_seconds = int(os.environ.get('WME_RATE_WINDOW_SECONDS', '300'))
# work queue maxReceiveCount, set by the CDK stack; unset means every attempt counts as final, so no event is held back
sqs_max_receive_count = int(os.environ.get('SQS_MAX_RECEIVE_COUNT', '1'))

PUBLISH_TIMEOUT_SECONDS = 10
TRANSIENT_HTTP_STATUS = (408, 429)
# RetryError is what the download session raises once its own 5xx retries are exhausted
TRANSIENT_DOWNLOAD_CAUSES = (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                             requests.exceptions.ChunkedEncodingError, requests.exceptions.RetryError)
TRANSIENT_ERRORS = (BotoCoreError, ClientError, S3UploadFailedError,
                    redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


class PublishError(Exception):
    """Publishing the cache/ notification to the broker failed."""


def is_transient(e: Exception) -> bool:
    """Decides whether a failed record is worth one SQS retry (queue maxReceiveCount is 2).

    Args:
        e: Exception that failed the record.

    Returns:
        True for failures likely to succeed later: network errors, timeouts, HTTP 5xx/408/429,
        S3, Redis and broker publish errors. False for TLS, other HTTP 4xx, integrity and unknown errors.
    """
    if isinstance(e, DownloadError):
        if e.tls:
            return False
        if e.status is not None:
            return e.status >= 500 or e.status in TRANSIENT_HTTP_STATUS
        return isinstance(e.__cause__, TRANSIENT_DOWNLOAD_CAUSES)
    return isinstance(e, (PublishError, *TRANSIENT_ERRORS))


def sanitized_traceback() -> str:
    """Returns the current exception's traceback with URL query strings removed.

    Download errors chain the requests exception, whose text carries the source URL and any presigned token.
    """
    return wis2_monitor.strip_url_queries(traceback.format_exc())


def is_final_attempt(receive_count) -> bool:
    """Decides whether SQS will deliver this record again if it fails now.

    Args:
        receive_count: SQS ApproximateReceiveCount attribute (a string), or None when absent.

    Returns:
        True when the count reaches the queue's maxReceiveCount, or is absent or unparsable.
    """
    try:
        return int(receive_count) >= sqs_max_receive_count
    except (TypeError, ValueError):
        return True


def publish(topic: str, payload, broker: dict) -> None:
    """Publishes one message to a broker with the WIS2 MQTT parameters (QoS 1, no retain) and waits for the PUBACK.

    Args:
        topic: MQTT topic.
        payload: str or bytes payload.
        broker: Broker connection dict from ``brokers``.

    Raises:
        ConnectionError: The broker refused the connection, refused the message (for example an ACL denial),
            or did not answer in time. Each step (TCP connect, TLS handshake, CONNACK, PUBACK) is bounded by
            PUBLISH_TIMEOUT_SECONDS.
        OSError: Network or TLS failure, including a broker certificate that fails verification.
        RuntimeError: paho could not queue the message.
    """
    acks = {}
    connected = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        acks["connect"] = reason_code
        connected.set()

    def on_publish(client, userdata, mid, reason_code, properties):
        acks["publish"] = reason_code

    # no reconnect: a reconnect would resend the QoS 1 message; a failure here is retried through SQS instead
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"wis2_{randint(0, 100000000)}",
                         protocol=mqtt.MQTTv5, reconnect_on_failure=False)
    client.connect_timeout = PUBLISH_TIMEOUT_SECONDS
    client.username_pw_set(broker['username'], broker['password'])
    client.tls_set(tls_version=ssl.PROTOCOL_TLSv1_2)
    # rollback for broker certificate problems: set True (README "Broker TLS and publish acknowledgements")
    client.tls_insecure_set(False)
    client.on_connect = on_connect
    client.on_publish = on_publish
    # keepalive also bounds the blocking TLS handshake in connect()
    client.connect(broker['host'], broker['port'], keepalive=PUBLISH_TIMEOUT_SECONDS)
    client.loop_start()
    try:
        if not connected.wait(PUBLISH_TIMEOUT_SECONDS):
            raise ConnectionError(f"no CONNACK from {broker['host']} within {PUBLISH_TIMEOUT_SECONDS}s")
        if acks["connect"].is_failure:
            raise ConnectionError(f"broker refused connection: {acks['connect']}")
        info = client.publish(topic, payload, qos=1)
        info.wait_for_publish(PUBLISH_TIMEOUT_SECONDS)
        if "publish" not in acks:
            raise ConnectionError(f"no PUBACK from {broker['host']} within {PUBLISH_TIMEOUT_SECONDS}s")
        if acks["publish"].is_failure:
            raise ConnectionError(f"broker refused publish to {topic}: {acks['publish']}")
    finally:
        client.disconnect()
        client.loop_stop()


def source_host(wis2_msg: Wis2Message) -> str:
    """Returns the data server hostname for WME titles, without userinfo or port.

    Args:
        wis2_msg: The notification message the event is about.

    Returns:
        The hostname, or "unknown" when the source link is missing or unparseable.
    """
    try:
        return urlsplit(wis2_msg.src_link).hostname or "unknown"
    except (TypeError, ValueError, AttributeError):
        return "unknown"


def publish_wme(wis2_msg: Wis2Message, event_type: str, severity: str, title: str, description: str = None,
                dedup_suffix: str = None, rate_limited: bool = True) -> bool:
    """Publishes one WME about the data item in wis2_msg, at most once per item and event.

    Never raises: a monitoring failure must not affect caching.

    Args:
        wis2_msg: The notification message the event is about.
        event_type: WME event type.
        severity: WME severity.
        title: Event title.
        description: Optional event description.
        dedup_suffix: Extra dedup key part, e.g. the data server for per-source failures.
        rate_limited: Apply the per (subject, type) rate limit.

    Returns:
        True if the event was published.
    """
    dedup_key = None
    log_ctx = {"event_type": event_type, "data_id": wis2_msg.data_id, "topic": wis2_msg.topic}
    try:
        if not wis2_monitor.is_centre_id(report_by):
            logger.error("REPORT_BY is not a valid centre-id; WME not published: report_by=%r", report_by,
                         extra=log_ctx)
            return False
        topic_keys = wis2_msg.topic.split('/')
        # a failure on another GC's cache/ copy says nothing reliable about the origin node (audit IQ-2)
        if event_type != wis2_monitor.EVENT_ITEM_CACHE and topic_keys[0] != 'origin':
            return False
        subject = topic_keys[3]
        wme =wis2_monitor.build_wme(event_type, severity, report_by, subject, title, description,
                                     channel=wis2_msg.topic)
        topic = wis2_monitor.monitor_topic(subject)
        payload = wis2_monitor.encode_wme(wme)
        dedup_key = "|".join(["wme", event_type, wis2_msg.data_id, wis2_msg.pubtime] +
                             ([dedup_suffix] if dedup_suffix else []))
        if not wis2_monitor.claim_event(redis_host, dedup_key, ttl_minutes * 60):
            return False
        if rate_limited and not wis2_monitor.within_rate_limit(
                redis_host, subject, event_type, wme_rate_limit, wme_rate_window_seconds, time.time()):
            return False
        for broker in brokers:
            publish(topic, payload, broker)
        logger.warning("published WME: type=%r subject=%r data_id=%r", event_type, subject, wis2_msg.data_id,
                       extra={**log_ctx, "wme_id": wme['id'], "monitor_topic": topic})
        return True
    except Exception:
        logger.error("failed to publish WME: type=%r data_id=%r\n%s", event_type, wis2_msg.data_id,
                     sanitized_traceback(), extra=log_ctx)
        if dedup_key is not None:
            # release the claim so a later copy of the notification can retry the event
            try:
                redis_host.delete(dedup_key)
            except Exception:
                logger.error("failed to release WME claim: key=%r\n%s", dedup_key, sanitized_traceback(),
                             extra=log_ctx)
        return False


def fetch_validated(wis2_msg: Wis2Message) -> bytes:
    """Gets the resource bytes, preferring embedded content, and validates integrity.

    Embedded content that cannot be decoded, has the wrong size, or fails integrity falls back to the URL
    (Guide 2.7.4.1).

    Args:
        wis2_msg: Message whose resource is fetched.

    Returns:
        Validated resource bytes.

    Raises:
        TooLargeToCache: The resource exceeds the cache size limit.
        DownloadError: The resource could not be downloaded.
        IntegrityError: The resource failed integrity validation.
    """
    try:
        data_bytes = wis2_msg.cache_msg_data(use_content=True)
        wis2_msg.validate_integrity()
        return data_bytes
    except (ContentDecodeError, IntegrityError) as content_error:
        if not wis2_msg.used_content:
            raise
        logger.warning("embedded content invalid; downloading instead: data_id=%r error_type=%s", wis2_msg.data_id,
                       type(content_error).__name__,
                       extra={"data_id": wis2_msg.data_id, "topic": wis2_msg.topic})
        publish_wme(wis2_msg, wis2_monitor.EVENT_ITEM_INTEGRITY, "WARNING",
                    title=f"Embedded content is invalid; downloading instead: {wis2_msg.data_id}",
                    description=str(content_error), dedup_suffix="content")
    data_bytes = wis2_msg.cache_msg_data(use_content=False)
    wis2_msg.validate_integrity()
    return data_bytes


def timer(func):
    """
    decorator to time function execution
    Parameters
    ----------
    func - function to time
    prints function name and execution time
    Returns - wrapper function
    -------

    """

    @wraps(func)
    def wrapper(*args):
        st = time.time();
        v = func(*args)
        print(f"{func.__name__} - {time.time() - st} seconds")
        return v

    return wrapper


def nested_get(d, keys):
    """
    gets value of nested key/s in dict
    :param d: dict
    :param keys: list of keys
    :return: value of nested key
    """
    for key in keys:
        try:
            d = d[key]
        except KeyError:
            # print(f'key "{key}" not found in {d}')
            return None
    return d


def parse_url(msg: dict):
    """
    parses the url from the message
    Parameters
    ----------
    msg: dict - message from wis2 broker

    Returns
    -------
    str - url to download data from
    """
    try:
        links = msg['links']
        canonical = [link for link in links if link['rel'] == 'canonical'][0]
        url = canonical['href']
        return url
    except Exception as e:
        logging.warning(f"failed to parse href from message {msg['id']}")
        logging.info(json.dumps(msg))
        raise e


def get_dt_str():
    # RFC3339
    """
    gets current datetime in RFC3339 format
    Returns - str of current datetime in RFC3339 format
    -------

    """
    return dt.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


def cache_metric(cache_client, key, cache_value, operation='set'):
    """
    cache in redis
    Parameters
    ----------
    cache_client - redis client
    key - str - key to store value
    cache_value - str - value to cache
    operation - str - operation to perform (set, get, delete)

    Returns
    -------
    value stored in cache

    """
    # cache_client = redis.client(os.environ.get('CACHE_ENDPOINT'), 6379)
    try:
        if cache_client is None:
            print(f"no cache client, not caching {key}")
            return
        if operation == 'set':
            return cache_client.set(key, cache_value)
        elif operation == 'inc':
            inc_val = 1 if cache_value is None else int(cache_value)
            return cache_client.incr(key, inc_val)
        elif operation == 'delete':
            return cache_client.delete(key)
        else:
            raise ValueError(f"operation {operation} not supported")
    except Exception as e:
        print(f"failed to cache {key} with value {cache_value}")
        # raise e

def cleanup_tmp_directory():
    """
    Cleans up old files in the /tmp directory to prevent disk space issues
    """
    try:
        # Clean up any files in /tmp that might be left from previous executions
        files = glob.glob("/tmp/*")
        for f in files:
            try:
                if os.path.isfile(f):
                    os.remove(f)
                    print(f"Cleaned up old file: {f}")
            except Exception as e:
                print(f"Failed to clean up file {f}: {e}")
        return len(files)
    except Exception as e:
        print(f"Error cleaning up /tmp directory: {e}")
        return 0


def msg_handler(msg_batch, context):
    """
    main handler function to handle wis2 messages
    Parameters
    ----------
    msg_batch - dict - batch of messages from sqs
    context

    Returns
    -------

    """
    # cleanup tmp directory
    tmp_files_cleaned = cleanup_tmp_directory()

    # setup metrics
    # metrics.set_property("MetricName", "WIS2GlobalCache")
    # check if 'Records' key exists in msg_batch
    if 'Records' in msg_batch:
        msg_batch = msg_batch['Records']
    batch_item_failures = []
    # handle if msg_batch is a single msg
    if not isinstance(msg_batch, list):
        msg_batch = [msg_batch]

    for sqs_msg in msg_batch:
        wis2_msg = None
        msg_centre = None
        try:
            # if body is a string, convert to dict
            if isinstance(sqs_msg['body'], str):
                msg_body = json.loads(sqs_msg['body'])
            else:
                msg_body = sqs_msg['body']
            wis2_msg = Wis2Message(msg_body, env)
            # topic split
            topic_keys = wis2_msg.topic.split('/')
            msg_centre = topic_keys[3]
            # check last cached
            last_cached = redis_host.get(wis2_msg.data_id)
            # print(f"received message: {wis2_msg.data_id}-{wis2_msg.pubtime}")
            if not wis2_msg.is_unique(last_cached):
                print(f"non-unique: {wis2_msg.data_id}-{wis2_msg.pubtime}")
                continue
            else:
                if wis2_msg.do_cache:
                    # the GC should cache the message
                    try:
                        # print(f'caching: {wis2_msg.data_id}-{wis2_msg.pubtime}')
                        cached_bytes = fetch_validated(wis2_msg)
                    except TypeError:
                        logger.warning("bad source link, message dropped: data_id=%r", wis2_msg.data_id,
                                       extra={"data_id": wis2_msg.data_id, "topic": wis2_msg.topic})
                        continue
                    except TooLargeToCache as e:
                        # GC decision not to cache (Guide gs:182): republish as pass-through and notify via WME
                        logger.warning("not caching oversize resource: data_id=%r", wis2_msg.data_id,
                                       extra={"data_id": wis2_msg.data_id, "topic": wis2_msg.topic, "reason": str(e)})
                        wis2_msg.do_cache = False
                        publish_wme(wis2_msg, wis2_monitor.EVENT_ITEM_CACHE, "INFO",
                                    title=f"Global Cache decided not to cache data item: {wis2_msg.data_id}",
                                    description=f"Resource too large to cache: {e}", rate_limited=False)
                    except IntegrityError as e:
                        print(f"failed integrity validation: {wis2_msg.data_id}")
                        integrity_status = cache_metric(redis_host, "|".join(
                            [msg_centre, 'wmo_wis2_gc_integrity_failed_total']), cache_value=1, operation='inc')
                        # metrics.put_metric("wmo_wis2_gc_integrity_failed", 1)
                        publish_wme(wis2_msg, wis2_monitor.EVENT_ITEM_INTEGRITY, "ERROR",
                                    title=f"File integrity failed: {source_host(wis2_msg)}",
                                    description=f"{e}; source: {wis2_msg.src_link}",
                                    dedup_suffix=wis2_msg.dataserver)
                        raise e
                if wis2_msg.do_cache:
                    # good to go - cache the data object:
                    bucket_path = wis2_msg.upload_to_bucket(cached_bytes)
                    # done with data object - delete it
                    if hasattr(wis2_msg, 'tmp_path'):
                        os.remove(wis2_msg.tmp_path)
                    del cached_bytes
                    gc.collect()
                # otherwise - this is a pass through message, we relay but do not cache the data object
                # nx sets only if key does not exist, returns True if successful
                # is_new = redis_host.set(wis2_msg.data_id, wis2_msg.pubtime, ex=ttl_minutes * 60, nx=True)
                # check uniqueness again
                last_cached = redis_host.get(wis2_msg.data_id)
                if not wis2_msg.is_unique(last_cached):
                    print(f"non-unique (last minute dump): {wis2_msg.data_id}-{wis2_msg.pubtime}")
                    continue
                else:
                    print(f"is_unique: {wis2_msg.data_id}-{wis2_msg.pubtime}")
                    # then we haven't seen it before, or it's an update
                    # redis set with ttl
                    redis_host.set(wis2_msg.data_id, wis2_msg.pubtime_epoch, ex=ttl_minutes * 60)
                # now format message, even if we did not cache it (pass through)
                notification_msg = wis2_msg.format_cache_msg()
                # send to mqtt broker/s
                for broker in brokers:
                    try:
                        publish(wis2_msg.new_topic, json.dumps(notification_msg), broker)
                        # print(f"published data_id {wis2_msg.data_id} to {broker['host']} on topic {wis2_msg.new_topic}")
                    except Exception as e:
                        print(
                            f"failed to publish data_id {wis2_msg.data_id} to {broker['host']} on topic {wis2_msg.new_topic}")
                        if not dev_mode:
                            # the dedup key is already set; release it or the SQS retry is skipped as a duplicate
                            try:
                                redis_host.delete(wis2_msg.data_id)
                            except Exception:
                                logger.error("failed to release dedup key; retry will be skipped: data_id=%r",
                                             wis2_msg.data_id, exc_info=True, extra={"data_id": wis2_msg.data_id})
                            raise PublishError(f"{type(e).__name__} publishing to {wis2_msg.new_topic}") from e
                # counted after the publish so a record retried for a publish failure is counted once
                if wis2_msg.do_cache:
                    dnld_total = cache_metric(redis_host, "|".join(
                        [msg_centre, 'wmo_wis2_gc_downloaded_total']), cache_value=1, operation='inc')
                    dnld_last = cache_metric(redis_host, "|".join(
                        [msg_centre, wis2_msg.dataserver, 'wmo_wis2_gc_dataserver_last_download_timestamp_seconds']),
                                             cache_value=int(time.time()), operation='set')
                    dataserver_status = cache_metric(redis_host, "|".join(
                        [msg_centre, wis2_msg.dataserver, 'wmo_wis2_gc_dataserver_status_flag']), cache_value=1,
                                                     operation='set')
                if not wis2_msg.do_cache:
                    # then increase wmo_wis2_gc_no_cache_total
                    no_cache_total = cache_metric(redis_host, "|".join(
                        [msg_centre, 'wmo_wis2_gc_no_cache_total']), cache_value=1, operation='inc')

        except Exception as e:
            tb = sanitized_traceback()
            logger.error("failed to process message: %s\n%s", sqs_msg['messageId'], tb)
            # GC_* markers are counted by CloudWatch metric filters (deploy/stacks/wis2_lambda_stack.py)
            fail_ctx = {"sqs_message_id": sqs_msg['messageId'], "error_type": type(e).__name__,
                        "data_id": getattr(wis2_msg, 'data_id', None),
                        "dataserver": getattr(wis2_msg, 'dataserver', None),
                        "receive_count": sqs_msg.get('attributes', {}).get('ApproximateReceiveCount')}
            transient = is_transient(e)
            fail_ctx["final_attempt"] = is_final_attempt(fail_ctx["receive_count"])
            if transient:
                batch_item_failures.append({"itemIdentifier": sqs_msg['messageId']})
                logger.warning("GC_RETRY transient failure returned to SQS: error_type=%s receive_count=%s "
                               "final_attempt=%s dataserver=%r data_id=%r", fail_ctx["error_type"],
                               fail_ctx["receive_count"], fail_ctx["final_attempt"], fail_ctx["dataserver"],
                               fail_ctx["data_id"], extra=fail_ctx)
            elif not isinstance(e, (DownloadError, IntegrityError)):
                logger.error("GC_UNCLASSIFIED_FAILURE not retried: error_type=%s dataserver=%r data_id=%r",
                             fail_ctx["error_type"], fail_ctx["dataserver"], fail_ctx["data_id"], extra=fail_ctx)
            if isinstance(e, DownloadError) and e.tls:
                logger.warning("GC_TLS_VERIFY_FAILED data server TLS/certificate check failed, item not cached: "
                               "dataserver=%r data_id=%r", fail_ctx["dataserver"], fail_ctx["data_id"], extra=fail_ctx)
            error_msg = {}
            # add error to cache_message
            error_topic = ['error']
            # check if wis2_msg exists
            if wis2_msg is not None:
                msg_topic = wis2_msg.topic
                error_topic = error_topic + msg_topic.split('/')
                # merge error message with original message
                error_msg = deepcopy(wis2_msg.msg)
            error_msg['error'] = {"msg": wis2_monitor.strip_url_queries(e), "traceback": tb}
            # just the first broker for now
            broker = brokers[0]
            error_topic = "/".join(error_topic)
            try:
                publish(error_topic, json.dumps(error_msg), broker)
                logger.error(f"published error msg: {error_topic}")
            except Exception:
                logger.error("failed to publish error msg: topic=%r\n%s", error_topic, sanitized_traceback(),
                             extra={"topic": error_topic, "sqs_message_id": sqs_msg['messageId']})
            logger.error("error msg: %s", wis2_monitor.strip_url_queries(json.dumps(error_msg)))
            if msg_centre is None:
                # the message never parsed far enough to identify a centre, so no download was attempted
                logger.error("message not parsed; download metrics not updated: sqs_message_id=%r",
                             sqs_msg['messageId'], extra={"sqs_message_id": sqs_msg['messageId']})
                continue
            # download metrics describe the data server (Guide 2.7.4.1): integrity failures have their own counter,
            # and S3/Redis/broker/parse errors are ours, so they show only in logs, GC_* metrics and error/
            if not isinstance(e, DownloadError):
                continue
            # an external ERROR event cannot be withdrawn, so a failure SQS will retry is reported only once it is final
            if not transient or fail_ctx["final_attempt"]:
                host = source_host(wis2_msg)
                publish_wme(wis2_msg, wis2_monitor.EVENT_ITEM_DOWNLOAD, "ERROR",
                            title=f"Cannot download data granule: {host}/{e.status}" if e.status
                            else f"Cannot download data granule: {host}",
                            description=f"{e}; source: {wis2_msg.src_link}", dedup_suffix=wis2_msg.dataserver)
            ds_name = wis2_msg.dataserver or 'unknown_dataserver'
            cache_metric(redis_host, "|".join([msg_centre, ds_name, 'wmo_wis2_gc_downloaded_errors_total']),
                         cache_value=1, operation='inc')
            if wis2_msg.dataserver is not None:
                cache_metric(redis_host, "|".join([msg_centre, wis2_msg.dataserver,
                                                   'wmo_wis2_gc_dataserver_status_flag']),
                             cache_value=0, operation='set')

    print({"batchItemFailures": len(batch_item_failures)})
    return {"batchItemFailures": batch_item_failures}

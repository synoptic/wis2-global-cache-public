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
import paho.mqtt.publish
import boto3
import requests as requests
from botocore.exceptions import ClientError
from datetime import datetime as dt
import paho.mqtt.client as mqtt
import time
import logging
from enum import Enum
import ssl
from wis2_message import Wis2Message

logger = logging.getLogger()
logger.setLevel(logging.WARN)

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
dev_mode = os.environ.get('DEV-MODE', 'False') not in ['True', 'true', '1']
logging.info(f"dev mode: {dev_mode}")


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
                        cached_bytes = wis2_msg.cache_msg_data(use_content=True)
                    except TypeError:
                        print(f"bad source link, skipping: {wis2_msg.data_id}")
                        continue
                    try:
                        # check if integrity block exists
                        if hasattr(wis2_msg, 'integrity_block'):
                            # then validate as this is not required
                            wis2_msg.validate_integrity()
                    except Exception as e:
                        print(f"failed integrity validation: {wis2_msg.data_id}")
                        integrity_status = cache_metric(redis_host, "|".join(
                            [msg_centre, 'wmo_wis2_gc_integrity_failed_total']), cache_value=1, operation='inc')
                        # metrics.put_metric("wmo_wis2_gc_integrity_failed", 1)
                        raise e
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
                # now format message, even if we did not cache it (pass through)
                notification_msg = wis2_msg.format_cache_msg()
                # send to mqtt broker/s
                for broker in brokers:
                    try:
                        client_id = f"wis2_{randint(0, 100000000)}"
                        # print(f"wis2 client id: {client_id}")
                        # Publish the message
                        paho.mqtt.publish.single(
                            wis2_msg.new_topic,
                            json.dumps(notification_msg),
                            hostname=broker['host'],
                            auth={'username': broker['username'], 'password': broker['password']},
                            port=broker['port'],
                            client_id=client_id,
                            protocol=mqtt.MQTTv5,
                            qos=1,
                            tls={'ca_certs': None, 'tls_version': ssl.PROTOCOL_TLSv1_2, 'insecure': True}
                        )
                        # print(f"published data_id {wis2_msg.data_id} to {broker['host']} on topic {wis2_msg.new_topic}")
                    except Exception as e:
                        print(
                            f"failed to publish data_id {wis2_msg.data_id} to {broker['host']} on topic {wis2_msg.new_topic}")
                        if not dev_mode:
                            raise e

        except Exception as e:
            logger.error(f"failed to process message: {sqs_msg['messageId']}", exc_info=True)
            batch_item_failures.append({"itemIdentifier": sqs_msg['messageId']})
            error_msg = {}
            # add error to cache_message
            error_topic = ['error']
            # check if wis2_msg exists
            if wis2_msg is not None:
                msg_topic = wis2_msg.topic
                error_topic = error_topic + msg_topic.split('/')
                # merge error message with original message
                error_msg = deepcopy(wis2_msg.msg)
            error_msg['error'] = {"msg": str(e), "traceback": traceback.format_exc()}
            # just the first broker for now
            broker = brokers[0]
            client_id = f"wis2_{randint(0, 100000000)}"
            error_topic = "/".join(error_topic)
            paho.mqtt.publish.single(
                error_topic,
                json.dumps(error_msg),
                hostname=broker['host'],
                auth={'username': broker['username'], 'password': broker['password']},
                port=broker['port'],
                client_id=client_id,
                protocol=mqtt.MQTTv5,
                qos=1,
                tls={'ca_certs': None, 'tls_version': ssl.PROTOCOL_TLSv1_2, 'insecure': True}
            )
            logger.error(f"published error msg: {error_topic}")
            logger.error(f"error msg: {json.dumps(error_msg)}")
            # metrics
            # todo - move parsing of these metrics components and or the metrics interactions to a different place
            ds_name = 'unknown_dataserver'
            if wis2_msg is not None and wis2_msg.dataserver is not None:
                ds_name = wis2_msg.dataserver
            dnld_error_total = cache_metric(redis_host, "|".join(
                [msg_centre, ds_name, 'wmo_wis2_gc_downloaded_errors_total']), cache_value=1,
                                            operation='inc')
            if wis2_msg.dataserver is not None and wis2_msg.dataserver is not None:
                dataserver_status = cache_metric(redis_host, "|".join(
                    [msg_centre, wis2_msg.dataserver, 'wmo_wis2_gc_dataserver_status_flag']), cache_value=0,
                                                 operation='set')

    sqs_batch_response = {"batchItemFailures": []}  # hard coded due to issue with batchItemFailures
    print({"batchItemFailures": len(batch_item_failures)})
    return sqs_batch_response

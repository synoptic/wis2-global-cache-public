import json
import logging
import os
from json import JSONDecodeError
import re
from urllib.parse import urlparse

import boto3
import dotenv
import paho.mqtt.client as mqtt
from cachetools import TTLCache
from datetime import datetime, timedelta
from paho.mqtt.properties import Properties
from paho.mqtt.packettypes import PacketTypes
from botocore.exceptions import ClientError
import threading
import time

# Set log level and format
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

# Add a StreamHandler to output logs to the console
# console_handler = logging.StreamHandler()
# console_handler.setLevel(logging.DEBUG)
# console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
# logger.addHandler(console_handler)

dotenv.load_dotenv()

SESSION_EXPIRY = 300
# random 6 character string
MQTT_CLIENT_ID = f"UK-US-GC-{os.urandom(6).hex()}"
logger.debug(f"MQTT_CLIENT_ID: {MQTT_CLIENT_ID}")
global id_cache
id_cache = TTLCache(maxsize=100000, ttl=timedelta(minutes=60), timer=datetime.now)

def on_connect(client, userdata, flags, reason_code, properties):
    logger.info(f"Connected to host. client id: {MQTT_CLIENT_ID}")
    if flags.session_present:
        # Handle session present
        pass
    if reason_code == 0:
        # success connect
        for topic in [
            "origin/a/wis2/+/data/core/#",
            "origin/a/wis2/+/metadata/#",
            "cache/a/wis2/+/data/core/#",
            "cache/a/wis2/+/metadata/#"
        ]:
            client.subscribe(topic, qos=1)
    if reason_code > 0:
        # error processing
        logger.error("client subscription error.")
        pass

def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    if reason_code == 0:
        # success disconnect
        logger.warning("Disconnected successfully with reason code 0")
        pass
    if reason_code > 0:
        # error processing
        logger.error("Unexpected disconnection.")
        # Implement reconnection logic with exponential backoff
        backoff_time = 1  # initial backoff time in seconds
        while not client.is_connected():
            try:
                client.reconnect()
                logger.info("Reconnected successfully")
                break  # exit the loop if reconnected successfully
            except Exception as e:
                logger.error(f"Reconnection failed, sleeping for {backoff_time} seconds", exc_info=True)
                time.sleep(backoff_time)  # Wait before retrying
                backoff_time = min(backoff_time * 2, 60)  # Exponential backoff with a max of 60 seconds

def on_message(client, userdata, message):
    global id_cache
    try:
        message_json = json.loads(message.payload)
        message_json['topic'] = message.topic
        data_id = message_json['properties']['data_id']
        msg_id = message_json['id']
        if 'links' not in message_json:
            logger.debug(f'no links: {data_id}')
            return
        hrefs = [link.get('href') for link in message_json['links']]
        if any(destination_bucket_name in href for href in hrefs):
            logger.debug(f'already cached: {data_id}')
            return
        if not is_cached(msg_id):
            msg_grp_data_id = re.sub(r'\W+', '', data_id)[-127:]
            response = queue.send_message(MessageBody=json.dumps(message_json),
                                          MessageGroupId=msg_grp_data_id)
            logger.debug(
                "Response from SQS id: %s", response.get('MessageId'))
        else:
            logger.debug("--Duplicate message '%s'--Not sent to SQS", message_json['properties']['data_id'])
    except JSONDecodeError as error:
        logger.exception("Received message with invalid json: %s", message.payload)
    except ClientError as error:
        logger.exception("Failed to send message to SQS Queue: %s", message.payload)
    except Exception as error:
        logger.exception("Failed to process message: %s", message.payload)

def is_cached(message_id):
    global id_cache
    try:
        id_cache[message_id]
        return True
    except:
        id_cache[message_id] = 1
        logger.debug(f"cached message id {message_id}\n cache size: {id_cache.currsize}")
        return False

def parse_connection_string(connection_string:str):
    s1 = connection_string.split(':')
    user = s1[1].strip('/')
    password = s1[2].split('@')[0]
    host = s1[2].split('@')[1].strip('/')
    port = int(s1[3].strip('/'))
    return user, password, host, port

def monitor_in_flight(client):
    print("Monitoring queue size")
    while True:
        in_flight = len(client._out_messages)
        if in_flight > 0:
            logger.info(f"Count in_flight: {in_flight}")
        time.sleep(1)

def main():
    global queue
    sqs = boto3.resource('sqs')
    queue_name = os.getenv('QUEUE_NAME')
    queue = sqs.get_queue_by_name(QueueName=queue_name)
    connection_string = os.getenv('GB_CONNECTION_STRING')
    global destination_bucket_name
    destination_bucket_name = os.getenv('BUCKET_NAME')
    global id_cache
    id_cache = TTLCache(maxsize=100000, ttl=timedelta(minutes=30), timer=datetime.now)
    connection_info = urlparse(connection_string)
    logger.debug(f"Connection info hostname: {connection_info.hostname}")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID, protocol=mqtt.MQTTv5)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.username_pw_set(connection_info.username, connection_info.password)

    client.max_queued_messages_set(20000)
    client.max_inflight_messages_set(10000)

    properties = Properties(PacketTypes.CONNECT)
    properties.SessionExpiryInterval = SESSION_EXPIRY
    client.tls_set()
    # client.will_set(
    #     topic="cache/a/wis2/+/status",
    #     payload=json.dumps({"status": "disconnected", "client_id": MQTT_CLIENT_ID}),
    #     qos=1,
    #     retain=True
    # )
    client.connect(
        host=connection_info.hostname,
        port=connection_info.port,
        clean_start=False,
        keepalive=60*5,
        properties=properties)
    logger.debug(f"Connection status: {client.is_connected()}")
    client.enable_logger(logger)

    threading.Thread(target=monitor_in_flight, args=(client,)).start()
    client.loop_forever()

if __name__ == "__main__":
    main()
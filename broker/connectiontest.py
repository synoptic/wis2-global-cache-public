# test double connection
from random import randint

import paho.mqtt.client as mqtt

# define mqtt connection and reuse for 'hot' lambda invocations
client_id = f"wis2_{randint(0, 100000000)}"
# spin up a client for each broker
brokers = [
    {"host": 'dev-cache.wis2.synopticdata.com', "port": 1883, "username": 'synoptic_admin', "password": 'e1758b10-2cc8-11ee-be56-0242ac120002'},
]

def connect_to_broker(broker):
    mqtt_client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv5)
    mqtt_client.username_pw_set(broker['username'], broker['password'])
    mqtt_client.connect(broker['host'], port=broker['port'])
    print(f"connected to broker {broker['host']} on port {broker['port']}")
    return mqtt_client
mqtt_clients = [mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv5) for b in brokers]
for i, mqtt_client in enumerate(mqtt_clients):
    mqtt_client.username_pw_set(brokers[i]['username'], brokers[i]['password'])
    mqtt_client.connect(brokers[i]['host'], port=brokers[i]['port'])
    print(f"connected to broker {brokers[i]['host']} on port {brokers[i]['port']}")

# publish a message to each broker
for i, mqtt_client in enumerate(mqtt_clients):
    mqtt_client.publish(f"error/test/a/wis2/test{i}", payload="test", qos=1)
    print(f"published message to broker {brokers[i]['host']}")

dc = mqtt_clients[0].connect(brokers[0]['host'], port=brokers[0]['port'])
print(dc)

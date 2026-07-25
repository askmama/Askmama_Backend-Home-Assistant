import json
import ssl
import threading
import paho.mqtt.client as mqtt
from app.core.config import settings
from app.core.identity import DeviceIdentityError, resolve_device_id
from app.inventory.service import process_weight_event

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print("Connected to HiveMQ Cloud")
        client.subscribe("askmama/+/weight_event", qos=1)
    else:
        print(f"MQTT connection failed: code {rc}")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        device_id = resolve_device_id(msg.topic, payload)
        process_weight_event(device_id, payload)
    except DeviceIdentityError as e:
        # Misconfigured firmware, not a transient fault — drop rather than
        # file the event under a device (and owner) we cannot confirm.
        print(f"DROPPED event on {msg.topic}: {e}")
    except Exception as e:
        print(f"Error processing MQTT message: {type(e).__name__}: {e}")

def start_mqtt():
    tls_context = ssl.create_default_context()

    client = mqtt.Client(
        client_id="askmama-backend",
        protocol=mqtt.MQTTv5,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2
    )
    client.tls_set_context(tls_context)
    client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(settings.MQTT_BROKER, settings.MQTT_PORT, keepalive=60)

    thread = threading.Thread(target=client.loop_forever, daemon=True)
    thread.start()
    print("MQTT listener started")
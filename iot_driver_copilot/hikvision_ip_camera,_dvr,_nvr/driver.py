import os
import sys
import time
import signal
import threading
import base64
import json
import yaml
from typing import Any, Dict

import paho.mqtt.client as mqtt

from kubernetes import client, config
from kubernetes.client.rest import ApiException

CONFIGMAP_PATH = '/etc/edgedevice/config/instructions.yaml'
CRD_GROUP = 'shifu.edgenesis.io'
CRD_VERSION = 'v1alpha1'
CRD_PLURAL = 'edgedevices'
CRD_KIND = 'Edgedevice'

# Environment variables
EDGEDEVICE_NAME = os.environ.get('EDGEDEVICE_NAME')
EDGEDEVICE_NAMESPACE = os.environ.get('EDGEDEVICE_NAMESPACE')
MQTT_BROKER_ADDRESS = os.environ.get('MQTT_BROKER_ADDRESS')

if not EDGEDEVICE_NAME or not EDGEDEVICE_NAMESPACE or not MQTT_BROKER_ADDRESS:
    sys.stderr.write('Required environment variables: EDGEDEVICE_NAME, EDGEDEVICE_NAMESPACE, MQTT_BROKER_ADDRESS\n')
    sys.exit(1)

# --- Load instructions.yaml ---
def load_instructions() -> Dict[str, Any]:
    if not os.path.exists(CONFIGMAP_PATH):
        return {}
    with open(CONFIGMAP_PATH, 'r') as f:
        return yaml.safe_load(f) or {}

INSTRUCTIONS = load_instructions()

def get_api_settings(api_name: str) -> Dict[str, Any]:
    return INSTRUCTIONS.get(api_name, {}).get('protocolPropertyList', {})

# --- Kubernetes CRD Status Management ---
def get_k8s_api():
    try:
        config.load_incluster_config()
    except Exception as e:
        sys.stderr.write(f'Failed to load incluster config: {e}\n')
        sys.exit(1)
    return client.CustomObjectsApi()

def update_edgedevice_status(phase: str):
    api = get_k8s_api()
    status_obj = {'status': {'edgedevicephase': phase}}
    try:
        api.patch_namespaced_custom_object_status(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=EDGEDEVICE_NAMESPACE,
            plural=CRD_PLURAL,
            name=EDGEDEVICE_NAME,
            body=status_obj
        )
    except ApiException as e:
        sys.stderr.write(f"Failed to update Edgedevice status: {e}\n")

# --- MQTT Client Logic ---
class CameraMQTTClient:
    def __init__(self, broker_address, topic, qos):
        self.broker_host, self.broker_port = self._parse_broker_address(broker_address)
        self.topic = topic
        self.qos = qos
        self.client = mqtt.Client()
        self.connected = False
        self.should_stop = threading.Event()
        self._setup_callbacks()

    def _parse_broker_address(self, addr: str):
        if ':' not in addr:
            return addr, 1883
        host, port = addr.rsplit(':', 1)
        return host, int(port)

    def _setup_callbacks(self):
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    def connect(self):
        try:
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.connected = True
            update_edgedevice_status('Running')
        except Exception as e:
            sys.stderr.write(f'MQTT connection failed: {e}\n')
            self.connected = False
            update_edgedevice_status('Failed')
            return
        self.client.loop_start()

    def disconnect(self):
        self.should_stop.set()
        self.client.loop_stop()
        self.client.disconnect()
        self.connected = False
        update_edgedevice_status('Pending')

    def subscribe(self):
        try:
            self.client.subscribe(self.topic, qos=self.qos)
        except Exception as e:
            sys.stderr.write(f'Failed to subscribe: {e}\n')
            update_edgedevice_status('Failed')

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            update_edgedevice_status('Running')
            self.subscribe()
        else:
            sys.stderr.write('Failed to connect to MQTT broker, return code %d\n' % rc)
            self.connected = False
            update_edgedevice_status('Failed')

    def on_disconnect(self, client, userdata, rc):
        self.connected = False
        if not self.should_stop.is_set():
            update_edgedevice_status('Pending')

    def on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode('utf-8')
            data = json.loads(payload)
            # Example: print frame metadata and truncate frame data for log brevity
            frame_info = {
                k: v for k, v in data.items() if k != 'frame'
            }
            print(f"[Camera Frame Received] Topic: {msg.topic}, Metadata: {frame_info}")
            # Frame data (base64) is available as data['frame']
        except Exception as e:
            sys.stderr.write(f'Error handling incoming frame: {e}\n')

    def loop_forever(self):
        while not self.should_stop.is_set():
            if not self.connected:
                try:
                    self.connect()
                except Exception:
                    update_edgedevice_status('Failed')
                    time.sleep(5)
            time.sleep(1)

def graceful_shutdown(sig, frame, client: CameraMQTTClient):
    client.disconnect()
    update_edgedevice_status('Pending')
    sys.exit(0)

def main():
    # Get MQTT topic settings from configmap or default
    settings = get_api_settings('device/camera/frame')
    topic = settings.get('topic', 'device/camera/frame')
    qos = int(settings.get('qos', 1))
    mqtt_client = CameraMQTTClient(MQTT_BROKER_ADDRESS, topic, qos)

    signal.signal(signal.SIGTERM, lambda sig, frm: graceful_shutdown(sig, frm, mqtt_client))
    signal.signal(signal.SIGINT, lambda sig, frm: graceful_shutdown(sig, frm, mqtt_client))

    try:
        update_edgedevice_status('Pending')
        mqtt_client.loop_forever()
    except Exception as e:
        sys.stderr.write(f'Unexpected error: {e}\n')
        update_edgedevice_status('Unknown')
        sys.exit(1)

if __name__ == '__main__':
    main()
import os
import sys
import time
import yaml
import json
import threading
from datetime import datetime
from typing import Any, Dict

from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

import paho.mqtt.client as mqtt

EDGEDEVICE_NAME = os.environ.get('EDGEDEVICE_NAME')
EDGEDEVICE_NAMESPACE = os.environ.get('EDGEDEVICE_NAMESPACE')
MQTT_BROKER_ADDRESS = os.environ.get('MQTT_BROKER_ADDRESS')
MQTT_CLIENT_ID = os.environ.get('MQTT_CLIENT_ID', f'deviceshifu-{EDGEDEVICE_NAME or "unknown"}')
MQTT_KEEPALIVE = int(os.environ.get('MQTT_KEEPALIVE', '60'))
MQTT_USERNAME = os.environ.get('MQTT_USERNAME')
MQTT_PASSWORD = os.environ.get('MQTT_PASSWORD')
MQTT_TLS_ENABLED = os.environ.get('MQTT_TLS_ENABLED', 'false').lower() == 'true'

INSTRUCTIONS_PATH = '/etc/edgedevice/config/instructions'

if not EDGEDEVICE_NAME or not EDGEDEVICE_NAMESPACE or not MQTT_BROKER_ADDRESS:
    print("Missing required environment variables.", file=sys.stderr)
    sys.exit(1)

# Kubernetes client setup
config.load_incluster_config()
crd = client.CustomObjectsApi()
core_v1 = client.CoreV1Api()

EDGEDEVICE_GROUP = 'shifu.edgenesis.io'
EDGEDEVICE_VERSION = 'v1alpha1'
EDGEDEVICE_PLURAL = 'edgedevices'

def get_edgedevice() -> Dict[str, Any]:
    try:
        return crd.get_namespaced_custom_object(
            EDGEDEVICE_GROUP, EDGEDEVICE_VERSION,
            EDGEDEVICE_NAMESPACE, EDGEDEVICE_PLURAL, EDGEDEVICE_NAME
        )
    except ApiException as e:
        return {}

def set_edgedevice_phase(phase: str):
    body = {
        "status": {
            "edgeDevicePhase": phase
        }
    }
    for _ in range(3):
        try:
            crd.patch_namespaced_custom_object_status(
                EDGEDEVICE_GROUP, EDGEDEVICE_VERSION, EDGEDEVICE_NAMESPACE,
                EDGEDEVICE_PLURAL, EDGEDEVICE_NAME, body
            )
            return
        except ApiException:
            time.sleep(2)

def get_device_address() -> str:
    ed = get_edgedevice()
    return ed.get('spec', {}).get('address', '')

def load_instructions() -> dict:
    try:
        with open(INSTRUCTIONS_PATH, 'r') as f:
            return yaml.safe_load(f)
    except Exception:
        return {}

class DeviceShifuMQTTClient:
    def __init__(self):
        self.broker_addr, self.broker_port = self._parse_broker_addr(MQTT_BROKER_ADDRESS)
        self.client_id = MQTT_CLIENT_ID
        self.keepalive = MQTT_KEEPALIVE
        self.username = MQTT_USERNAME
        self.password = MQTT_PASSWORD
        self.tls_enabled = MQTT_TLS_ENABLED

        self.client = mqtt.Client(client_id=self.client_id, clean_session=True)
        if self.username:
            self.client.username_pw_set(self.username, self.password)
        if self.tls_enabled:
            self.client.tls_set()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self.connected = False
        self.subscriptions = {}
        self.status_lock = threading.Lock()
        self.last_status = 'Pending'
        self.device_address = get_device_address()
        self.instructions = load_instructions()
        self.telemetry_data = None

        self.client_loop_thread = threading.Thread(target=self._loop_forever)
        self.client_loop_thread.daemon = True

    @staticmethod
    def _parse_broker_addr(addr: str):
        if ':' in addr:
            host, port = addr.rsplit(':', 1)
            return host, int(port)
        return addr, 1883

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = True
        self._update_status('Running')
        # Subscribe to telemetry
        self.subscribe_telemetry()

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        self._update_status('Pending' if rc != 0 else 'Unknown')

    def _on_message(self, client, userdata, msg):
        if msg.topic == 'device/telemetry/state':
            try:
                self.telemetry_data = json.loads(msg.payload.decode())
            except Exception:
                self.telemetry_data = None

    def _loop_forever(self):
        try:
            self.client.connect(self.broker_addr, self.broker_port, self.keepalive)
            self.client.loop_forever()
        except Exception:
            self._update_status('Failed')
            while True:
                time.sleep(5)

    def start(self):
        self.client_loop_thread.start()

    def _update_status(self, status):
        with self.status_lock:
            if self.last_status != status:
                set_edgedevice_phase(status)
                self.last_status = status

    def publish(self, topic, payload, qos=1):
        if not self.connected:
            raise RuntimeError("MQTT not connected")
        info = self.client.publish(topic, json.dumps(payload), qos=qos)
        info.wait_for_publish()
        return info.is_published()

    def subscribe_telemetry(self):
        self.client.subscribe('device/telemetry/state', qos=1)

    # Device API methods
    def move(self, rotate: float, forward: float, lateral: float) -> bool:
        topic = 'device/commands/move'
        payload = {'rotate': rotate, 'forward': forward, 'lateral': lateral}
        qos = self._get_api_qos('move')
        return self.publish(topic, payload, qos=qos)

    def stand(self) -> bool:
        topic = 'device/commands/stand'
        payload = {'command': 'stand'}
        qos = self._get_api_qos('stand')
        return self.publish(topic, payload, qos=qos)

    def heartbeat(self) -> bool:
        topic = 'device/commands/heartbeat'
        payload = {'timestamp': datetime.utcnow().isoformat() + 'Z'}
        qos = self._get_api_qos('heartbeat')
        return self.publish(topic, payload, qos=qos)

    def get_telemetry_state(self) -> Any:
        return self.telemetry_data

    def _get_api_qos(self, api_name: str) -> int:
        # Find in instructions or fallback to default
        inst = self.instructions.get(f'device/commands/{api_name}', {})
        protocol_list = inst.get('protocolPropertyList', {})
        return int(protocol_list.get('qos', 1))

# HTTP Flask API for user interaction (can be extended as needed)
from flask import Flask, request, jsonify

app = Flask(__name__)
ds_mqtt = DeviceShifuMQTTClient()
ds_mqtt.start()

@app.route('/move', methods=['POST'])
def api_move():
    params = request.json or {}
    rotate = float(params.get('rotate', 0))
    forward = float(params.get('forward', 0))
    lateral = float(params.get('lateral', 0))
    try:
        ok = ds_mqtt.move(rotate, forward, lateral)
        return jsonify({'success': ok}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/stand', methods=['POST'])
def api_stand():
    try:
        ok = ds_mqtt.stand()
        return jsonify({'success': ok}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/heartbeat', methods=['POST'])
def api_heartbeat():
    try:
        ok = ds_mqtt.heartbeat()
        return jsonify({'success': ok}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/telemetry/state', methods=['GET'])
def api_telemetry():
    data = ds_mqtt.get_telemetry_state()
    if data is not None:
        return jsonify(data), 200
    return jsonify({'error': 'No telemetry data'}), 404

def edgedevice_status_monitor():
    while True:
        time.sleep(15)
        if not ds_mqtt.connected:
            ds_mqtt._update_status('Pending')

if __name__ == '__main__':
    monitor_thread = threading.Thread(target=edgedevice_status_monitor)
    monitor_thread.daemon = True
    monitor_thread.start()
    app.run(host='0.0.0.0', port=int(os.environ.get('SHIFU_HTTP_PORT', '8080')))
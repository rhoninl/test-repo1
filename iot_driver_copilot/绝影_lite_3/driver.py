import os
import sys
import json
import yaml
import time
import threading
import signal

from kubernetes import client, config
from kubernetes.client.rest import ApiException

import paho.mqtt.client as mqtt

EDGEDEVICE_NAME = os.environ.get("EDGEDEVICE_NAME")
EDGEDEVICE_NAMESPACE = os.environ.get("EDGEDEVICE_NAMESPACE")
MQTT_BROKER_ADDRESS = os.environ.get("MQTT_BROKER_ADDRESS")
INSTRUCTION_CONFIG_PATH = "/etc/edgedevice/config/instructions"

if not EDGEDEVICE_NAME or not EDGEDEVICE_NAMESPACE or not MQTT_BROKER_ADDRESS:
    print("Missing required environment variables.", file=sys.stderr)
    sys.exit(1)

# Load Kubernetes in-cluster configuration
config.load_incluster_config()
crd_api = client.CustomObjectsApi()

CRD_GROUP = 'shifu.edgenesis.io'
CRD_VERSION = 'v1alpha1'
CRD_PLURAL = 'edgedevices'

# Load instructions config
def load_instruction_config():
    try:
        with open(INSTRUCTION_CONFIG_PATH, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Failed to load instructions config: {e}", file=sys.stderr)
        return {}

INSTRUCTION_CONFIG = load_instruction_config()

# MQTT Topics and QoS mapping from API info
API_DEFINITIONS = {
    "move": {
        "topic": "device/commands/move",
        "qos": 1,
        "type": "publish"
    },
    "stand": {
        "topic": "device/commands/stand",
        "qos": 1,
        "type": "publish"
    },
    "heartbeat": {
        "topic": "device/commands/heartbeat",
        "qos": 1,
        "type": "publish"
    },
    "telemetry_state": {
        "topic": "device/telemetry/state",
        "qos": 1,
        "type": "subscribe"
    }
}

# Status phases
PHASE_PENDING = "Pending"
PHASE_RUNNING = "Running"
PHASE_FAILED = "Failed"
PHASE_UNKNOWN = "Unknown"

# Global for telemetry cache
telemetry_state_cache = None
telemetry_state_lock = threading.Lock()

def update_edgedevice_phase(phase):
    try:
        # Fetch latest EdgeDevice
        ed = crd_api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=EDGEDEVICE_NAMESPACE,
            plural=CRD_PLURAL,
            name=EDGEDEVICE_NAME,
        )
        if 'status' not in ed:
            ed['status'] = {}
        ed['status']['edgeDevicePhase'] = phase
        crd_api.patch_namespaced_custom_object_status(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=EDGEDEVICE_NAMESPACE,
            plural=CRD_PLURAL,
            name=EDGEDEVICE_NAME,
            body={"status": ed['status']}
        )
    except ApiException as e:
        print(f"Failed to update EdgeDevice phase: {e}", file=sys.stderr)

def get_device_address():
    try:
        ed = crd_api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=EDGEDEVICE_NAMESPACE,
            plural=CRD_PLURAL,
            name=EDGEDEVICE_NAME,
        )
        return ed.get('spec', {}).get('address')
    except ApiException as e:
        print(f"Failed to get device address: {e}", file=sys.stderr)
        return None

# MQTT Client setup
class MQTTDeviceClient:
    def __init__(self, broker_address):
        self.broker_address = broker_address
        self.client = mqtt.Client()
        self.connected = threading.Event()
        self.should_stop = threading.Event()
        self.subscribe_topics = []
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected.set()
            update_edgedevice_phase(PHASE_RUNNING)
            for topic, qos in self.subscribe_topics:
                client.subscribe(topic, qos)
        else:
            update_edgedevice_phase(PHASE_FAILED)
            print(f"MQTT connect failed: {rc}", file=sys.stderr)

    def on_disconnect(self, client, userdata, rc):
        self.connected.clear()
        if not self.should_stop.is_set():
            update_edgedevice_phase(PHASE_FAILED)

    def on_message(self, client, userdata, msg):
        global telemetry_state_cache
        if msg.topic == API_DEFINITIONS["telemetry_state"]["topic"]:
            with telemetry_state_lock:
                try:
                    telemetry_state_cache = json.loads(msg.payload.decode())
                except Exception:
                    telemetry_state_cache = msg.payload.decode()

    def connect_and_loop(self):
        update_edgedevice_phase(PHASE_PENDING)
        try:
            self.client.connect(self.broker_address.split(":")[0],
                                int(self.broker_address.split(":")[1]))
        except Exception as e:
            update_edgedevice_phase(PHASE_FAILED)
            print(f"MQTT connect exception: {e}", file=sys.stderr)
            return

        self.client.loop_start()
        # Wait until connected or timeout
        for _ in range(30):
            if self.connected.is_set():
                return
            time.sleep(1)
        update_edgedevice_phase(PHASE_FAILED)

    def stop(self):
        self.should_stop.set()
        self.client.loop_stop()
        self.client.disconnect()
        update_edgedevice_phase(PHASE_UNKNOWN)

    def add_subscribe(self, topic, qos):
        self.subscribe_topics.append((topic, qos))

    def publish(self, topic, payload, qos):
        if not self.connected.is_set():
            raise Exception("MQTT not connected")
        result = self.client.publish(topic, json.dumps(payload), qos=qos)
        result.wait_for_publish()
        return result.rc == mqtt.MQTT_ERR_SUCCESS

# API Implementation
class DeviceShifu:
    def __init__(self):
        broker = MQTT_BROKER_ADDRESS
        self.mqtt_client = MQTTDeviceClient(broker)
        # Setup subscriptions
        telemetry_topic = API_DEFINITIONS["telemetry_state"]["topic"]
        telemetry_qos = API_DEFINITIONS["telemetry_state"]["qos"]
        self.mqtt_client.add_subscribe(telemetry_topic, telemetry_qos)
        self.mqtt_client.connect_and_loop()

    def shutdown(self):
        self.mqtt_client.stop()

    def publish_move(self, rotate=0, forward=0, lateral=0):
        # Instruction-based settings can be added here if needed
        payload = {"rotate": rotate, "forward": forward, "lateral": lateral}
        topic = API_DEFINITIONS["move"]["topic"]
        qos = API_DEFINITIONS["move"]["qos"]
        return self.mqtt_client.publish(topic, payload, qos)

    def publish_stand(self):
        payload = {"command": "stand"}
        topic = API_DEFINITIONS["stand"]["topic"]
        qos = API_DEFINITIONS["stand"]["qos"]
        return self.mqtt_client.publish(topic, payload, qos)

    def publish_heartbeat(self, timestamp=None):
        if timestamp is None:
            timestamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        payload = {"timestamp": timestamp}
        topic = API_DEFINITIONS["heartbeat"]["topic"]
        qos = API_DEFINITIONS["heartbeat"]["qos"]
        return self.mqtt_client.publish(topic, payload, qos)

    def get_telemetry_state(self):
        # Returns the most recent cached telemetry state
        with telemetry_state_lock:
            return telemetry_state_cache

# API HTTP Server
from flask import Flask, request, jsonify

app = Flask(__name__)
device_shifu = DeviceShifu()

@app.route("/move", methods=["POST"])
def api_move():
    data = request.get_json(force=True)
    rotate = data.get('rotate', 0)
    forward = data.get('forward', 0)
    lateral = data.get('lateral', 0)
    try:
        success = device_shifu.publish_move(rotate=rotate, forward=forward, lateral=lateral)
        return jsonify({"result": "success" if success else "failed"}), 200
    except Exception as e:
        return jsonify({"result": "failed", "error": str(e)}), 500

@app.route("/stand", methods=["POST"])
def api_stand():
    try:
        success = device_shifu.publish_stand()
        return jsonify({"result": "success" if success else "failed"}), 200
    except Exception as e:
        return jsonify({"result": "failed", "error": str(e)}), 500

@app.route("/heartbeat", methods=["POST"])
def api_heartbeat():
    timestamp = request.json.get("timestamp") if request.is_json else None
    try:
        success = device_shifu.publish_heartbeat(timestamp=timestamp)
        return jsonify({"result": "success" if success else "failed"}), 200
    except Exception as e:
        return jsonify({"result": "failed", "error": str(e)}), 500

@app.route("/telemetry/state", methods=["GET"])
def api_telemetry_state():
    try:
        state = device_shifu.get_telemetry_state()
        return jsonify({"state": state}), 200
    except Exception as e:
        return jsonify({"result": "failed", "error": str(e)}), 500

def sigterm_handler(signum, frame):
    device_shifu.shutdown()
    sys.exit(0)

signal.signal(signal.SIGTERM, sigterm_handler)
signal.signal(signal.SIGINT, sigterm_handler)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("HTTP_SERVER_PORT", "8080")))

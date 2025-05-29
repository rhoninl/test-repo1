import os
import signal
import sys
import threading
import time
import base64
import yaml
import json

from kubernetes import client, config
from kubernetes.client.rest import ApiException

import paho.mqtt.client as mqtt

EDGEDEVICE_NAME = os.environ.get("EDGEDEVICE_NAME")
EDGEDEVICE_NAMESPACE = os.environ.get("EDGEDEVICE_NAMESPACE")
MQTT_BROKER_ADDRESS = os.environ.get("MQTT_BROKER_ADDRESS")
INSTRUCTION_CONFIG_PATH = "/etc/edgedevice/config/instructions"

if not EDGEDEVICE_NAME or not EDGEDEVICE_NAMESPACE or not MQTT_BROKER_ADDRESS:
    raise RuntimeError("EDGEDEVICE_NAME, EDGEDEVICE_NAMESPACE, and MQTT_BROKER_ADDRESS must be set.")

# Constants for Edgedevice CRD
CRD_GROUP = "shifu.edgenesis.io"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "edgedevices"

# MQTT topic for video frame
MQTT_TOPIC_CAMERA_FRAME = "device/camera/frame"
MQTT_QOS_CAMERA_FRAME = 1

# Device phase constants
PHASE_PENDING = "Pending"
PHASE_RUNNING = "Running"
PHASE_FAILED = "Failed"
PHASE_UNKNOWN = "Unknown"

# Read instruction config
def read_instruction_config():
    try:
        with open(INSTRUCTION_CONFIG_PATH, "r") as f:
            return yaml.safe_load(f)
    except Exception:
        return {}

class EdgedeviceStatusManager:
    def __init__(self, name, namespace):
        # Use incluster config, fallback to kubeconfig for local dev
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        self.custom_api = client.CustomObjectsApi()
        self.name = name
        self.namespace = namespace
        self.last_phase = None

    def get_edgedevice(self):
        try:
            return self.custom_api.get_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=self.namespace,
                plural=CRD_PLURAL,
                name=self.name
            )
        except ApiException:
            return None

    def update_phase(self, phase):
        if self.last_phase == phase:
            return
        for _ in range(3):
            try:
                edgedevice = self.get_edgedevice()
                if not edgedevice:
                    continue
                # Patch status
                body = {"status": {"edgedevicephase": phase}}
                self.custom_api.patch_namespaced_custom_object_status(
                    group=CRD_GROUP,
                    version=CRD_VERSION,
                    namespace=self.namespace,
                    plural=CRD_PLURAL,
                    name=self.name,
                    body=body
                )
                self.last_phase = phase
                return
            except ApiException:
                time.sleep(1)

class MQTTFrameSubscriber:
    def __init__(self, broker_address, topic, qos, status_manager, protocol_settings):
        self.broker_address = broker_address
        self.topic = topic
        self.qos = qos
        self.status_manager = status_manager
        self.protocol_settings = protocol_settings
        self.client = mqtt.Client()
        self.connected = False
        self.should_stop = threading.Event()

        # On connect
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        # Optional: setup username/password or TLS
        if self.protocol_settings:
            username = self.protocol_settings.get("username")
            password = self.protocol_settings.get("password")
            if username:
                self.client.username_pw_set(username, password)
            # TLS, keepalive etc. can be set here from protocol_settings

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            self.status_manager.update_phase(PHASE_RUNNING)
            client.subscribe(self.topic, self.qos)
        else:
            self.status_manager.update_phase(PHASE_FAILED)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            self.status_manager.update_phase(PHASE_FAILED)
        else:
            self.status_manager.update_phase(PHASE_PENDING)

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode('utf-8')
            data = json.loads(payload)
            # Expected: {'timestamp': ..., 'frame_number': ..., 'frame': 'base64str'}
            # Here you would process the frame, for demo just print metadata
            print(f"Received frame: timestamp={data.get('timestamp')}, frame_number={data.get('frame_number')}, size={len(data.get('frame', ''))}")
        except Exception as e:
            print(f"Error decoding message: {e}")

    def start(self):
        broker, port = self._parse_broker_address(self.broker_address)
        try:
            self.client.connect(broker, port, keepalive=60)
        except Exception:
            self.status_manager.update_phase(PHASE_FAILED)
            return
        t = threading.Thread(target=self._loop_forever)
        t.daemon = True
        t.start()

    def _loop_forever(self):
        self.client.loop_start()
        while not self.should_stop.is_set():
            if not self.connected:
                self.status_manager.update_phase(PHASE_PENDING)
            time.sleep(2)
        self.client.loop_stop()

    def stop(self):
        self.should_stop.set()
        self.client.disconnect()

    @staticmethod
    def _parse_broker_address(address):
        if ':' in address:
            host, port = address.rsplit(':', 1)
            return host, int(port)
        return address, 1883

def main():
    # Handle termination signals
    stop_event = threading.Event()
    def handle_sigterm(*_):
        stop_event.set()
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    # Phase: Pending (initial)
    status_manager = EdgedeviceStatusManager(EDGEDEVICE_NAME, EDGEDEVICE_NAMESPACE)
    status_manager.update_phase(PHASE_PENDING)

    # Load protocol config for the API (if any)
    instructions = read_instruction_config()
    protocol_settings = {}
    api_conf = instructions.get("device/camera/frame")
    if api_conf and "protocolPropertyList" in api_conf:
        protocol_settings = api_conf["protocolPropertyList"]

    # Start MQTT frame subscriber
    subscriber = MQTTFrameSubscriber(
        broker_address=MQTT_BROKER_ADDRESS,
        topic=MQTT_TOPIC_CAMERA_FRAME,
        qos=MQTT_QOS_CAMERA_FRAME,
        status_manager=status_manager,
        protocol_settings=protocol_settings
    )
    subscriber.start()

    # Main loop: monitor & keep alive
    while not stop_event.is_set():
        time.sleep(1)

    subscriber.stop()
    status_manager.update_phase(PHASE_PENDING)

if __name__ == "__main__":
    main()
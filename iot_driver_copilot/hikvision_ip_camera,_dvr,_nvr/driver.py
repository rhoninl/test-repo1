import os
import sys
import base64
import yaml
import time
import threading
import logging
import signal
from io import BytesIO
from datetime import datetime

import requests
import cv2
import numpy as np

from kubernetes import client, config
from kubernetes.client.rest import ApiException

import paho.mqtt.client as mqtt

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

# Constants
INSTRUCTIONS_PATH = '/etc/edgedevice/config/instructions'
EDGEDEVICE_CRD_GROUP = 'shifu.edgenesis.io'
EDGEDEVICE_CRD_VERSION = 'v1alpha1'
EDGEDEVICE_CRD_PLURAL = 'edgedevices'
STATUS_PHASES = ["Pending", "Running", "Failed", "Unknown"]
FRAME_TOPIC = "device/camera/frame"
FRAME_QOS = 1
FRAME_PUBLISH_INTERVAL = 1 / 5  # 5 FPS default

# Get required ENV vars
def get_env(name, required=True):
    val = os.environ.get(name)
    if required and not val:
        logging.error(f"Missing required environment variable: {name}")
        sys.exit(1)
    return val

EDGEDEVICE_NAME = get_env("EDGEDEVICE_NAME")
EDGEDEVICE_NAMESPACE = get_env("EDGEDEVICE_NAMESPACE")
MQTT_BROKER_ADDRESS = get_env("MQTT_BROKER_ADDRESS")

# Load instructions/config (YAML)
def load_instructions(path):
    try:
        with open(path, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logging.error(f"Failed to read instructions file: {e}")
        return {}

instructions = load_instructions(INSTRUCTIONS_PATH)

# Get camera RTSP address from Kubernetes Edgedevice CR
def get_camera_address():
    try:
        config.load_incluster_config()
        api = client.CustomObjectsApi()
        cr = api.get_namespaced_custom_object(
            group=EDGEDEVICE_CRD_GROUP,
            version=EDGEDEVICE_CRD_VERSION,
            namespace=EDGEDEVICE_NAMESPACE,
            plural=EDGEDEVICE_CRD_PLURAL,
            name=EDGEDEVICE_NAME
        )
        address = cr.get("spec", {}).get("address")
        if not address:
            raise Exception("No camera address found in Edgedevice.spec.address")
        return address
    except Exception as e:
        logging.error(f"Failed to get camera address from Edgedevice CR: {e}")
        set_device_phase("Unknown")
        sys.exit(1)

# Status management
def set_device_phase(phase):
    if phase not in STATUS_PHASES:
        phase = "Unknown"
    try:
        config.load_incluster_config()
        api = client.CustomObjectsApi()
        body = {"status": {"edgedevicephase": phase}}
        api.patch_namespaced_custom_object_status(
            group=EDGEDEVICE_CRD_GROUP,
            version=EDGEDEVICE_CRD_VERSION,
            namespace=EDGEDEVICE_NAMESPACE,
            plural=EDGEDEVICE_CRD_PLURAL,
            name=EDGEDEVICE_NAME,
            body=body
        )
        logging.info(f"Set Edgedevice phase: {phase}")
    except ApiException as e:
        logging.error(f"Failed to update Edgedevice phase: {e}")

# MQTT Client
class MqttPublisher:
    def __init__(self, broker_addr):
        self.broker_host, self.broker_port = self._parse_broker(broker_addr)
        self.client = mqtt.Client()
        self.connected = False
        self.should_stop = False
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect

    def _parse_broker(self, addr):
        # Accept forms: mqtt://host:port or host:port
        addr = addr.replace("mqtt://", "")
        if ":" not in addr:
            logging.error("MQTT_BROKER_ADDRESS must be in host:port format.")
            sys.exit(1)
        host, port = addr.split(":", 1)
        return host, int(port)

    def start(self):
        try:
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
        except Exception as e:
            logging.error(f"Failed to connect to MQTT broker: {e}")
            self.connected = False

    def stop(self):
        self.should_stop = True
        self.client.loop_stop()
        self.client.disconnect()

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            logging.info("Connected to MQTT Broker.")
        else:
            self.connected = False
            logging.error(f"Failed to connect to MQTT Broker, rc={rc}")

    def on_disconnect(self, client, userdata, rc):
        self.connected = False
        logging.warning("Disconnected from MQTT Broker.")

    def publish(self, topic, payload, qos=1):
        if self.connected:
            result = self.client.publish(topic, payload, qos=qos)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logging.error(f"Failed to publish to MQTT: {mqtt.error_string(result.rc)}")
        else:
            logging.warning("MQTT client not connected, skipping publish.")

# Video Frame Fetcher
class VideoFrameStreamer:
    def __init__(self, rtsp_url, mqtt_publisher, topic, qos, interval, settings):
        self.rtsp_url = rtsp_url
        self.publisher = mqtt_publisher
        self.topic = topic
        self.qos = qos
        self.interval = interval
        self.settings = settings or {}
        self.should_stop = False

    def start(self):
        self.should_stop = False
        threading.Thread(target=self.stream_frames, daemon=True).start()

    def stop(self):
        self.should_stop = True

    def stream_frames(self):
        set_device_phase("Pending")
        cap = None
        try:
            cap = cv2.VideoCapture(self.rtsp_url)
            if not cap.isOpened():
                logging.error("Failed to open RTSP stream.")
                set_device_phase("Failed")
                return
            set_device_phase("Running")
            while not self.should_stop:
                ret, frame = cap.read()
                if not ret:
                    logging.warning("Failed to grab frame, retrying...")
                    set_device_phase("Failed")
                    time.sleep(2)
                    continue
                set_device_phase("Running")
                # Optionally resize if configured
                if "resize" in self.settings:
                    width = self.settings["resize"].get("width")
                    height = self.settings["resize"].get("height")
                    if width and height:
                        frame = cv2.resize(frame, (int(width), int(height)))
                # Encode frame as JPEG
                _, buf = cv2.imencode('.jpg', frame)
                b64_frame = base64.b64encode(buf.tobytes()).decode('utf-8')
                payload = {
                    "timestamp": datetime.utcnow().isoformat() + 'Z',
                    "frame_number": int(cap.get(cv2.CAP_PROP_POS_FRAMES)),
                    "frame": b64_frame
                }
                self.publisher.publish(self.topic, mqtt_payload_json(payload), qos=self.qos)
                time.sleep(self.interval)
        except Exception as e:
            logging.error(f"Exception in video streaming: {e}")
            set_device_phase("Failed")
        finally:
            if cap:
                cap.release()
            set_device_phase("Pending")

def mqtt_payload_json(obj):
    import json
    return json.dumps(obj)

# Parse settings for api
api_settings = {}
if instructions and "device/camera/frame" in instructions:
    api_settings = instructions["device/camera/frame"].get("protocolPropertyList", {})

# Optionally override FPS from settings
fps = int(api_settings.get("fps", 5)) if api_settings.get("fps") else 5
FRAME_PUBLISH_INTERVAL = 1.0 / fps if fps > 0 else 1.0 / 5

# Main logic
def main():
    # Signal handler
    stop_event = threading.Event()
    def sigterm_handler(signum, frame):
        stop_event.set()
    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigterm_handler)

    set_device_phase("Pending")
    rtsp_url = get_camera_address()
    mqtt_pub = MqttPublisher(MQTT_BROKER_ADDRESS)
    mqtt_pub.start()
    streamer = VideoFrameStreamer(
        rtsp_url=rtsp_url,
        mqtt_publisher=mqtt_pub,
        topic=FRAME_TOPIC,
        qos=FRAME_QOS,
        interval=FRAME_PUBLISH_INTERVAL,
        settings=api_settings
    )
    streamer.start()

    try:
        while not stop_event.is_set():
            time.sleep(1)
    finally:
        streamer.stop()
        mqtt_pub.stop()
        set_device_phase("Pending")

if __name__ == "__main__":
    main()
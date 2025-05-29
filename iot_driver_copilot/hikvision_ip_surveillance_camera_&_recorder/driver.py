import base64
import os
import signal
import sys
import threading
import time
import yaml
import json
import datetime

from kubernetes import client, config
from kubernetes.client.rest import ApiException

import cv2
import paho.mqtt.client as mqtt

# --- Constants ---
INSTRUCTION_PATH = '/etc/edgedevice/config/instructions'
EDGEDEVICE_NAME = os.environ['EDGEDEVICE_NAME']
EDGEDEVICE_NAMESPACE = os.environ['EDGEDEVICE_NAMESPACE']
MQTT_BROKER_ADDRESS = os.environ['MQTT_BROKER_ADDRESS']

# --- Kubernetes EdgeDevice CRD Setup ---
EDGEDEVICE_GROUP = 'shifu.edgenesis.io'
EDGEDEVICE_VERSION = 'v1alpha1'
EDGEDEVICE_PLURAL = 'edgedevices'

# --- MQTT Topic ---
VIDEO_FRAME_TOPIC = 'ipcam/video/frame'

# --- Load K8s In-Cluster ---
config.load_incluster_config()
k8s_api = client.CustomObjectsApi()

# --- Helper: Update EdgeDevice Phase ---
def update_edgedevice_phase(phase):
    for _ in range(3):
        try:
            device = k8s_api.get_namespaced_custom_object(
                EDGEDEVICE_GROUP, EDGEDEVICE_VERSION, EDGEDEVICE_NAMESPACE, EDGEDEVICE_PLURAL, EDGEDEVICE_NAME)
            if device.get('status', {}).get('edgeDevicePhase', None) == phase:
                return
            body = {
                "status": {
                    "edgeDevicePhase": phase
                }
            }
            k8s_api.patch_namespaced_custom_object_status(
                EDGEDEVICE_GROUP, EDGEDEVICE_VERSION, EDGEDEVICE_NAMESPACE, EDGEDEVICE_PLURAL, EDGEDEVICE_NAME, body)
            return
        except Exception:
            time.sleep(1)
    return

# --- Helper: Get RTSP Address from EdgeDevice CRD ---
def get_rtsp_address_from_edgedevice():
    try:
        device = k8s_api.get_namespaced_custom_object(
            EDGEDEVICE_GROUP, EDGEDEVICE_VERSION, EDGEDEVICE_NAMESPACE, EDGEDEVICE_PLURAL, EDGEDEVICE_NAME)
        address = device.get('spec', {}).get('address', '')
        return address
    except Exception:
        return ''

# --- Helper: Load API Instruction Settings ---
def load_api_settings():
    try:
        with open(INSTRUCTION_PATH, 'r') as f:
            data = yaml.safe_load(f)
            return data or {}
    except Exception:
        return {}

# --- MQTT Client Setup ---
def get_mqtt_host_port(addr):
    # e.g., "10.2.3.4:1883"
    if ':' in addr:
        host, port = addr.split(':')
        return host, int(port)
    return addr, 1883

# --- Video Frame Publisher ---
class VideoFramePublisher(threading.Thread):
    def __init__(self, mqtt_client, rtsp_url, topic, qos, frame_interval=1.0, jpeg_quality=80):
        super().__init__()
        self.mqtt_client = mqtt_client
        self.rtsp_url = rtsp_url
        self.topic = topic
        self.qos = qos
        self.frame_interval = frame_interval
        self.jpeg_quality = jpeg_quality
        self._stop_event = threading.Event()
        self._cap = None

    def run(self):
        update_edgedevice_phase('Running')
        try:
            self._cap = cv2.VideoCapture(self.rtsp_url)
            if not self._cap.isOpened():
                update_edgedevice_phase('Failed')
                return
            while not self._stop_event.is_set():
                ret, frame = self._cap.read()
                if not ret or frame is None:
                    update_edgedevice_phase('Failed')
                    break
                _, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
                b64_data = base64.b64encode(buf).decode('utf-8')
                msg = {
                    'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
                    'frame_data': b64_data
                }
                self.mqtt_client.publish(self.topic, json.dumps(msg), qos=self.qos)
                # Wait for the next frame interval
                time.sleep(self.frame_interval)
        except Exception:
            update_edgedevice_phase('Failed')
        finally:
            if self._cap:
                self._cap.release()
            update_edgedevice_phase('Pending')

    def stop(self):
        self._stop_event.set()

# --- MQTT Logic ---
class MQTTDeviceShifu:
    def __init__(self):
        self.mqtt_host, self.mqtt_port = get_mqtt_host_port(MQTT_BROKER_ADDRESS)
        self.api_settings = load_api_settings()
        self.rtsp_url = get_rtsp_address_from_edgedevice()
        self.publisher = None
        self.mqtt_client = mqtt.Client()
        self._setup_mqtt_callbacks()
        # Get settings for the topic
        api_setting = self.api_settings.get('ipcam/video/frame', {})
        protocol_props = api_setting.get('protocolPropertyList', {})
        self.frame_interval = float(protocol_props.get('frame_interval', 1.0))
        self.jpeg_quality = int(protocol_props.get('jpeg_quality', 80))
        self.qos = int(protocol_props.get('qos', 1))

    def _setup_mqtt_callbacks(self):
        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                # Device is running
                update_edgedevice_phase('Running')
            else:
                update_edgedevice_phase('Failed')

        def on_disconnect(client, userdata, rc):
            update_edgedevice_phase('Pending')

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_disconnect = on_disconnect

    def start(self):
        update_edgedevice_phase('Pending')
        try:
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)
        except Exception:
            update_edgedevice_phase('Failed')
            return
        # Start MQTT loop in background
        self.mqtt_client.loop_start()
        # Start pushing frames
        self.publisher = VideoFramePublisher(
            self.mqtt_client,
            self.rtsp_url,
            VIDEO_FRAME_TOPIC,
            self.qos,
            self.frame_interval,
            self.jpeg_quality
        )
        self.publisher.start()

    def stop(self):
        if self.publisher:
            self.publisher.stop()
            self.publisher.join()
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

# --- Main Entrypoint ---
shifu = MQTTDeviceShifu()
running = True

def signal_handler(sig, frame):
    global running
    running = False
    shifu.stop()
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

def main():
    shifu.start()
    # Keep process alive
    while running:
        time.sleep(2)

if __name__ == '__main__':
    main()
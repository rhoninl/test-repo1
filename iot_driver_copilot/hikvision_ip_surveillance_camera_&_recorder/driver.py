import os
import sys
import time
import json
import base64
import logging
import threading
import signal
from typing import Dict, Any, Optional

import yaml
from flask import Flask, jsonify
import paho.mqtt.client as mqtt

from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.rest import ApiException

import cv2  # OpenCV for RTSP frame grabbing

# ========== Logging Configuration ==========
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ========== Environment Variables ==========
EDGEDEVICE_NAME = os.getenv("EDGEDEVICE_NAME", "deviceshifu-ipcamera")
EDGEDEVICE_NAMESPACE = os.getenv("EDGEDEVICE_NAMESPACE", "devices")
CONFIG_MOUNT_PATH = os.getenv("CONFIG_MOUNT_PATH", "/etc/edgedevice/config")

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_BROKER_USERNAME = os.getenv("MQTT_BROKER_USERNAME", "")
MQTT_BROKER_PASSWORD = os.getenv("MQTT_BROKER_PASSWORD", "")
MQTT_TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "shifu")

HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))

# For RTSP connection (must be provided via env or driverProperties)
RTSP_URL = os.getenv("RTSP_URL", "")  # e.g., "rtsp://<user>:<pass>@<ip>:554/Streaming/Channels/101"

# ========== ShifuClient Definition ==========
class ShifuClient:
    def __init__(self):
        self.device_name = EDGEDEVICE_NAME
        self.namespace = EDGEDEVICE_NAMESPACE
        self.config_mount_path = CONFIG_MOUNT_PATH
        self._init_k8s_client()

    def _init_k8s_client(self):
        try:
            k8s_config.load_incluster_config()
            logger.info("Using in-cluster Kubernetes config.")
        except Exception:
            try:
                k8s_config.load_kube_config()
                logger.info("Using local kubeconfig.")
            except Exception as e:
                logger.error(f"Failed to load Kubernetes config: {e}")
                self.k8s_api = None
                return
        self.k8s_api = k8s_client.CustomObjectsApi()

    def get_edge_device(self) -> Optional[Dict[str, Any]]:
        if not self.k8s_api:
            logger.warning("Kubernetes API not initialized.")
            return None
        try:
            return self.k8s_api.get_namespaced_custom_object(
                group="deviceshifu.edgedevice.io",
                version="v1alpha2",
                namespace=self.namespace,
                plural="edgedevices",
                name=self.device_name
            )
        except ApiException as e:
            logger.error(f"Failed to get EdgeDevice CR: {e}")
            return None

    def get_device_address(self) -> Optional[str]:
        ed = self.get_edge_device()
        if not ed:
            return None
        try:
            addr = ed["spec"]["address"]
            logger.info(f"Device address from EdgeDevice CR: {addr}")
            return addr
        except KeyError:
            logger.warning("No address found in EdgeDevice CR.")
            return None

    def update_device_status(self, status: str) -> None:
        if not self.k8s_api:
            return
        patch = {"status": {"connection": status}}
        try:
            self.k8s_api.patch_namespaced_custom_object_status(
                group="deviceshifu.edgedevice.io",
                version="v1alpha2",
                namespace=self.namespace,
                plural="edgedevices",
                name=self.device_name,
                body=patch
            )
            logger.info(f"Updated device status to '{status}'.")
        except ApiException as e:
            logger.error(f"Failed to update device status: {e}")

    def read_mounted_config_file(self, filename: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(self.config_mount_path, filename)
        try:
            with open(path, "r") as f:
                content = yaml.safe_load(f)
                logger.info(f"Loaded config file: {filename}")
                return content
        except Exception as e:
            logger.error(f"Failed to read config file {filename}: {e}")
            return None

    def get_instruction_config(self) -> Dict[str, Any]:
        cfg = self.read_mounted_config_file("instructions")
        return cfg if cfg else {}

# ========== IPCameraMQTTDriver Definition ==========
class IPCameraMQTTDriver:
    def __init__(self):
        self.shifu_client = ShifuClient()
        self.latest_data: Dict[str, Any] = {}
        self.shutdown_flag = threading.Event()
        self.flask_app = Flask(__name__)

        # MQTT
        self.mqtt_client = None
        self.mqtt_connected = threading.Event()
        self.mqtt_client_id = f"{EDGEDEVICE_NAME}-{int(time.time())}"
        self.mqtt_topics: Dict[str, Dict[str, Any]] = {}
        self.mqtt_qos_map: Dict[str, int] = {}

        # RTSP
        self.rtsp_url = self._get_rtsp_url()
        self.rtsp_capture = None
        self.rtsp_connected = False

        # Instructions
        self.instructions = self.shifu_client.get_instruction_config()

        # Thread management
        self.threads = []

        # Camera frame publishing
        self.publish_interval_ms = 1000  # Default to 1 FPS if not specified
        self.frame_topic = ""
        self._parse_publish_intervals()

        # Subscribe topics
        self.subscriber_topics = []

        self.setup_routes()

    def _get_rtsp_url(self) -> str:
        # Priority: RTSP_URL env > driverProperties > EdgeDevice address
        if RTSP_URL:
            logger.info("RTSP URL from env.")
            return RTSP_URL
        driver_props = self.shifu_client.read_mounted_config_file("driverProperties")
        if driver_props:
            url = driver_props.get("RTSP_URL")
            if url:
                logger.info("RTSP URL from driverProperties.")
                return url
        addr = self.shifu_client.get_device_address()
        if addr and addr.startswith("rtsp://"):
            logger.info("RTSP URL from EdgeDevice CR.")
            return addr
        logger.error("RTSP URL not configured.")
        return ""

    def _parse_publish_intervals(self):
        # Only 'ipcam/video/frame' in sample, but generalize for others
        if not self.instructions:
            logger.warning("No instructions found in config map.")
            return
        for instr_name, instr_val in self.instructions.items():
            if "publishIntervalMS" in instr_val:
                interval = int(instr_val.get("publishIntervalMS", 1000))  # ms
            else:
                interval = 1000
            self.mqtt_topics[instr_name] = {
                "interval": interval,
                "mode": instr_val.get("mode", "publisher")
            }
            qos = int(instr_val.get("qos", 0))
            self.mqtt_qos_map[instr_name] = qos
            if instr_val.get("mode", "publisher") == "publisher":
                self.frame_topic = instr_name
                self.publish_interval_ms = interval
            elif instr_val.get("mode", "subscriber") == "subscriber":
                self.subscriber_topics.append(instr_name)
        logger.info(f"Publish intervals parsed: {self.mqtt_topics}")

    def _start_scheduled_publishers(self):
        for topic in self.mqtt_topics:
            if self.mqtt_topics[topic]["mode"] == "publisher":
                t = threading.Thread(
                    target=self._publish_topic_periodically,
                    args=(topic, self.mqtt_topics[topic]["interval"]),
                    name=f"Publisher-{topic}",
                    daemon=True
                )
                self.threads.append(t)
                t.start()

    def _publish_topic_periodically(self, topic: str, interval_ms: int):
        logger.info(f"Started periodic publisher for topic '{topic}' every {interval_ms} ms.")
        while not self.shutdown_flag.is_set():
            frame_data = self.get_camera_frame()
            if frame_data is not None:
                payload = {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
                    "frame_data": frame_data
                }
                mqtt_topic = f"{MQTT_TOPIC_PREFIX}/{EDGEDEVICE_NAME}/{topic}"
                self.latest_data[topic] = payload
                self.publish_mqtt(mqtt_topic, payload, qos=self.mqtt_qos_map.get(topic, 1))
            time.sleep(interval_ms / 1000.0)

    def get_camera_frame(self) -> Optional[str]:
        """Grab a frame from RTSP, encode as JPEG base64."""
        if not self.rtsp_url:
            logger.error("No RTSP URL provided for camera frame grabbing.")
            return None
        try:
            if self.rtsp_capture is None or not self.rtsp_connected:
                logger.info(f"Connecting to RTSP stream: {self.rtsp_url}")
                self.rtsp_capture = cv2.VideoCapture(self.rtsp_url)
                # Wait for connection
                time.sleep(1)
                if not self.rtsp_capture.isOpened():
                    logger.error("Failed to open RTSP stream.")
                    self.rtsp_connected = False
                    self.shifu_client.update_device_status("disconnected")
                    return None
                self.rtsp_connected = True
                self.shifu_client.update_device_status("connected")
            ret, frame = self.rtsp_capture.read()
            if not ret or frame is None:
                logger.error("Failed to grab frame from RTSP stream.")
                self.rtsp_connected = False
                self.shifu_client.update_device_status("disconnected")
                # Try to reconnect next time
                self.rtsp_capture.release()
                self.rtsp_capture = None
                return None
            # Encode frame as JPEG
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                logger.error("Failed to encode frame as JPEG.")
                return None
            # Encode to base64
            b64_frame = base64.b64encode(jpeg.tobytes()).decode('utf-8')
            return b64_frame
        except Exception as e:
            logger.error(f"Error getting camera frame: {e}")
            self.rtsp_connected = False
            self.shifu_client.update_device_status("disconnected")
            if self.rtsp_capture:
                self.rtsp_capture.release()
                self.rtsp_capture = None
            return None

    def connect_mqtt(self):
        logger.info("Connecting to MQTT broker...")
        self.mqtt_client = mqtt.Client(client_id=self.mqtt_client_id, clean_session=True)
        if MQTT_BROKER_USERNAME and MQTT_BROKER_PASSWORD:
            self.mqtt_client.username_pw_set(MQTT_BROKER_USERNAME, MQTT_BROKER_PASSWORD)
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_subscribe = self.on_mqtt_subscribe
        self.mqtt_client.on_publish = self.on_mqtt_publish

        # Connection loop with retry
        while not self.shutdown_flag.is_set():
            try:
                self.mqtt_client.connect(MQTT_BROKER, MQTT_BROKER_PORT, keepalive=60)
                break
            except Exception as e:
                logger.error(f"MQTT connection failed: {e}, retrying in 5s...")
                time.sleep(5)
        t = threading.Thread(target=self.mqtt_client.loop_forever, name="MQTTLoop", daemon=True)
        self.threads.append(t)
        t.start()

    def on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to MQTT broker.")
            self.mqtt_connected.set()
            # Subscribe to all control and subscriber topics
            for topic in self.subscriber_topics:
                mqtt_topic = f"{MQTT_TOPIC_PREFIX}/{EDGEDEVICE_NAME}/{topic}"
                qos = self.mqtt_qos_map.get(topic, 1)
                client.subscribe(mqtt_topic, qos)
                logger.info(f"Subscribed to topic: {mqtt_topic} (QoS={qos})")
            # control/#
            control_topic = f"{MQTT_TOPIC_PREFIX}/{EDGEDEVICE_NAME}/control/#"
            client.subscribe(control_topic, qos=1)
            logger.info(f"Subscribed to control topic: {control_topic}")
            # Publish status
            self.publish_status("connected")
        else:
            logger.error(f"Failed to connect to MQTT broker: {rc}")

    def on_mqtt_disconnect(self, client, userdata, rc):
        logger.warning("Disconnected from MQTT broker.")
        self.mqtt_connected.clear()
        self.publish_status("disconnected")

    def on_mqtt_subscribe(self, client, userdata, mid, granted_qos):
        logger.info(f"Subscribed, mid={mid}, qos={granted_qos}")

    def on_mqtt_publish(self, client, userdata, mid):
        logger.debug(f"Published message, mid={mid}")

    def on_mqtt_message(self, client, userdata, msg):
        logger.info(f"Received MQTT message on topic: {msg.topic}")
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            payload = msg.payload.decode("utf-8")
        self.handle_mqtt_message(msg.topic, payload)

    def handle_mqtt_message(self, topic: str, payload: Any):
        # Handle control commands or subscriber topics
        if topic.startswith(f"{MQTT_TOPIC_PREFIX}/{EDGEDEVICE_NAME}/control/"):
            cmd = topic.split("/")[-1]
            logger.info(f"Received control command: {cmd}, payload: {payload}")
            # Implement command handling here (start/stop recording, PTZ, etc.)
            # For now, just log
        else:
            logger.info(f"Received message on topic {topic}: {payload}")

    def publish_mqtt(self, topic: str, payload: Any, qos: int = 1):
        if not self.mqtt_connected.is_set():
            logger.warning("MQTT not connected, dropping message.")
            return
        try:
            self.mqtt_client.publish(topic, json.dumps(payload, default=str), qos=qos)
            logger.debug(f"Published to MQTT topic {topic}.")
        except Exception as e:
            logger.error(f"Failed to publish MQTT message: {e}")

    def publish_status(self, status: str):
        topic = f"{MQTT_TOPIC_PREFIX}/{EDGEDEVICE_NAME}/status"
        payload = {
            "status": status,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime())
        }
        try:
            self.mqtt_client.publish(topic, json.dumps(payload, default=str), qos=1)
        except Exception as e:
            logger.error(f"Failed to publish status: {e}")

    # ========== HTTP Server ==========
    def setup_routes(self):
        @self.flask_app.route('/health', methods=['GET'])
        def health():
            return jsonify({"status": "ok"}), 200

        @self.flask_app.route('/status', methods=['GET'])
        def status():
            return jsonify({
                "mqtt_connected": self.mqtt_connected.is_set(),
                "rtsp_connected": self.rtsp_connected,
                "device_status": self.latest_data.get("status", {}),
                "topics": list(self.latest_data.keys())
            }), 200

    def start_http_server(self):
        t = threading.Thread(
            target=self.flask_app.run,
            kwargs={"host": HTTP_HOST, "port": HTTP_PORT, "threaded": True, "use_reloader": False},
            name="HTTPServer",
            daemon=True
        )
        self.threads.append(t)
        t.start()
        logger.info(f"HTTP server started at http://{HTTP_HOST}:{HTTP_PORT}")

    # ========== Signal Handling & Shutdown ==========
    def signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        self.shutdown()

    def shutdown(self):
        logger.info("Shutting down driver...")
        self.shutdown_flag.set()
        # Release RTSP
        if self.rtsp_capture:
            try:
                self.rtsp_capture.release()
            except Exception:
                pass
        # Disconnect MQTT
        if self.mqtt_client:
            try:
                self.mqtt_client.disconnect()
                self.mqtt_client.loop_stop()
            except Exception:
                pass
        # Wait for threads
        for t in self.threads:
            if t.is_alive():
                t.join(timeout=2)
        logger.info("Driver shutdown complete.")
        sys.exit(0)

    # ========== Run ==========
    def run(self):
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

        self.start_http_server()
        self.connect_mqtt()
        self._start_scheduled_publishers()

        try:
            while not self.shutdown_flag.is_set():
                time.sleep(1)
        except Exception as e:
            logger.error(f"Main loop exception: {e}")
            self.shutdown()

# ========== Main ==========
if __name__ == "__main__":
    driver = IPCameraMQTTDriver()
    driver.run()
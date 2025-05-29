import os
import sys
import time
import json
import yaml
import base64
import signal
import logging
import threading
from typing import Dict, Any, Optional

from flask import Flask, jsonify
import paho.mqtt.client as mqtt

from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.rest import ApiException

import cv2  # Required for video frame capture

# Logging configuration
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO)
)
logger = logging.getLogger("deviceshifu-ipcam-mqtt")

# Environment variable defaults
ENV = {
    "EDGEDEVICE_NAME": os.environ.get("EDGEDEVICE_NAME", "deviceshifu-ipcamera"),
    "EDGEDEVICE_NAMESPACE": os.environ.get("EDGEDEVICE_NAMESPACE", "devices"),
    "CONFIG_MOUNT_PATH": os.environ.get("CONFIG_MOUNT_PATH", "/etc/edgedevice/config"),
    "MQTT_BROKER": os.environ.get("MQTT_BROKER", "localhost"),
    "MQTT_BROKER_PORT": int(os.environ.get("MQTT_BROKER_PORT", 1883)),
    "MQTT_BROKER_USERNAME": os.environ.get("MQTT_BROKER_USERNAME"),
    "MQTT_BROKER_PASSWORD": os.environ.get("MQTT_BROKER_PASSWORD"),
    "MQTT_TOPIC_PREFIX": os.environ.get("MQTT_TOPIC_PREFIX", "shifu"),
    "HTTP_HOST": os.environ.get("HTTP_HOST", "0.0.0.0"),
    "HTTP_PORT": int(os.environ.get("HTTP_PORT", 8080)),
}

# ConfigMap file paths
INSTRUCTIONS_FILE = os.path.join(ENV["CONFIG_MOUNT_PATH"], "instructions")
DRIVER_PROPERTIES_FILE = os.path.join(ENV["CONFIG_MOUNT_PATH"], "driverProperties")

# Global shutdown flag for threads
shutdown_flag = threading.Event()


class ShifuClient:
    def __init__(self):
        self.edgedevice_name = ENV["EDGEDEVICE_NAME"]
        self.namespace = ENV["EDGEDEVICE_NAMESPACE"]
        self.config_mount_path = ENV["CONFIG_MOUNT_PATH"]
        self.k8s_api = None
        self._init_k8s_client()

    def _init_k8s_client(self):
        try:
            try:
                k8s_config.load_incluster_config()
                logger.info("Loaded in-cluster Kubernetes config.")
            except Exception:
                k8s_config.load_kube_config()
                logger.info("Loaded local kubeconfig.")
            self.k8s_api = k8s_client.CustomObjectsApi()
        except Exception as e:
            logger.error(f"Failed to initialize Kubernetes client: {e}")
            self.k8s_api = None

    def get_edge_device(self) -> Optional[Dict[str, Any]]:
        if self.k8s_api is None:
            logger.error("Kubernetes client not initialized.")
            return None
        group = "edgedevice.shifu.edgenesis.io"
        version = "v1alpha1"
        plural = "edgedevices"
        try:
            return self.k8s_api.get_namespaced_custom_object(
                group=group,
                version=version,
                namespace=self.namespace,
                plural=plural,
                name=self.edgedevice_name
            )
        except ApiException as e:
            logger.error(f"Error getting EdgeDevice: {e}")
            return None

    def get_device_address(self) -> Optional[str]:
        edge_device = self.get_edge_device()
        if not edge_device:
            return None
        try:
            return edge_device.get("spec", {}).get("address")
        except Exception as e:
            logger.error(f"Failed to extract device address: {e}")
            return None

    def update_device_status(self, status_msg: str) -> bool:
        if self.k8s_api is None:
            logger.warning("Skipping device status update: K8s client not available.")
            return False
        group = "edgedevice.shifu.edgenesis.io"
        version = "v1alpha1"
        plural = "edgedevices"
        try:
            body = {"status": {"devicePhase": status_msg}}
            self.k8s_api.patch_namespaced_custom_object_status(
                group=group,
                version=version,
                namespace=self.namespace,
                plural=plural,
                name=self.edgedevice_name,
                body=body
            )
            logger.info(f"Device status updated: {status_msg}")
            return True
        except ApiException as e:
            logger.error(f"Failed to update device status: {e}")
            return False

    def read_mounted_config_file(self, file_path: str) -> Dict[str, Any]:
        try:
            with open(file_path, "r") as f:
                content = yaml.safe_load(f)
                if content is None:
                    content = {}
                return content
        except Exception as e:
            logger.error(f"Failed to read config file {file_path}: {e}")
            return {}

    def get_instruction_config(self) -> Dict[str, Any]:
        return self.read_mounted_config_file(INSTRUCTIONS_FILE)


class IPCameraMQTTDriver:
    def __init__(self):
        self.shifu_client = ShifuClient()
        self.device_address = self._resolve_rtsp_address()
        self.driver_properties = self.shifu_client.read_mounted_config_file(DRIVER_PROPERTIES_FILE)
        self.instruction_config = self.shifu_client.get_instruction_config()
        self.latest_data: Dict[str, Any] = {}
        self.mqtt_client = None
        self.mqtt_connected = threading.Event()
        self.client_id = f"{ENV['EDGEDEVICE_NAME']}-{int(time.time())}"
        self.mqtt_topic_prefix = ENV["MQTT_TOPIC_PREFIX"]
        self.running_streams = {}
        self.http_app = Flask(__name__)
        self._setup_routes()
        self.http_thread = threading.Thread(target=self._run_http_server, name="HTTPServer", daemon=True)
        self.status_lock = threading.Lock()
        self.scheduled_publish_threads = []
        self._parse_publish_intervals()

    def _resolve_rtsp_address(self) -> str:
        # Priority: ConfigMap 'driverProperties' > EdgeDevice K8s CR > ENV
        rtsp_address = (
            self.driver_properties.get("rtsp_url")
            or self.shifu_client.get_device_address()
            or os.environ.get("RTSP_URL")
        )
        if not rtsp_address:
            logger.error("RTSP address for camera is not configured.")
        else:
            logger.info(f"RTSP address resolved: {rtsp_address}")
        return rtsp_address

    def _parse_publish_intervals(self):
        # Parse instruction config for topics and intervals
        self.publish_intervals = {}
        if not self.instruction_config:
            logger.warning("Instruction config not found or empty.")
            return
        for instr_name, instr_cfg in self.instruction_config.items():
            mode = instr_cfg.get("protocolProperties", {}).get("mode", "publisher").lower()
            interval = instr_cfg.get("protocolProperties", {}).get("publishIntervalMS", 0)
            if mode == "publisher" and interval > 0:
                self.publish_intervals[instr_name] = interval / 1000.0  # convert ms to seconds

    def _start_scheduled_publishers(self):
        # Start threads for periodic publishing
        for instr_name, interval in self.publish_intervals.items():
            t = threading.Thread(
                target=self._publish_topic_periodically,
                args=(instr_name, interval),
                name=f"Publisher-{instr_name}",
                daemon=True
            )
            t.start()
            self.scheduled_publish_threads.append(t)
            logger.info(f"Started scheduled publisher thread for '{instr_name}' every {interval}s.")

    def _publish_topic_periodically(self, instr_name: str, interval: float):
        topic = f"{self.mqtt_topic_prefix}/{ENV['EDGEDEVICE_NAME']}/{instr_name}"
        logger.info(f"Periodic publishing to topic {topic} every {interval}s.")
        while not shutdown_flag.is_set():
            try:
                if instr_name == "video_frame":
                    frame_json = self._capture_and_package_frame()
                    if frame_json:
                        self.latest_data[instr_name] = frame_json
                        self.publish_mqtt(topic, frame_json, qos=1)
                else:
                    data = self.latest_data.get(instr_name, {})
                    if data:
                        self.publish_mqtt(topic, data, qos=1)
            except Exception as e:
                logger.error(f"Error in periodic publishing for {instr_name}: {e}")
            time.sleep(interval)

    def _capture_and_package_frame(self) -> Optional[Dict[str, Any]]:
        # OpenCV capture from RTSP stream and package frame as JSON
        try:
            cap = cv2.VideoCapture(self.device_address)
            if not cap.isOpened():
                logger.warning("Failed to open RTSP stream for video frame capture.")
                return None
            ret, frame = cap.read()
            cap.release()
            if not ret or frame is None:
                logger.warning("No frame captured from RTSP.")
                return None
            # JPEG encode
            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                logger.warning("Failed to encode frame to JPEG.")
                return None
            frame_b64 = base64.b64encode(buffer).decode('utf-8')
            frame_json = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "frame_data": frame_b64
            }
            return frame_json
        except Exception as e:
            logger.error(f"Error capturing video frame: {e}")
            return None

    def connect_mqtt(self):
        self.mqtt_client = mqtt.Client(self.client_id, clean_session=True)
        if ENV["MQTT_BROKER_USERNAME"]:
            self.mqtt_client.username_pw_set(ENV["MQTT_BROKER_USERNAME"], ENV["MQTT_BROKER_PASSWORD"])
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect

        while not shutdown_flag.is_set():
            try:
                logger.info(f"Connecting to MQTT broker {ENV['MQTT_BROKER']}:{ENV['MQTT_BROKER_PORT']} ...")
                self.mqtt_client.connect(ENV["MQTT_BROKER"], ENV["MQTT_BROKER_PORT"], keepalive=60)
                self.mqtt_client.loop_start()
                break
            except Exception as e:
                logger.error(f"MQTT connection failed: {e}. Retrying in 5s.")
                time.sleep(5)

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to MQTT broker.")
            self.mqtt_connected.set()
            # Subscribe to control topics (e.g., ipcam/cmd/stream)
            control_topic = f"{self.mqtt_topic_prefix}/{ENV['EDGEDEVICE_NAME']}/cmd/stream"
            self.mqtt_client.subscribe(control_topic, qos=2)
            logger.info(f"Subscribed to control topic: {control_topic}")
        else:
            logger.error(f"MQTT connection failed with code {rc}.")

    def _on_mqtt_disconnect(self, client, userdata, rc):
        self.mqtt_connected.clear()
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect. Will attempt to reconnect.")
            self.connect_mqtt()

    def _on_mqtt_message(self, client, userdata, msg):
        logger.info(f"MQTT message received: topic={msg.topic}")
        try:
            payload = json.loads(msg.payload.decode())
            if msg.topic.endswith("cmd/stream"):
                self._handle_stream_command(payload)
        except Exception as e:
            logger.error(f"Failed to process MQTT message: {e}")

    def _handle_stream_command(self, payload: Dict[str, Any]):
        # Payload: {"command": "start"/"stop"}
        command = payload.get("command", "").lower()
        logger.info(f"Stream command received: {command}")
        if command == "start":
            if "video_frame" not in self.running_streams:
                t = threading.Thread(target=self._realtime_frame_publisher, name="RTSP-Frame-Publisher", daemon=True)
                self.running_streams["video_frame"] = t
                t.start()
                logger.info("Started real-time video frame publisher.")
        elif command == "stop":
            if "video_frame" in self.running_streams:
                self.running_streams.pop("video_frame", None)
                logger.info("Stopped real-time video frame publisher.")
        else:
            logger.warning(f"Unknown stream command: {command}")

    def _realtime_frame_publisher(self):
        topic = f"{self.mqtt_topic_prefix}/{ENV['EDGEDEVICE_NAME']}/video/frame"
        cap = cv2.VideoCapture(self.device_address)
        if not cap.isOpened():
            logger.error("Failed to open RTSP stream for real-time publisher.")
            return
        try:
            while not shutdown_flag.is_set() and "video_frame" in self.running_streams:
                ret, frame = cap.read()
                if not ret or frame is None:
                    logger.warning("No frame from RTSP in real-time publisher.")
                    time.sleep(0.2)
                    continue
                ret, buffer = cv2.imencode('.jpg', frame)
                if not ret:
                    logger.warning("Failed to encode real-time frame.")
                    continue
                frame_b64 = base64.b64encode(buffer).decode('utf-8')
                frame_json = {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "frame_data": frame_b64
                }
                self.latest_data["video_frame"] = frame_json
                self.publish_mqtt(topic, frame_json, qos=1)
                time.sleep(0.1)  # ~10 FPS
        except Exception as e:
            logger.error(f"Real-time frame publisher error: {e}")
        finally:
            cap.release()
            logger.info("RTSP stream closed for real-time publisher.")

    def publish_mqtt(self, topic: str, data: Any, qos: int = 1, retain: bool = False):
        try:
            payload = json.dumps(data, default=str)
            result = self.mqtt_client.publish(topic, payload, qos=qos, retain=retain)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.debug(f"Published to {topic}")
            else:
                logger.error(f"Failed to publish to {topic}: {mqtt.error_string(result.rc)}")
        except Exception as e:
            logger.error(f"MQTT publish exception: {e}")

    def _setup_routes(self):
        @self.http_app.route("/health", methods=["GET"])
        def health():
            return jsonify({"status": "ok"}), 200

        @self.http_app.route("/status", methods=["GET"])
        def status():
            with self.status_lock:
                status = {
                    "mqtt_connected": self.mqtt_connected.is_set(),
                    "latest_data_keys": list(self.latest_data.keys()),
                    "device_address": self.device_address or "",
                    "running_streams": list(self.running_streams.keys())
                }
            return jsonify(status), 200

    def _run_http_server(self):
        try:
            logger.info(f"Starting HTTP server on {ENV['HTTP_HOST']}:{ENV['HTTP_PORT']}")
            self.http_app.run(host=ENV["HTTP_HOST"], port=ENV["HTTP_PORT"], threaded=True)
        except Exception as e:
            logger.error(f"HTTP server error: {e}")

    def signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down.")
        shutdown_flag.set()
        self.shutdown()

    def shutdown(self):
        logger.info("Shutting down driver...")
        try:
            if self.mqtt_client:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
                logger.info("MQTT client disconnected.")
        except Exception as e:
            logger.error(f"Error during MQTT disconnect: {e}")
        try:
            self.shifu_client.update_device_status("Offline")
        except Exception as e:
            logger.error(f"Error updating EdgeDevice status on shutdown: {e}")

    def run(self):
        # Register signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

        # Set device status Online
        self.shifu_client.update_device_status("Online")

        # Start HTTP server thread
        self.http_thread.start()

        # Connect to MQTT broker
        self.connect_mqtt()

        # Start any scheduled publishers (periodic publishing)
        self._start_scheduled_publishers()

        try:
            # Main thread loop: monitor threads and wait for shutdown
            while not shutdown_flag.is_set():
                time.sleep(1)
        except Exception as e:
            logger.error(f"Driver main loop exception: {e}")
        finally:
            self.shutdown()
            logger.info("Driver stopped.")


if __name__ == "__main__":
    driver = IPCameraMQTTDriver()
    driver.run()
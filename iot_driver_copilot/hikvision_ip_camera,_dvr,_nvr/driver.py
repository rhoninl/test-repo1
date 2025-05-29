import os
import time
import threading
import base64
import json
import yaml
import logging
import signal

from kubernetes import client, config
from kubernetes.client.rest import ApiException

import paho.mqtt.client as mqtt

CONFIGMAP_PATH = "/etc/edgedevice/config/instructions"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---- Kubernetes CRD Updater ----
class EdgeDeviceCRDManager:
    def __init__(self, name, namespace):
        self.name = name
        self.namespace = namespace
        try:
            config.load_incluster_config()
            self.api = client.CustomObjectsApi()
            logging.info("Loaded in-cluster k8s config")
        except Exception as e:
            logging.error(f"Failed to load k8s config: {e}")
            self.api = None

    def set_phase(self, phase):
        if not self.api:
            return
        body = {'status': {'edgedevicephase': phase}}
        for _ in range(3):
            try:
                self.api.patch_namespaced_custom_object_status(
                    group="shifu.edgenesis.io",
                    version="v1alpha1",
                    namespace=self.namespace,
                    plural="edgedevices",
                    name=self.name,
                    body=body
                )
                logging.info(f"Set edgedevicephase to {phase}")
                return
            except ApiException as e:
                logging.warning(f"Failed to update CRD status: {e}")
                time.sleep(1)

# ---- Config Reader ----
def read_api_settings(api_name):
    try:
        with open(CONFIGMAP_PATH, "r") as f:
            configmap_data = yaml.safe_load(f)
        return configmap_data.get(api_name, {}).get("protocolPropertyList", {})
    except Exception as e:
        logging.warning(f"Failed to read configmap: {e}")
        return {}

# ---- MQTT Client Handler ----
class MQTTFrameSubscriber:
    def __init__(self, broker_address, topic, qos, crd_manager):
        self.broker_address = broker_address
        self.topic = topic
        self.qos = qos
        self.crd_manager = crd_manager
        self.client = mqtt.Client()
        self.connected = False
        self.should_stop = threading.Event()

        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    def connect(self):
        try:
            host, port = self.broker_address.split(':')
            port = int(port)
            self.client.connect(host, port, keepalive=60)
            self.client.loop_start()
        except Exception as e:
            logging.error(f"MQTT connection failed: {e}")
            self.crd_manager.set_phase("Failed")
            self.connected = False

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            self.crd_manager.set_phase("Running")
            logging.info(f"Connected to MQTT broker at {self.broker_address}")
            client.subscribe(self.topic, qos=self.qos)
            logging.info(f"Subscribed to topic: {self.topic} (qos={self.qos})")
        else:
            logging.error(f"MQTT connection failed with code {rc}")
            self.crd_manager.set_phase("Failed")
            self.connected = False

    def on_disconnect(self, client, userdata, rc):
        self.connected = False
        if not self.should_stop.is_set():
            logging.warning("MQTT disconnected unexpectedly, setting device phase to Pending")
            self.crd_manager.set_phase("Pending")

    def on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8")
            data = json.loads(payload)
            ts = data.get("timestamp")
            frame_b64 = data.get("frame")
            frame_number = data.get("frame_number")
            # Here we just log reception; in a real deployment, we'd process/store frame.
            if frame_b64:
                logging.info(f"Received frame {frame_number} at {ts} (size={len(frame_b64)} base64 chars)")
            else:
                logging.info(f"Received message with no frame in topic {msg.topic}")
        except Exception as e:
            logging.error(f"Failed to process received MQTT message: {e}")

    def stop(self):
        self.should_stop.set()
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

# ---- Main Entrypoint ----
class DeviceShifuDriver:
    def __init__(self):
        self.device_name = os.environ.get("EDGEDEVICE_NAME")
        self.device_namespace = os.environ.get("EDGEDEVICE_NAMESPACE")
        self.mqtt_broker_address = os.environ.get("MQTT_BROKER_ADDRESS")

        if not self.device_name or not self.device_namespace or not self.mqtt_broker_address:
            logging.error("Missing required environment variables: EDGEDEVICE_NAME, EDGEDEVICE_NAMESPACE, MQTT_BROKER_ADDRESS")
            exit(1)

        self.crd_manager = EdgeDeviceCRDManager(self.device_name, self.device_namespace)
        self.crd_manager.set_phase("Pending")

        # Read configmap for topic/qos
        api_settings = read_api_settings("device/camera/frame")
        self.topic = "device/camera/frame"
        self.qos = 1
        if "qos" in api_settings:
            try:
                self.qos = int(api_settings["qos"])
            except Exception:
                pass
        if "topic" in api_settings:
            self.topic = api_settings["topic"]

        self.subscriber = MQTTFrameSubscriber(
            broker_address=self.mqtt_broker_address,
            topic=self.topic,
            qos=self.qos,
            crd_manager=self.crd_manager
        )

    def run(self):
        def handle_signal(signum, frame):
            logging.info("Signal received, shutting down gracefully...")
            self.subscriber.stop()
            self.crd_manager.set_phase("Pending")
            exit(0)
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        self.crd_manager.set_phase("Pending")
        self.subscriber.connect()

        # Monitor connection and update status
        while not self.subscriber.should_stop.is_set():
            if not self.subscriber.connected:
                self.crd_manager.set_phase("Pending")
            time.sleep(2)

if __name__ == "__main__":
    driver = DeviceShifuDriver()
    driver.run()
import os
import sys
import time
import base64
import threading
import yaml
import json
from datetime import datetime, timezone
import queue

import cv2
import paho.mqtt.client as mqtt
from kubernetes import client, config, watch

# --- Constants ---
EDGEDEVICE_CRD_GROUP = "shifu.edgenesis.io"
EDGEDEVICE_CRD_VERSION = "v1alpha1"
EDGEDEVICE_CRD_PLURAL = "edgedevices"
INSTRUCTIONS_PATH = "/etc/edgedevice/config/instructions"
INSTRUCTIONS_FILE = os.path.join(INSTRUCTIONS_PATH, "instructions.yaml")
PHASE_PENDING = "Pending"
PHASE_RUNNING = "Running"
PHASE_FAILED = "Failed"
PHASE_UNKNOWN = "Unknown"

# --- Environment Variables ---
EDGEDEVICE_NAME = os.getenv("EDGEDEVICE_NAME")
EDGEDEVICE_NAMESPACE = os.getenv("EDGEDEVICE_NAMESPACE")
MQTT_BROKER_ADDRESS = os.getenv("MQTT_BROKER_ADDRESS")
DEVICE_ID = EDGEDEVICE_NAME

if not all([EDGEDEVICE_NAME, EDGEDEVICE_NAMESPACE, MQTT_BROKER_ADDRESS]):
    print("Missing required environment variables.", file=sys.stderr)
    sys.exit(1)

# --- Global State ---
streaming = False
streaming_lock = threading.Lock()
client_connected = threading.Event()
last_k8s_update = None
video_capture = None
video_thread = None
snapshot_queue = queue.Queue()
mqtt_client = None
mqtt_options = {}

def get_k8s_api():
    config.load_incluster_config()
    return client.CustomObjectsApi()

def get_edgedevice(api):
    return api.get_namespaced_custom_object(
        group=EDGEDEVICE_CRD_GROUP,
        version=EDGEDEVICE_CRD_VERSION,
        namespace=EDGEDEVICE_NAMESPACE,
        plural=EDGEDEVICE_CRD_PLURAL,
        name=EDGEDEVICE_NAME
    )

def patch_edgedevice_phase(api, phase):
    body = {"status": {"edgeDevicePhase": phase}}
    try:
        api.patch_namespaced_custom_object_status(
            group=EDGEDEVICE_CRD_GROUP,
            version=EDGEDEVICE_CRD_VERSION,
            namespace=EDGEDEVICE_NAMESPACE,
            plural=EDGEDEVICE_CRD_PLURAL,
            name=EDGEDEVICE_NAME,
            body=body
        )
    except Exception:
        pass

def load_instructions():
    try:
        with open(INSTRUCTIONS_FILE, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def get_camera_rtsp_url(edgedevice):
    try:
        return edgedevice["spec"]["address"]
    except Exception:
        return None

def encode_jpeg_frame(frame):
    _, jpeg = cv2.imencode('.jpg', frame)
    return base64.b64encode(jpeg.tobytes()).decode('utf-8')

def get_iso8601_timestamp():
    return datetime.now(timezone.utc).isoformat()

# --- MQTT ---
def on_mqtt_connect(client, userdata, flags, rc):
    if rc == 0:
        client_connected.set()
        subscribe_control_topics(client)
    else:
        client_connected.clear()

def on_mqtt_disconnect(client, userdata, rc):
    client_connected.clear()

def on_mqtt_message(client, userdata, msg):
    global streaming
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        return
    topic = msg.topic

    # PTZ
    if topic.endswith("/commands/ptz"):
        # PTZ command placeholder: not implemented in driver
        pass

    # Stream control (start/stop)
    elif topic.endswith("/commands/stream"):
        action = payload.get("action")
        if action == "start":
            start_streaming()
        elif action == "stop":
            stop_streaming()

    elif topic.endswith("/commands/start"):
        start_streaming()

    elif topic.endswith("/commands/stop"):
        stop_streaming()

    # One-time snapshot
    elif topic.endswith("/snapshot"):
        snapshot_queue.put("snapshot")

def subscribe_control_topics(client):
    base_topics = [
        f"device/{DEVICE_ID}/commands/ptz",
        f"device/{DEVICE_ID}/commands/stream",
        f"device/{DEVICE_ID}/commands/start",
        f"device/{DEVICE_ID}/commands/stop",
        f"device/{DEVICE_ID}/snapshot"
    ]
    for topic in base_topics:
        client.subscribe(topic, qos=1)

def mqtt_publish(topic, payload, qos=1):
    client_connected.wait()
    mqtt_client.publish(topic, payload, qos=qos, retain=False)

def init_mqtt_client():
    global mqtt_client
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_disconnect = on_mqtt_disconnect
    mqtt_client.on_message = on_mqtt_message

    # Optional authentication from environment
    mqtt_user = os.getenv("MQTT_USERNAME")
    mqtt_pass = os.getenv("MQTT_PASSWORD")
    if mqtt_user:
        mqtt_client.username_pw_set(mqtt_user, mqtt_pass)
    broker, port = MQTT_BROKER_ADDRESS.split(":")
    mqtt_client.connect(broker, int(port), keepalive=60)
    threading.Thread(target=mqtt_client.loop_forever, daemon=True).start()

# --- Video & Snapshots ---
def video_frame_publisher(rtsp_url):
    global streaming, video_capture
    try:
        video_capture = cv2.VideoCapture(rtsp_url)
        if not video_capture.isOpened():
            return False
        while True:
            with streaming_lock:
                if not streaming:
                    time.sleep(0.2)
                    continue
            ret, frame = video_capture.read()
            if not ret:
                break
            jpeg_b64 = encode_jpeg_frame(frame)
            now = get_iso8601_timestamp()
            payload = json.dumps({"timestamp": now, "frame": jpeg_b64})
            mqtt_publish(f"device/{DEVICE_ID}/video/frame", payload, qos=1)
            # Streaming status
            mqtt_publish(f"device/{DEVICE_ID}/status/streaming",
                         json.dumps({"streaming": True, "timestamp": now}), qos=1)
            # Periodic snapshot (optional, e.g., every N frames)
            if now[-2:] == "00":  # every minute (just as an example)
                mqtt_publish(f"device/{DEVICE_ID}/video/snapshot",
                             json.dumps({"timestamp": now, "snapshot": jpeg_b64}), qos=1)
            # Respond to snapshot requests
            while not snapshot_queue.empty():
                try:
                    snapshot_queue.get_nowait()
                except queue.Empty:
                    break
                mqtt_publish(f"device/{DEVICE_ID}/snapshot",
                             json.dumps({"timestamp": now, "image": jpeg_b64}), qos=1)
            time.sleep(0.1)
        video_capture.release()
        return True
    except Exception:
        if video_capture is not None:
            video_capture.release()
        return False

def start_streaming():
    global streaming, video_thread
    with streaming_lock:
        if not streaming:
            streaming = True
            if video_thread is None or not video_thread.is_alive():
                video_thread = threading.Thread(target=video_streaming_loop, daemon=True)
                video_thread.start()

def stop_streaming():
    global streaming
    with streaming_lock:
        if streaming:
            streaming = False
            # publish streaming stopped
            now = get_iso8601_timestamp()
            mqtt_publish(f"device/{DEVICE_ID}/status/streaming",
                         json.dumps({"streaming": False, "timestamp": now}), qos=1)

def video_streaming_loop():
    api = get_k8s_api()
    try:
        edgedevice = get_edgedevice(api)
        rtsp_url = get_camera_rtsp_url(edgedevice)
        if not rtsp_url:
            patch_edgedevice_phase(api, PHASE_FAILED)
            return
    except Exception:
        patch_edgedevice_phase(api, PHASE_FAILED)
        return
    patch_edgedevice_phase(api, PHASE_RUNNING)
    res = video_frame_publisher(rtsp_url)
    if not res:
        patch_edgedevice_phase(api, PHASE_FAILED)
    else:
        patch_edgedevice_phase(api, PHASE_RUNNING)

# --- K8s Device Status Monitor ---
def device_status_monitor():
    api = get_k8s_api()
    phase = PHASE_PENDING
    patch_edgedevice_phase(api, phase)
    prev_status = None
    while True:
        try:
            edgedevice = get_edgedevice(api)
            rtsp_url = get_camera_rtsp_url(edgedevice)
            if not rtsp_url:
                phase = PHASE_PENDING
            else:
                if streaming:
                    phase = PHASE_RUNNING
                else:
                    phase = PHASE_PENDING
        except Exception:
            phase = PHASE_FAILED
        if prev_status != phase:
            patch_edgedevice_phase(api, phase)
            prev_status = phase
        time.sleep(5)

# --- Main ---
def main():
    # Load instructions/config
    instructions = load_instructions()
    # Start MQTT client
    init_mqtt_client()
    # Start K8s status monitor thread
    threading.Thread(target=device_status_monitor, daemon=True).start()
    # Wait for MQTT connection
    client_connected.wait()

    # Main thread: idle, all work is threaded/event-based
    while True:
        time.sleep(10)

if __name__ == "__main__":
    main()
import os
import threading
import time
import json
import requests
import queue
import base64
from flask import Flask, request, Response, jsonify, stream_with_context

try:
    import paho.mqtt.client as mqtt
except ImportError:
    raise ImportError("paho-mqtt is required, install it via pip")

# Environment variable config
HTTP_SERVER_HOST = os.environ.get("HTTP_SERVER_HOST", "0.0.0.0")
HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", "8080"))
MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "localhost")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "hikvision/camera/stream")
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "hikvision_driver_client")
MQTT_USERNAME = os.environ.get("MQTT_USERNAME")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")
CAMERA_URL = os.environ.get("CAMERA_URL", "http://192.168.1.64/ISAPI/Streaming/channels/101/httpPreview")
CAMERA_USERNAME = os.environ.get("CAMERA_USERNAME")
CAMERA_PASSWORD = os.environ.get("CAMERA_PASSWORD")
CAMERA_STREAM_PATH = os.environ.get("CAMERA_STREAM_PATH", "/ISAPI/Streaming/channels/101/httpPreview")
CAMERA_PROTOCOL = os.environ.get("CAMERA_PROTOCOL", "http")
CAMERA_IP = os.environ.get("CAMERA_IP", "192.168.1.64")
CAMERA_PORT = os.environ.get("CAMERA_PORT", "80")

# Persisted config (in-memory for simplicity)
persisted = {
    "camera": {
        "url": CAMERA_URL,
        "username": CAMERA_USERNAME,
        "password": CAMERA_PASSWORD,
        "stream_path": CAMERA_STREAM_PATH,
        "protocol": CAMERA_PROTOCOL,
        "ip": CAMERA_IP,
        "port": CAMERA_PORT
    },
    "mqtt": {
        "broker_host": MQTT_BROKER_HOST,
        "broker_port": MQTT_BROKER_PORT,
        "topic": MQTT_TOPIC,
        "client_id": MQTT_CLIENT_ID,
        "username": MQTT_USERNAME,
        "password": MQTT_PASSWORD
    }
}

status = {
    "forwarding": False,
    "uptime": 0,
    "frames_published": 0,
    "last_error": None,
    "start_time": None,
    "fps": 0
}

# MQTT Forwarding Worker
class StreamForwarder(threading.Thread):
    def __init__(self, camera_conf, mqtt_conf):
        super().__init__()
        self.camera_conf = camera_conf
        self.mqtt_conf = mqtt_conf
        self._stop_event = threading.Event()
        self.frames_published = 0
        self.fps = 0
        self.last_error = None
        self._client = None

    def run(self):
        global status
        mqttc = mqtt.Client(self.mqtt_conf["client_id"])
        if self.mqtt_conf["username"]:
            mqttc.username_pw_set(self.mqtt_conf["username"], self.mqtt_conf["password"])
        try:
            mqttc.connect(self.mqtt_conf["broker_host"], int(self.mqtt_conf["broker_port"]), 60)
        except Exception as e:
            status["last_error"] = f"MQTT connect error: {str(e)}"
            status["forwarding"] = False
            return
        self._client = mqttc

        url = self.camera_conf.get("url") or f'{self.camera_conf["protocol"]}://{self.camera_conf["ip"]}:{self.camera_conf["port"]}{self.camera_conf["stream_path"]}'
        auth = None
        if self.camera_conf.get("username"):
            auth = (self.camera_conf["username"], self.camera_conf["password"])

        # ISAPI HTTP stream is multipart/x-mixed-replace; boundary=... (MJPEG)
        try:
            with requests.get(url, auth=auth, stream=True, timeout=10) as r:
                r.raise_for_status()
                boundary = None
                ct = r.headers.get("Content-Type", "")
                if "boundary=" in ct:
                    boundary = ct.split("boundary=")[-1]
                    if boundary.startswith('"') and boundary.endswith('"'):
                        boundary = boundary[1:-1]
                else:
                    boundary = "--myboundary"
                delimiter = b"--" + boundary.encode("utf-8")
                buf = b""
                last_frame_time = time.time()
                frame_count = 0
                period = 1
                start = time.time()
                for chunk in r.iter_content(chunk_size=4096):
                    if self._stop_event.is_set():
                        break
                    buf += chunk
                    while True:
                        # Find the next delimiter
                        start_idx = buf.find(delimiter)
                        if start_idx < 0:
                            break
                        # Find the next delimiter after start_idx+len(delimiter)
                        next_idx = buf.find(delimiter, start_idx + len(delimiter))
                        if next_idx < 0:
                            break
                        frame = buf[start_idx + len(delimiter):next_idx]
                        buf = buf[next_idx:]
                        # Frame headers and JPEG data
                        # Remove potential \r\n at start
                        frame = frame.lstrip(b"\r\n")
                        # Find end of headers
                        header_end = frame.find(b"\r\n\r\n")
                        if header_end < 0:
                            continue
                        headers = frame[:header_end]
                        jpeg_data = frame[header_end + 4:]
                        # Publish to MQTT as base64-encoded JPEG with timestamp and meta
                        payload = {
                            "timestamp": time.time(),
                            "image_base64": base64.b64encode(jpeg_data).decode("ascii"),
                            "content_type": "image/jpeg"
                        }
                        try:
                            mqttc.publish(self.mqtt_conf["topic"], json.dumps(payload), qos=0)
                        except Exception as me:
                            status["last_error"] = f"MQTT publish error: {str(me)}"
                            self.last_error = status["last_error"]
                            status["forwarding"] = False
                            return
                        self.frames_published += 1
                        frame_count += 1
                        now = time.time()
                        if now - last_frame_time > period:
                            self.fps = frame_count / (now - last_frame_time)
                            last_frame_time = now
                            frame_count = 0
                status["fps"] = self.fps
        except Exception as e:
            status["last_error"] = f"Stream error: {str(e)}"
            self.last_error = status["last_error"]
            status["forwarding"] = False
            return
        status["forwarding"] = False

    def stop(self):
        self._stop_event.set()

forwarder = None
forwarder_lock = threading.Lock()

app = Flask(__name__)

def update_status():
    global status, forwarder
    if status["forwarding"] and status["start_time"]:
        status["uptime"] = int(time.time() - status["start_time"])
        if forwarder:
            status["frames_published"] = forwarder.frames_published
            status["fps"] = forwarder.fps
            status["last_error"] = forwarder.last_error

@app.route("/config", methods=["GET"])
def get_config():
    return jsonify(persisted)

@app.route("/config", methods=["PUT"])
def put_config():
    data = request.get_json(force=True)
    cam = data.get("camera")
    mqttconf = data.get("mqtt")
    if cam:
        persisted["camera"].update(cam)
    if mqttconf:
        persisted["mqtt"].update(mqttconf)
    return jsonify(persisted)

@app.route("/camera/settings", methods=["PUT"])
def camera_settings():
    data = request.get_json(force=True)
    persisted["camera"].update(data)
    return jsonify({"status": "ok", "camera": persisted["camera"]})

@app.route("/mqtt/settings", methods=["PUT"])
def mqtt_settings():
    data = request.get_json(force=True)
    persisted["mqtt"].update(data)
    return jsonify({"status": "ok", "mqtt": persisted["mqtt"]})

@app.route("/stream/start", methods=["POST"])
@app.route("/stream/forward", methods=["POST"])
def stream_start():
    global forwarder, status
    with forwarder_lock:
        if status["forwarding"]:
            return jsonify({"status": "already running"}), 409
        forwarder = StreamForwarder(dict(persisted["camera"]), dict(persisted["mqtt"]))
        forwarder.daemon = True
        status["forwarding"] = True
        status["start_time"] = time.time()
        status["frames_published"] = 0
        status["last_error"] = None
        forwarder.start()
    return jsonify({"status": "forwarding started"}), 202

@app.route("/stream/stop", methods=["POST"])
@app.route("/stream/forward", methods=["DELETE"])
def stream_stop():
    global forwarder, status
    with forwarder_lock:
        if not status["forwarding"]:
            return jsonify({"status": "not running"}), 409
        if forwarder:
            forwarder.stop()
            forwarder.join(timeout=5)
        status["forwarding"] = False
        status["uptime"] = int(time.time() - status["start_time"]) if status["start_time"] else 0
        status["start_time"] = None
    return jsonify({"status": "forwarding stopped"})

@app.route("/stream/status", methods=["GET"])
def stream_status():
    update_status()
    return jsonify({
        "forwarding": status["forwarding"],
        "uptime": status["uptime"],
        "frames_published": status["frames_published"],
        "fps": status.get("fps", 0),
        "last_error": status["last_error"]
    })

# HTTP MJPEG stream proxy endpoint (for browser/CLI view)
@app.route("/stream/http", methods=["GET"])
def http_stream():
    cam = persisted["camera"]
    url = cam.get("url") or f'{cam["protocol"]}://{cam["ip"]}:{cam["port"]}{cam["stream_path"]}'
    auth = None
    if cam.get("username"):
        auth = (cam["username"], cam["password"])
    def generate():
        with requests.get(url, stream=True, auth=auth, timeout=10) as r:
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            yield f'HTTP/1.1 200 OK\r\nContent-Type: {ct}\r\n\r\n'.encode("utf-8")
            for chunk in r.iter_content(chunk_size=4096):
                yield chunk
    # Properly pass through content-type and multipart boundary for MJPEG
    with requests.get(url, stream=True, auth=auth, timeout=10) as r:
        ct = r.headers.get("Content-Type", "")
    return Response(stream_with_context(generate()), content_type=ct)

if __name__ == "__main__":
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)
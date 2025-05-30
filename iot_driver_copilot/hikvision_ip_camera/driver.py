import os
import json
import time
import threading
from datetime import datetime
from io import BytesIO

from flask import Flask, Response, request, jsonify, stream_with_context

import requests
import paho.mqtt.client as mqtt

app = Flask(__name__)

# Environment variable configuration
HTTP_SERVER_HOST = os.environ.get("HTTP_SERVER_HOST", "0.0.0.0")
HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", "8080"))

# Persistent configuration (in-memory for this example; use files/db in production)
CONFIG_PATH = "config.json"
DEFAULT_CONFIG = {
    "camera": {
        "url": os.environ.get("CAMERA_URL", ""),
        "username": os.environ.get("CAMERA_USERNAME", ""),
        "password": os.environ.get("CAMERA_PASSWORD", ""),
        "stream_path": os.environ.get("CAMERA_STREAM_PATH", "/Streaming/channels/101/httpPreview"),
    },
    "mqtt": {
        "host": os.environ.get("MQTT_HOST", "localhost"),
        "port": int(os.environ.get("MQTT_PORT", "1883")),
        "topic": os.environ.get("MQTT_TOPIC", "hikvision/stream"),
        "username": os.environ.get("MQTT_USERNAME", ""),
        "password": os.environ.get("MQTT_PASSWORD", ""),
        "client_id": os.environ.get("MQTT_CLIENT_ID", "hikvision_forwarder"),
        "tls": False
    }
}

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    else:
        return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)

config = load_config()

# Streaming/forwarding status
class StreamStatus:
    def __init__(self):
        self.active = False
        self.started_at = None
        self.frames_published = 0
        self.last_error = None
        self.fps = 0.0
        self._frame_times = []

    def start(self):
        self.active = True
        self.started_at = datetime.now().isoformat()
        self.frames_published = 0
        self.last_error = None
        self.fps = 0.0
        self._frame_times = []

    def stop(self):
        self.active = False
        self.fps = 0.0
        self._frame_times = []

    def frame(self):
        now = time.time()
        self.frames_published += 1
        self._frame_times.append(now)
        # Keep only last 5 seconds for FPS calc
        self._frame_times = [t for t in self._frame_times if now - t < 5]
        if len(self._frame_times) > 1:
            self.fps = len(self._frame_times) / (self._frame_times[-1] - self._frame_times[0] + 1e-6)
        else:
            self.fps = 0.0

    def error(self, msg):
        self.last_error = msg

    def to_dict(self):
        uptime = 0
        if self.active and self.started_at:
            uptime = (datetime.now() - datetime.fromisoformat(self.started_at)).total_seconds()
        return {
            "active": self.active,
            "uptime_sec": uptime,
            "frames_published": self.frames_published,
            "fps": round(self.fps, 2),
            "last_error": self.last_error
        }

stream_status = StreamStatus()
_stream_thread = None
_stream_thread_stop = threading.Event()

def get_camera_stream_url():
    c = config["camera"]
    url = c["url"]
    if not url:
        # Compose from components
        ip = os.environ.get("CAMERA_IP")
        port = os.environ.get("CAMERA_PORT", "80")
        proto = "http"
        stream_path = c.get("stream_path") or "/Streaming/channels/101/httpPreview"
        if ip:
            url = f"{proto}://{ip}:{port}{stream_path}"
        else:
            url = ""
    return url

def _connect_camera_stream():
    url = get_camera_stream_url()
    auth = None
    if config["camera"]["username"]:
        auth = (config["camera"]["username"], config["camera"]["password"])
    headers = {"Accept": "multipart/x-mixed-replace"}
    try:
        resp = requests.get(url, auth=auth, headers=headers, stream=True, timeout=5)
        resp.raise_for_status()
        return resp
    except Exception as ex:
        stream_status.error(str(ex))
        return None

def _parse_mjpeg_stream(resp):
    boundary = None
    content_type = resp.headers.get("Content-Type", "")
    if "boundary=" in content_type:
        boundary = content_type.split("boundary=", 1)[1]
        if boundary.startswith('"') and boundary.endswith('"'):
            boundary = boundary[1:-1]
        if not boundary.startswith("--"):
            boundary = "--" + boundary
    else:
        boundary = "--myboundary"
    delimiter = boundary.encode()
    buffer = b''
    for chunk in resp.iter_content(chunk_size=4096):
        if _stream_thread_stop.is_set():
            break
        buffer += chunk
        while True:
            idx = buffer.find(delimiter)
            if idx == -1:
                break
            part = buffer[:idx]
            buffer = buffer[idx + len(delimiter):]
            if b'Content-Type: image/jpeg' in part:
                start = part.find(b'\r\n\r\n')
                if start != -1:
                    jpeg_data = part[start+4:]
                    yield jpeg_data
            # else ignore non-jpeg part

def _forward_stream_to_mqtt():
    global stream_status
    stream_status.start()
    mqtt_cfg = config["mqtt"]
    client = mqtt.Client(mqtt_cfg.get("client_id", "hikvision_forwarder"))
    if mqtt_cfg.get("username"):
        client.username_pw_set(mqtt_cfg["username"], mqtt_cfg.get("password", ""))
    if mqtt_cfg.get("tls"):
        client.tls_set()
    try:
        client.connect(mqtt_cfg["host"], mqtt_cfg["port"])
    except Exception as ex:
        stream_status.error("MQTT connect error: %s" % str(ex))
        stream_status.stop()
        return
    client.loop_start()
    resp = _connect_camera_stream()
    if not resp:
        stream_status.stop()
        client.loop_stop()
        return
    try:
        for jpeg_data in _parse_mjpeg_stream(resp):
            if _stream_thread_stop.is_set():
                break
            client.publish(mqtt_cfg["topic"], jpeg_data)
            stream_status.frame()
    except Exception as ex:
        stream_status.error("Streaming error: %s" % str(ex))
    finally:
        client.loop_stop()
        stream_status.stop()

def start_forwarding():
    global _stream_thread, _stream_thread_stop
    if stream_status.active:
        return False
    _stream_thread_stop.clear()
    _stream_thread = threading.Thread(target=_forward_stream_to_mqtt, daemon=True)
    _stream_thread.start()
    return True

def stop_forwarding():
    global _stream_thread, _stream_thread_stop
    _stream_thread_stop.set()
    if _stream_thread and _stream_thread.is_alive():
        _stream_thread.join(timeout=2)
    stream_status.stop()

@app.route("/config", methods=["GET"])
def get_config():
    return jsonify(config)

@app.route("/config", methods=["PUT"])
def update_config():
    body = request.get_json(force=True)
    config.update(body)
    save_config(config)
    return jsonify({"status": "ok", "config": config})

@app.route("/camera/settings", methods=["PUT"])
def camera_settings():
    body = request.get_json(force=True)
    config["camera"].update(body)
    save_config(config)
    return jsonify({"status": "ok", "camera": config["camera"]})

@app.route("/mqtt/settings", methods=["PUT"])
def mqtt_settings():
    body = request.get_json(force=True)
    config["mqtt"].update(body)
    save_config(config)
    return jsonify({"status": "ok", "mqtt": config["mqtt"]})

@app.route("/stream/forward", methods=["POST"])
def api_stream_forward():
    if stream_status.active:
        return jsonify({"status": "already_started"}), 202
    started = start_forwarding()
    if started:
        return jsonify({"status": "started"}), 202
    else:
        return jsonify({"status": "failed", "error": stream_status.last_error}), 500

@app.route("/stream/start", methods=["POST"])
def api_stream_start():
    return api_stream_forward()

@app.route("/stream/stop", methods=["POST"])
@app.route("/stream/forward", methods=["DELETE"])
def api_stream_stop():
    stop_forwarding()
    return jsonify({"status": "stopped"})

@app.route("/stream/status", methods=["GET"])
def stream_status_query():
    return jsonify(stream_status.to_dict())

@app.route("/video", methods=["GET"])
def serve_video():
    # Proxy MJPEG stream over HTTP for browser/CLI viewing
    resp = _connect_camera_stream()
    if not resp:
        return "Could not connect to camera", 502
    boundary = None
    content_type = resp.headers.get("Content-Type", "")
    if "boundary=" in content_type:
        boundary = content_type.split("boundary=", 1)[1]
        if boundary.startswith('"') and boundary.endswith('"'):
            boundary = boundary[1:-1]
        if not boundary.startswith("--"):
            boundary = "--" + boundary
    else:
        boundary = "--myboundary"
    multipart_boundary = boundary

    def generate():
        for jpeg_data in _parse_mjpeg_stream(resp):
            yield (b"%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % (
                multipart_boundary.encode(), len(jpeg_data))) + jpeg_data + b'\r\n'
            if _stream_thread_stop.is_set():
                break

    headers = {
        "Content-Type": "multipart/x-mixed-replace; boundary=%s" % multipart_boundary[2:]
    }
    return Response(stream_with_context(generate()), headers=headers)

if __name__ == "__main__":
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)
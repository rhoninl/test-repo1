import os
import io
import threading
import base64
import time
from flask import Flask, Response, jsonify, request, abort
import requests
import cv2
import numpy as np

# --- Configuration from Environment Variables ---
DEVICE_IP = os.environ.get("DEVICE_IP", "192.168.1.64")
DEVICE_RTSP_PORT = int(os.environ.get("DEVICE_RTSP_PORT", 554))
DEVICE_HTTP_PORT = int(os.environ.get("DEVICE_HTTP_PORT", 80))
DEVICE_USER = os.environ.get("DEVICE_USER", "admin")
DEVICE_PASS = os.environ.get("DEVICE_PASS", "12345")
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", 8080))

RTSP_STREAM_PATH = os.environ.get("RTSP_STREAM_PATH", "Streaming/Channels/101")
SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "/ISAPI/Streaming/channels/101/picture")
ONVIF_PTZ_ENDPOINT = os.environ.get("ONVIF_PTZ_ENDPOINT", "/ISAPI/PTZCtrl/channels/1/continuous")

RTSP_URL = f"rtsp://{DEVICE_USER}:{DEVICE_PASS}@{DEVICE_IP}:{DEVICE_RTSP_PORT}/{RTSP_STREAM_PATH}"

CAMERA_AUTH = (DEVICE_USER, DEVICE_PASS)

# --- Flask App ---
app = Flask(__name__)

# --- RTSP Stream Management ---
streaming_active = threading.Event()
stream_lock = threading.Lock()
current_stream = {"thread": None}

def gen_mjpeg_frames():
    cap = cv2.VideoCapture(RTSP_URL)
    if not cap.isOpened():
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + b"\xff\xd8\xff\xd9" + b"\r\n"
        return
    try:
        while streaming_active.is_set():
            ret, frame = cap.read()
            if not ret:
                continue
            _, jpeg = cv2.imencode('.jpg', frame)
            jpg_bytes = jpeg.tobytes()
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + jpg_bytes + b"\r\n")
    finally:
        cap.release()

def start_streaming():
    with stream_lock:
        streaming_active.set()

def stop_streaming():
    with stream_lock:
        streaming_active.clear()

# --- API Endpoints ---

@app.route("/camera/snapshot", methods=["GET"])
def camera_snapshot():
    # Try HTTP snapshot API
    url = f"http://{DEVICE_IP}:{DEVICE_HTTP_PORT}{SNAPSHOT_PATH}"
    try:
        resp = requests.get(url, auth=CAMERA_AUTH, timeout=5, stream=True)
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image/jpeg"):
            return Response(resp.raw, mimetype="image/jpeg")
    except Exception:
        pass
    # Fallback: Capture from RTSP stream
    cap = cv2.VideoCapture(RTSP_URL)
    ret, frame = cap.read()
    cap.release()
    if not ret:
        abort(503, "Unable to fetch snapshot")
    _, jpeg = cv2.imencode('.jpg', frame)
    return Response(jpeg.tobytes(), mimetype='image/jpeg')

@app.route("/camera/stream", methods=["GET"])
def camera_stream():
    if not streaming_active.is_set():
        start_streaming()
    return Response(gen_mjpeg_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/camera/status", methods=["GET"])
def camera_status():
    status = {
        "online": False,
        "streaming": streaming_active.is_set(),
        "ptz": None
    }
    # Check online status
    try:
        # Try device info API
        resp = requests.get(f"http://{DEVICE_IP}:{DEVICE_HTTP_PORT}/ISAPI/System/status", auth=CAMERA_AUTH, timeout=2)
        status['online'] = resp.status_code == 200
    except Exception:
        status['online'] = False
    # Try to get PTZ status (simplified)
    try:
        ptz_resp = requests.get(f"http://{DEVICE_IP}:{DEVICE_HTTP_PORT}/ISAPI/PTZCtrl/channels/1/status", auth=CAMERA_AUTH, timeout=2)
        if ptz_resp.status_code == 200:
            # This is Hikvision-specific XML; parse as needed
            status['ptz'] = "available"
        else:
            status['ptz'] = None
    except Exception:
        status['ptz'] = None
    return jsonify(status)

@app.route("/camera/ptz", methods=["POST"])
def camera_ptz():
    data = request.get_json(force=True)
    action = data.get("action")
    value = data.get("value", 0)
    speed = data.get("speed", 5)
    if action not in ("pan", "tilt", "zoom"):
        abort(400, "Invalid action")
    # Build ONVIF/Hikvision PTZ XML payload (simplified)
    xml_map = {
        "pan": f"<PTZData><pan>{value}</pan><panSpeed>{speed}</panSpeed></PTZData>",
        "tilt": f"<PTZData><tilt>{value}</tilt><tiltSpeed>{speed}</tiltSpeed></PTZData>",
        "zoom": f"<PTZData><zoom>{value}</zoom><zoomSpeed>{speed}</zoomSpeed></PTZData>"
    }
    xml_payload = f"""<?xml version="1.0" encoding="UTF-8"?><PTZData>{xml_map[action]}</PTZData>"""
    ptz_url = f"http://{DEVICE_IP}:{DEVICE_HTTP_PORT}{ONVIF_PTZ_ENDPOINT}"
    headers = {"Content-Type": "application/xml"}
    resp = requests.put(ptz_url, data=xml_payload, auth=CAMERA_AUTH, headers=headers, timeout=3)
    if resp.status_code not in (200, 204):
        abort(503, "PTZ command failed")
    return jsonify({"result": "success"})

@app.route("/camera/stream", methods=["DELETE"])
def camera_stream_delete():
    stop_streaming()
    return jsonify({"result": "stream stopped"})

# --- Main Entrypoint ---
if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
import os
import io
import threading
import requests
import time
from flask import Flask, Response, request, jsonify, stream_with_context, abort

app = Flask(__name__)

# Configuration from environment variables
CAMERA_IP = os.environ.get('CAMERA_IP')
CAMERA_RTSP_PORT = int(os.environ.get('CAMERA_RTSP_PORT', '554'))
CAMERA_HTTP_PORT = int(os.environ.get('CAMERA_HTTP_PORT', '80'))
CAMERA_USER = os.environ.get('CAMERA_USER', 'admin')
CAMERA_PASSWORD = os.environ.get('CAMERA_PASSWORD', '12345')
SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', '8080'))

# Camera endpoints (Hikvision standard)
SNAPSHOT_URL = f"http://{CAMERA_IP}:{CAMERA_HTTP_PORT}/ISAPI/Streaming/channels/101/picture"
STATUS_URL = f"http://{CAMERA_IP}:{CAMERA_HTTP_PORT}/ISAPI/System/status"
PTZ_URL = f"http://{CAMERA_IP}:{CAMERA_HTTP_PORT}/ISAPI/PTZCtrl/channels/1/continuous"
# RTSP Stream URL
RTSP_URL = f"rtsp://{CAMERA_USER}:{CAMERA_PASSWORD}@{CAMERA_IP}:{CAMERA_RTSP_PORT}/Streaming/Channels/101/"

# --- Video Streaming Proxy (RTSP to MJPEG over HTTP) ---

import cv2
import base64

class CameraStream:
    def __init__(self, rtsp_url, user, password):
        self.rtsp_url = rtsp_url
        self.user = user
        self.password = password
        self.cap = None
        self.lock = threading.Lock()
        self.running = False
        self.last_frame = None
        self.thread = None

    def start(self):
        with self.lock:
            if not self.running:
                self.cap = cv2.VideoCapture(self.rtsp_url)
                self.running = True
                self.thread = threading.Thread(target=self._update, daemon=True)
                self.thread.start()

    def stop(self):
        with self.lock:
            self.running = False
            if self.cap:
                self.cap.release()
                self.cap = None

    def _update(self):
        while self.running and self.cap and self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret:
                # Encode as JPEG
                ret2, jpeg = cv2.imencode('.jpg', frame)
                if ret2:
                    self.last_frame = jpeg.tobytes()
            else:
                # Try to reconnect after a brief pause
                time.sleep(1)
                if self.cap:
                    self.cap.release()
                self.cap = cv2.VideoCapture(self.rtsp_url)
        if self.cap:
            self.cap.release()

    def get_frame(self):
        with self.lock:
            return self.last_frame

streamer = CameraStream(RTSP_URL, CAMERA_USER, CAMERA_PASSWORD)

# --- Snapshot API ---

@app.route("/camera/snapshot", methods=["GET"])
def camera_snapshot():
    url = SNAPSHOT_URL
    try:
        resp = requests.get(url, auth=(CAMERA_USER, CAMERA_PASSWORD), timeout=5, stream=True)
        if resp.status_code == 200 and resp.headers.get('Content-Type', '').startswith('image/jpeg'):
            return Response(resp.content, mimetype='image/jpeg')
        else:
            # As fallback, grab from RTSP stream
            streamer.start()
            frame = streamer.get_frame()
            if frame is not None:
                return Response(frame, mimetype='image/jpeg')
            else:
                return "Unable to capture snapshot", 503
    except Exception as ex:
        # Try fallback as above
        streamer.start()
        frame = streamer.get_frame()
        if frame is not None:
            return Response(frame, mimetype='image/jpeg')
        else:
            return "Unable to capture snapshot", 503

# --- Video Stream API (MJPEG over HTTP) ---

def gen_mjpeg():
    streamer.start()
    while True:
        frame = streamer.get_frame()
        if frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        else:
            time.sleep(0.1)

@app.route("/camera/stream", methods=["GET"])
def camera_stream():
    return Response(gen_mjpeg(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/camera/stream", methods=["DELETE"])
def stop_camera_stream():
    streamer.stop()
    return jsonify({"status": "stopped"}), 200

# --- Camera Status API ---

@app.route("/camera/status", methods=["GET"])
def camera_status():
    # Get online/offline and PTZ position
    status = {
        "online": False,
        "streaming": streamer.running,
        "ptz": None
    }
    # Try connection to camera status endpoint
    try:
        resp = requests.get(STATUS_URL, auth=(CAMERA_USER, CAMERA_PASSWORD), timeout=3)
        if resp.status_code == 200:
            status["online"] = True
    except Exception:
        status["online"] = False
    # Get last PTZ position (best effort)
    try:
        ptz_status_url = f"http://{CAMERA_IP}:{CAMERA_HTTP_PORT}/ISAPI/PTZCtrl/channels/1/status"
        resp = requests.get(ptz_status_url, auth=(CAMERA_USER, CAMERA_PASSWORD), timeout=3)
        if resp.status_code == 200:
            # Hikvision XML; parse simple fields
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(resp.content)
                pan = root.find('.//absoluteHigh').text if root.find('.//absoluteHigh') is not None else None
                tilt = root.find('.//absoluteTilt').text if root.find('.//absoluteTilt') is not None else None
                zoom = root.find('.//absoluteZoom').text if root.find('.//absoluteZoom') is not None else None
                status["ptz"] = {
                    "pan": pan,
                    "tilt": tilt,
                    "zoom": zoom
                }
            except Exception:
                status["ptz"] = None
    except Exception:
        status["ptz"] = None
    return jsonify(status)

# --- PTZ Control API ---

@app.route("/camera/ptz", methods=["POST"])
def camera_ptz():
    data = request.get_json(force=True)
    action = data.get("action")
    value = data.get("value", 0)
    speed = data.get("speed", 1)

    # Map action to Hikvision PTZ XML
    # actions: "pan_left", "pan_right", "tilt_up", "tilt_down", "zoom_in", "zoom_out", or "stop"
    ptz_cmds = {
        "pan_left":   {"pan": -speed, "tilt": 0, "zoom": 0},
        "pan_right":  {"pan": speed,  "tilt": 0, "zoom": 0},
        "tilt_up":    {"pan": 0, "tilt": speed,  "zoom": 0},
        "tilt_down":  {"pan": 0, "tilt": -speed, "zoom": 0},
        "zoom_in":    {"pan": 0, "tilt": 0, "zoom": speed},
        "zoom_out":   {"pan": 0, "tilt": 0, "zoom": -speed},
        "stop":       {"pan": 0, "tilt": 0, "zoom": 0}
    }
    if action in ptz_cmds:
        ptz = ptz_cmds[action]
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<PTZData>
    <pan>{ptz['pan']}</pan>
    <tilt>{ptz['tilt']}</tilt>
    <zoom>{ptz['zoom']}</zoom>
</PTZData>
"""
        url = PTZ_URL
        headers = {'Content-Type': 'application/xml'}
        try:
            resp = requests.put(url, data=xml, headers=headers, auth=(CAMERA_USER, CAMERA_PASSWORD), timeout=2)
            if resp.status_code in (200, 201, 204):
                return jsonify({"result": "success"}), 200
            else:
                return jsonify({"result": "error", "details": resp.text}), 400
        except Exception as ex:
            return jsonify({"result": "error", "details": str(ex)}), 500
    else:
        return jsonify({"result": "error", "details": "Unknown PTZ action"}), 400

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
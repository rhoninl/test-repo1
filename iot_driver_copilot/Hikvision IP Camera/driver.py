import os
import io
import json
import time
import threading
import requests
from flask import Flask, Response, request, jsonify, stream_with_context, abort

app = Flask(__name__)

# Environment variables for configuration
DEVICE_IP = os.environ.get('DEVICE_IP', '192.168.1.64')
DEVICE_RTSP_PORT = int(os.environ.get('DEVICE_RTSP_PORT', '554'))
DEVICE_SNAPSHOT_PORT = int(os.environ.get('DEVICE_SNAPSHOT_PORT', '80'))
DEVICE_USERNAME = os.environ.get('DEVICE_USERNAME', 'admin')
DEVICE_PASSWORD = os.environ.get('DEVICE_PASSWORD', '12345')
SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', '8000'))

# Camera API endpoints
RTSP_STREAM_PATH = os.environ.get('RTSP_STREAM_PATH', '/Streaming/Channels/101')
SNAPSHOT_PATH = os.environ.get('SNAPSHOT_PATH', '/ISAPI/Streaming/channels/101/picture')
PTZ_PATH = os.environ.get('PTZ_PATH', '/ISAPI/PTZCtrl/channels/1/continuous')
STATUS_PATH = os.environ.get('STATUS_PATH', '/ISAPI/System/status')

# Session state
active_streams = {}

def device_rtsp_url():
    return f"rtsp://{DEVICE_USERNAME}:{DEVICE_PASSWORD}@{DEVICE_IP}:{DEVICE_RTSP_PORT}{RTSP_STREAM_PATH}"

def device_snapshot_url():
    return f"http://{DEVICE_IP}:{DEVICE_SNAPSHOT_PORT}{SNAPSHOT_PATH}"

def device_status_url():
    return f"http://{DEVICE_IP}:{DEVICE_SNAPSHOT_PORT}{STATUS_PATH}"

def device_ptz_url():
    return f"http://{DEVICE_IP}:{DEVICE_SNAPSHOT_PORT}{PTZ_PATH}"

def get_device_status():
    try:
        resp = requests.get(device_status_url(), auth=(DEVICE_USERNAME, DEVICE_PASSWORD), timeout=3)
        return resp.json()
    except Exception as e:
        return {
            "online": False,
            "error": str(e)
        }

def get_snapshot_bytes():
    try:
        resp = requests.get(device_snapshot_url(), auth=(DEVICE_USERNAME, DEVICE_PASSWORD), stream=True, timeout=5)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        return None

def send_ptz_command(direction=None, speed=5, stop=False):
    """
    Hikvision ISAPI PTZ control. Example: move left/right/up/down/zoom_in/zoom_out/stop
    """
    mapping = {
        "left": {"pan": -speed, "tilt": 0, "zoom": 0},
        "right": {"pan": speed, "tilt": 0, "zoom": 0},
        "up": {"pan": 0, "tilt": speed, "zoom": 0},
        "down": {"pan": 0, "tilt": -speed, "zoom": 0},
        "zoom_in": {"pan": 0, "tilt": 0, "zoom": speed},
        "zoom_out": {"pan": 0, "tilt": 0, "zoom": -speed},
        "stop": {"pan": 0, "tilt": 0, "zoom": 0}
    }
    if stop:
        action = mapping["stop"]
    else:
        if direction not in mapping:
            return False, "Invalid direction"
        action = mapping[direction]

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<PTZData>
    <pan>{action['pan']}</pan>
    <tilt>{action['tilt']}</tilt>
    <zoom>{action['zoom']}</zoom>
</PTZData>"""
    try:
        resp = requests.put(device_ptz_url(), data=xml, auth=(DEVICE_USERNAME, DEVICE_PASSWORD), headers={'Content-Type': 'application/xml'}, timeout=3)
        if resp.status_code in (200, 201, 204):
            return True, "OK"
        else:
            return False, f"PTZ HTTP {resp.status_code}: {resp.text}"
    except Exception as e:
        return False, str(e)

def rtsp_to_mjpeg(rtsp_url):
    """
    Use OpenCV to connect to RTSP, read H.264 frames, encode as JPEG, and stream as MJPEG.
    """
    import cv2

    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        yield b''
        return

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            _, jpeg = cv2.imencode('.jpg', frame)
            jpg_bytes = jpeg.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpg_bytes + b'\r\n')
    finally:
        cap.release()

@app.route('/status', methods=['GET'])
@app.route('/camera/status', methods=['GET'])
def camera_status():
    status = get_device_status()
    return jsonify(status)

@app.route('/snapshot', methods=['GET'])
@app.route('/camera/snapshot', methods=['GET'])
def snapshot():
    image = get_snapshot_bytes()
    if image is None:
        abort(503)
    return Response(image, mimetype='image/jpeg')

@app.route('/stream', methods=['GET'])
@app.route('/camera/stream', methods=['GET'])
def stream():
    # Each client gets a new RTSP session
    rtsp_url = device_rtsp_url()
    def generate():
        for frame in rtsp_to_mjpeg(rtsp_url):
            yield frame
    response = Response(stream_with_context(generate()), mimetype='multipart/x-mixed-replace; boundary=frame')
    return response

@app.route('/stream', methods=['DELETE'])
@app.route('/camera/stream', methods=['DELETE'])
def stop_stream():
    # Since each HTTP request is stateless, client just closes the connection to stop stream.
    return jsonify({"status": "stream stopped"}), 200

@app.route('/ptz/move', methods=['POST'])
def ptz_move():
    data = request.get_json(force=True)
    direction = data.get('direction')
    speed = int(data.get('speed', 5))
    stop = direction == 'stop'
    ok, msg = send_ptz_command(direction, speed, stop)
    status = "success" if ok else "error"
    code = 200 if ok else 400
    return jsonify({"status": status, "message": msg}), code

@app.route('/camera/ptz', methods=['POST'])
def ptz_action():
    data = request.get_json(force=True)
    action = data.get('action')
    value = int(data.get('value', 0))
    speed = int(data.get('speed', 5))
    direction = None
    if action == "zoom":
        direction = "zoom_in" if value > 0 else "zoom_out"
    elif action in ("left", "right", "up", "down"):
        direction = action
    elif action == "stop":
        direction = "stop"
    else:
        return jsonify({"status": "error", "message": "Invalid PTZ action"}), 400

    stop = (direction == "stop")
    ok, msg = send_ptz_command(direction, speed, stop)
    status = "success" if ok else "error"
    code = 200 if ok else 400
    return jsonify({"status": status, "message": msg}), code

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
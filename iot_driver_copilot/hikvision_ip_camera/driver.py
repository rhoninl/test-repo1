import os
import io
import threading
import time
import requests
from flask import Flask, Response, send_file, jsonify, request, abort
import cv2

# Configuration from environment variables
CAMERA_IP = os.environ.get('CAMERA_IP')
RTSP_PORT = int(os.environ.get('RTSP_PORT', 554))
CAMERA_USER = os.environ.get('CAMERA_USER')
CAMERA_PASS = os.environ.get('CAMERA_PASS')
HTTP_HOST = os.environ.get('HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.environ.get('HTTP_PORT', 8080))
RTSP_STREAM_PATH = os.environ.get('RTSP_STREAM_PATH', '/Streaming/Channels/101')
SNAPSHOT_PATH = os.environ.get('SNAPSHOT_PATH', '/ISAPI/Streaming/channels/101/picture')
ONVIF_PORT = int(os.environ.get('ONVIF_PORT', 80))  # Used for snapshot and status

# RTSP stream control
streaming_enabled = False
stream_lock = threading.Lock()

app = Flask(__name__)

def get_rtsp_url():
    userpass = f"{CAMERA_USER}:{CAMERA_PASS}@" if CAMERA_USER and CAMERA_PASS else ""
    return f"rtsp://{userpass}{CAMERA_IP}:{RTSP_PORT}{RTSP_STREAM_PATH}"

def get_snapshot_url():
    userpass = f"{CAMERA_USER}:{CAMERA_PASS}@" if CAMERA_USER and CAMERA_PASS else ""
    return f"http://{CAMERA_IP}:{ONVIF_PORT}{SNAPSHOT_PATH}"

def gen_mjpeg():
    global streaming_enabled
    cap = None
    try:
        cap = cv2.VideoCapture(get_rtsp_url())
        if not cap.isOpened():
            yield b''
            return
        while True:
            with stream_lock:
                if not streaming_enabled:
                    break
            ret, frame = cap.read()
            if not ret:
                break
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            frame_bytes = jpeg.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            time.sleep(0.04)  # ~25 FPS
    finally:
        if cap:
            cap.release()

@app.route('/start', methods=['POST'])
def start_stream():
    global streaming_enabled
    with stream_lock:
        streaming_enabled = True
    return jsonify({"status": "streaming started"}), 200

@app.route('/stop', methods=['POST'])
def stop_stream():
    global streaming_enabled
    with stream_lock:
        streaming_enabled = False
    return jsonify({"status": "streaming stopped"}), 200

@app.route('/snap', methods=['POST'])
def snap():
    # Try HTTP snapshot API first
    try:
        url = get_snapshot_url()
        resp = requests.get(url, auth=(CAMERA_USER, CAMERA_PASS) if CAMERA_USER and CAMERA_PASS else None, timeout=3)
        if resp.status_code == 200 and resp.headers.get('Content-Type', '').startswith('image/jpeg'):
            return Response(resp.content, mimetype='image/jpeg')
    except Exception:
        pass
    # Fallback: grab a frame from RTSP stream
    cap = cv2.VideoCapture(get_rtsp_url())
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return abort(500, description='Unable to capture snapshot')
    ret, jpeg = cv2.imencode('.jpg', frame)
    if not ret:
        return abort(500, description='JPEG encoding error')
    return Response(jpeg.tobytes(), mimetype='image/jpeg')

@app.route('/live', methods=['GET'])
def live():
    global streaming_enabled
    with stream_lock:
        if not streaming_enabled:
            return abort(403, description='Stream not started. Use /start to enable streaming.')
    return Response(gen_mjpeg(), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)
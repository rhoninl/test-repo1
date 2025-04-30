import os
import threading
import io
import time
import cv2
import requests
from flask import Flask, Response, jsonify, send_file, request, abort

# Configuration via environment variables
CAMERA_IP = os.environ.get('CAMERA_IP', '192.168.1.64')
CAMERA_USER = os.environ.get('CAMERA_USER', 'admin')
CAMERA_PASS = os.environ.get('CAMERA_PASS', '12345')
SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', '8080'))
RTSP_PORT = int(os.environ.get('RTSP_PORT', '554'))
HTTP_PORT = int(os.environ.get('HTTP_PORT', '80'))
CHANNEL = os.environ.get('CAMERA_CHANNEL', '101')  # default main stream
RTSP_PATH = os.environ.get('RTSP_PATH', f'ISAPI/Streaming/channels/{CHANNEL}')

# Video stream state
streaming_active = threading.Event()
stream_lock = threading.Lock()

def get_rtsp_url():
    user = CAMERA_USER
    pw = CAMERA_PASS
    host = CAMERA_IP
    port = RTSP_PORT
    channel = CHANNEL
    return f'rtsp://{user}:{pw}@{host}:{port}/Streaming/Channels/{channel}'

def get_snapshot_url():
    # Hikvision snapshot API
    host = CAMERA_IP
    port = HTTP_PORT
    user = CAMERA_USER
    pw = CAMERA_PASS
    channel = CHANNEL
    return f'http://{host}:{port}/ISAPI/Streaming/channels/{channel}/picture'

def check_camera_status():
    url = f'http://{CAMERA_IP}:{HTTP_PORT}/ISAPI/System/status'
    try:
        resp = requests.get(url, auth=(CAMERA_USER, CAMERA_PASS), timeout=5)
        if resp.status_code == 200:
            return {"status": "online", "details": resp.text}
        else:
            return {"status": "offline", "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"status": "offline", "error": str(e)}

# MJPEG generator using OpenCV
def mjpeg_stream():
    rtsp_url = get_rtsp_url()
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        yield b''
        return
    streaming_active.set()
    try:
        while streaming_active.is_set():
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            frame_bytes = jpeg.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
    finally:
        cap.release()
        streaming_active.clear()

# Flask app
app = Flask(__name__)

@app.route('/video/start', methods=['POST'])
def start_stream():
    with stream_lock:
        streaming_active.set()
    return jsonify({"message": "Video stream started."}), 200

@app.route('/video/stop', methods=['POST'])
def stop_stream():
    with stream_lock:
        streaming_active.clear()
    return jsonify({"message": "Video stream stopped."}), 200

@app.route('/video/url', methods=['GET'])
def get_rtsp_url_api():
    url = get_rtsp_url()
    return jsonify({"rtsp_url": url})

@app.route('/camera/status', methods=['GET'])
def camera_status():
    status = check_camera_status()
    return jsonify(status)

@app.route('/snapshot', methods=['GET'])
def snapshot():
    url = get_snapshot_url()
    try:
        resp = requests.get(url, auth=(CAMERA_USER, CAMERA_PASS), timeout=5, stream=True)
        if resp.status_code == 200:
            return Response(resp.content, mimetype='image/jpeg')
        else:
            # fallback: try to grab a frame from RTSP
            cap = cv2.VideoCapture(get_rtsp_url())
            ret, frame = cap.read()
            cap.release()
            if ret:
                _, jpeg = cv2.imencode('.jpg', frame)
                return Response(jpeg.tobytes(), mimetype='image/jpeg')
            else:
                return jsonify({"error": "Failed to capture snapshot"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/video/stream', methods=['GET'])
def video_stream():
    if not streaming_active.is_set():
        streaming_active.set()
    return Response(mjpeg_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
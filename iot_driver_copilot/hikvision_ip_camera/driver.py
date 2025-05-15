import os
import threading
import signal
from flask import Flask, Response, jsonify, request
import cv2

# Environment Variables
DEVICE_IP = os.environ.get("DEVICE_IP")
DEVICE_USER = os.environ.get("DEVICE_USER", "")
DEVICE_PASS = os.environ.get("DEVICE_PASS", "")
RTSP_PORT = os.environ.get("RTSP_PORT", "554")
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))

# RTSP URL Construction
if DEVICE_USER and DEVICE_PASS:
    RTSP_URL = f"rtsp://{DEVICE_USER}:{DEVICE_PASS}@{DEVICE_IP}:{RTSP_PORT}/Streaming/Channels/101"
else:
    RTSP_URL = f"rtsp://{DEVICE_IP}:{RTSP_PORT}/Streaming/Channels/101"

app = Flask(__name__)

streaming_active = threading.Event()
stream_capture = None
stream_lock = threading.Lock()

def gen_frames():
    global stream_capture
    while streaming_active.is_set():
        with stream_lock:
            if stream_capture is not None:
                ret, frame = stream_capture.read()
                if not ret:
                    break
                # JPEG encode
                ret, buffer = cv2.imencode('.jpg', frame)
                if not ret:
                    continue
                frame_bytes = buffer.tobytes()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            else:
                break

@app.route('/start', methods=['POST'])
def start_stream():
    global stream_capture
    if streaming_active.is_set():
        return jsonify({"status": "already streaming"}), 200
    with stream_lock:
        stream_capture = cv2.VideoCapture(RTSP_URL)
        if not stream_capture.isOpened():
            stream_capture.release()
            stream_capture = None
            return jsonify({"status": "error", "message": "Failed to open RTSP stream"}), 500
        streaming_active.set()
    return jsonify({"status": "streaming started"}), 200

@app.route('/stop', methods=['POST'])
def stop_stream():
    global stream_capture
    if not streaming_active.is_set():
        return jsonify({"status": "already stopped"}), 200
    streaming_active.clear()
    with stream_lock:
        if stream_capture is not None:
            stream_capture.release()
            stream_capture = None
    return jsonify({"status": "streaming stopped"}), 200

@app.route('/video')
def video_feed():
    if not streaming_active.is_set():
        return jsonify({"status": "not streaming"}), 400
    return Response(gen_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

def cleanup(*args):
    global stream_capture
    streaming_active.clear()
    with stream_lock:
        if stream_capture is not None:
            stream_capture.release()
            stream_capture = None
    os._exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
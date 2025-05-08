import os
import io
import threading
import time
from flask import Flask, Response, jsonify, request, abort
import requests
import cv2
import numpy as np
from requests.auth import HTTPDigestAuth

# Configuration from environment variables
DEVICE_IP = os.environ.get('DEVICE_IP')
DEVICE_RTSP_PORT = int(os.environ.get('DEVICE_RTSP_PORT', 554))
DEVICE_ONVIF_PORT = int(os.environ.get('DEVICE_ONVIF_PORT', 80))
DEVICE_USER = os.environ.get('DEVICE_USER')
DEVICE_PASS = os.environ.get('DEVICE_PASS')
SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', 8080))
RTSP_PATH = os.environ.get('RTSP_PATH', '/Streaming/Channels/101')
SNAPSHOT_PATH = os.environ.get('SNAPSHOT_PATH', '/ISAPI/Streaming/channels/101/picture')
PTZ_PATH = os.environ.get('PTZ_PATH', '/ISAPI/PTZCtrl/channels/1/continuous')

# Global session state
g_rtsp_active = False
g_rtsp_thread = None
g_rtsp_lock = threading.Lock()
g_last_frame = None
g_stream_stop = threading.Event()

app = Flask(__name__)

# Utilities

def get_rtsp_url():
    return f'rtsp://{DEVICE_USER}:{DEVICE_PASS}@{DEVICE_IP}:{DEVICE_RTSP_PORT}{RTSP_PATH}'

def get_snapshot_url():
    return f'http://{DEVICE_IP}:{DEVICE_ONVIF_PORT}{SNAPSHOT_PATH}'

def get_ptz_url():
    return f'http://{DEVICE_IP}:{DEVICE_ONVIF_PORT}{PTZ_PATH}'

def grab_video_frames():
    global g_last_frame
    cap = cv2.VideoCapture(get_rtsp_url())
    while not g_stream_stop.is_set() and cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue
        _, jpeg = cv2.imencode('.jpg', frame)
        g_last_frame = jpeg.tobytes()
    cap.release()

def start_rtsp_stream():
    global g_rtsp_active, g_rtsp_thread, g_stream_stop
    with g_rtsp_lock:
        if not g_rtsp_active:
            g_stream_stop.clear()
            g_rtsp_thread = threading.Thread(target=grab_video_frames, daemon=True)
            g_rtsp_active = True
            g_rtsp_thread.start()

def stop_rtsp_stream():
    global g_rtsp_active, g_rtsp_thread, g_stream_stop
    with g_rtsp_lock:
        if g_rtsp_active:
            g_stream_stop.set()
            if g_rtsp_thread is not None:
                g_rtsp_thread.join(timeout=2)
            g_rtsp_active = False
            g_rtsp_thread = None

# API Endpoints

@app.route('/camera/info', methods=['GET'])
@app.route('/status', methods=['GET'])
def camera_info():
    # Online check
    try:
        url = f'http://{DEVICE_IP}:{DEVICE_ONVIF_PORT}/ISAPI/System/status'
        resp = requests.get(url, auth=HTTPDigestAuth(DEVICE_USER, DEVICE_PASS), timeout=3)
        online = resp.status_code == 200
        status_json = resp.json() if online else {}
    except Exception:
        online = False
        status_json = {}
    # Stream status
    info = {
        'online': online,
        'stream_active': g_rtsp_active,
        'last_frame': g_last_frame is not None,
        'device_ip': DEVICE_IP,
        'recording': status_json.get('recording', None),
        'alarms': status_json.get('alarms', []),
        'errors': status_json.get('errors', []),
    }
    return jsonify(info), 200

@app.route('/camera/live/on', methods=['POST'])
def camera_live_on():
    start_rtsp_stream()
    return jsonify({'success': True, 'message': 'Live feed activated.'})

@app.route('/camera/live/off', methods=['POST'])
def camera_live_off():
    stop_rtsp_stream()
    return jsonify({'success': True, 'message': 'Live feed deactivated.'})

@app.route('/stream/start', methods=['POST'])
def stream_start():
    start_rtsp_stream()
    return jsonify({'success': True, 'message': 'RTSP streaming started.'})

@app.route('/stream/stop', methods=['POST'])
def stream_stop():
    stop_rtsp_stream()
    return jsonify({'success': True, 'message': 'RTSP streaming stopped.'})

@app.route('/camera/live-feed', methods=['GET'])
def camera_live_feed():
    if not g_rtsp_active:
        abort(409, 'Live feed not active. Call /camera/live/on first.')

    def mjpeg_stream():
        global g_last_frame
        while g_rtsp_active:
            if g_last_frame is not None:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + g_last_frame + b'\r\n')
                time.sleep(0.05)
            else:
                time.sleep(0.1)
    return Response(mjpeg_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/camera/photo', methods=['GET'])
@app.route('/snapshot', methods=['GET'])
def camera_photo():
    # Try ISAPI snapshot (HTTP JPEG)
    try:
        url = get_snapshot_url()
        resp = requests.get(url, auth=HTTPDigestAuth(DEVICE_USER, DEVICE_PASS), timeout=5)
        if resp.status_code == 200 and resp.headers.get('Content-Type', '').startswith('image'):
            return Response(resp.content, mimetype='image/jpeg')
    except Exception:
        pass
    # Fallback to RTSP frame grab if available
    if g_last_frame is not None:
        return Response(g_last_frame, mimetype='image/jpeg')
    abort(503, 'Snapshot unavailable')

@app.route('/ptz/control', methods=['POST'])
@app.route('/camera/ptz', methods=['POST'])
def ptz_control():
    data = request.get_json(force=True)
    pan = data.get('pan')
    tilt = data.get('tilt')
    zoom = data.get('zoom')
    action = data.get('action')
    speed = data.get('speed', 1)

    # Build ISAPI PTZ command
    ptz_xml = '<?xml version="1.0" encoding="UTF-8"?><PTZData>'
    if action:
        if action == 'left':
            ptz_xml += f'<pan>{-abs(speed)}</pan><tilt>0</tilt><zoom>0</zoom>'
        elif action == 'right':
            ptz_xml += f'<pan>{abs(speed)}</pan><tilt>0</tilt><zoom>0</zoom>'
        elif action == 'up':
            ptz_xml += f'<pan>0</pan><tilt>{abs(speed)}</tilt><zoom>0</zoom>'
        elif action == 'down':
            ptz_xml += f'<pan>0</pan><tilt>{-abs(speed)}</tilt><zoom>0</zoom>'
        elif action == 'zoom_in':
            ptz_xml += f'<pan>0</pan><tilt>0</tilt><zoom>{abs(speed)}</zoom>'
        elif action == 'zoom_out':
            ptz_xml += f'<pan>0</pan><tilt>0</tilt><zoom>{-abs(speed)}</zoom>'
    else:
        ptz_xml += f'<pan>{pan or 0}</pan><tilt>{tilt or 0}</tilt><zoom>{zoom or 0}</zoom>'
    ptz_xml += '</PTZData>'

    url = get_ptz_url()
    headers = {'Content-Type': 'application/xml'}
    try:
        resp = requests.put(url, data=ptz_xml, headers=headers, auth=HTTPDigestAuth(DEVICE_USER, DEVICE_PASS), timeout=3)
        if resp.status_code in (200, 201, 202, 204):
            return jsonify({'success': True, 'message': 'PTZ command sent.'})
        else:
            return jsonify({'success': False, 'error': resp.text}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
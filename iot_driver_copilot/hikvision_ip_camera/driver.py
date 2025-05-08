import os
import threading
import time
from io import BytesIO

from flask import Flask, Response, jsonify, request, abort

import requests
import cv2
import numpy as np
from requests.auth import HTTPDigestAuth

try:
    from onvif import ONVIFCamera
except ImportError:
    ONVIFCamera = None  # PTZ will not be available without onvif

# --- Environment Variables ---
DEVICE_IP = os.environ.get("DEVICE_IP", "192.168.1.64")
DEVICE_RTSP_PORT = int(os.environ.get("DEVICE_RTSP_PORT", "554"))
DEVICE_HTTP_PORT = int(os.environ.get("DEVICE_HTTP_PORT", "80"))
DEVICE_ONVIF_PORT = int(os.environ.get("DEVICE_ONVIF_PORT", "80"))
DEVICE_USER = os.environ.get("DEVICE_USER", "admin")
DEVICE_PASS = os.environ.get("DEVICE_PASS", "12345")
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))
RTSP_PATH = os.environ.get("RTSP_PATH", "/Streaming/Channels/101")
ONVIF_WSDL_DIR = os.environ.get("ONVIF_WSDL_DIR", "/etc/onvif/wsdl")
RTSP_TRANSPORT = os.environ.get("RTSP_TRANSPORT", "tcp")  # tcp or udp

# --- Globals ---
app = Flask(__name__)
camera_stream_thread = None
stream_active = threading.Event()
frame_lock = threading.Lock()
latest_frame = None
rtsp_capture = None
capture_thread_should_run = False

def get_rtsp_url():
    user = DEVICE_USER
    pwd = DEVICE_PASS
    ip = DEVICE_IP
    port = DEVICE_RTSP_PORT
    path = RTSP_PATH.lstrip("/")
    return f"rtsp://{user}:{pwd}@{ip}:{port}/{path}"

def get_snapshot_url():
    return f"http://{DEVICE_IP}:{DEVICE_HTTP_PORT}/ISAPI/Streaming/channels/101/picture"

def get_status_url():
    return f"http://{DEVICE_IP}:{DEVICE_HTTP_PORT}/ISAPI/System/status"

def get_recording_url():
    return f"http://{DEVICE_IP}:{DEVICE_HTTP_PORT}/ISAPI/ContentMgmt/record/tracks"

def get_alarm_url():
    return f"http://{DEVICE_IP}:{DEVICE_HTTP_PORT}/ISAPI/Event/notification/alertStream"

def get_onvif_camera():
    if ONVIFCamera is None:
        return None
    return ONVIFCamera(DEVICE_IP, DEVICE_ONVIF_PORT, DEVICE_USER, DEVICE_PASS, wsdl_dir=ONVIF_WSDL_DIR)

# --- Camera Stream Thread ---
def camera_stream_worker():
    global latest_frame, rtsp_capture, capture_thread_should_run
    capture_thread_should_run = True
    cap = cv2.VideoCapture(get_rtsp_url(), cv2.CAP_FFMPEG)
    if not cap.isOpened():
        rtsp_capture = None
        return
    rtsp_capture = cap
    while stream_active.is_set() and capture_thread_should_run:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.1)
            continue
        with frame_lock:
            latest_frame = frame
        # Limit FPS to ~15
        time.sleep(0.066)
    cap.release()
    rtsp_capture = None

def start_camera_stream():
    global camera_stream_thread
    if stream_active.is_set():
        return True
    stream_active.set()
    camera_stream_thread = threading.Thread(target=camera_stream_worker, daemon=True)
    camera_stream_thread.start()
    # Wait for the first frame
    for _ in range(30):
        time.sleep(0.1)
        with frame_lock:
            if latest_frame is not None:
                return True
    return False

def stop_camera_stream():
    global capture_thread_should_run
    stream_active.clear()
    capture_thread_should_run = False
    # Wait for thread to finish
    if camera_stream_thread is not None:
        camera_stream_thread.join(timeout=2)
    with frame_lock:
        global latest_frame
        latest_frame = None

# --- MJPEG Streaming ---
def mjpeg_stream_generator():
    global latest_frame
    while stream_active.is_set():
        with frame_lock:
            frame = latest_frame.copy() if latest_frame is not None else None
        if frame is not None:
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            jpg_bytes = jpeg.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpg_bytes + b'\r\n')
        else:
            time.sleep(0.05)

# --- API ROUTES ---
@app.route("/camera/live/on", methods=["POST"])
def api_camera_live_on():
    ok = start_camera_stream()
    if ok:
        return jsonify({"success": True, "message": "Live stream started"}), 200
    else:
        return jsonify({"success": False, "message": "Could not start live stream"}), 500

@app.route("/camera/live/off", methods=["POST"])
def api_camera_live_off():
    stop_camera_stream()
    return jsonify({"success": True, "message": "Live stream stopped"}), 200

@app.route("/stream/start", methods=["POST"])
def api_stream_start():
    ok = start_camera_stream()
    if ok:
        return jsonify({"success": True, "message": "RTSP stream started"}), 200
    else:
        return jsonify({"success": False, "message": "Could not start RTSP stream"}), 500

@app.route("/stream/stop", methods=["POST"])
def api_stream_stop():
    stop_camera_stream()
    return jsonify({"success": True, "message": "RTSP stream stopped"}), 200

@app.route("/camera/live-feed", methods=["GET"])
def api_camera_live_feed():
    if not stream_active.is_set():
        return abort(400, "Live feed not active. POST /camera/live/on first.")
    return Response(mjpeg_stream_generator(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/camera/photo", methods=["GET"])
@app.route("/snapshot", methods=["GET"])
def api_camera_snapshot():
    # Try to get a fresh frame from RTSP first if live, otherwise use HTTP snapshot
    global latest_frame
    if stream_active.is_set() and latest_frame is not None:
        with frame_lock:
            frame = latest_frame.copy()
        ret, jpeg = cv2.imencode('.jpg', frame)
        if ret:
            return Response(jpeg.tobytes(), mimetype='image/jpeg')
    # Fallback to HTTP snapshot
    url = get_snapshot_url()
    try:
        r = requests.get(url, auth=HTTPDigestAuth(DEVICE_USER, DEVICE_PASS), timeout=5)
        if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image/jpeg"):
            return Response(r.content, mimetype='image/jpeg')
        else:
            return abort(500, "Snapshot fetch failed")
    except Exception as e:
        return abort(500, str(e))

@app.route("/camera/info", methods=["GET"])
@app.route("/status", methods=["GET"])
def api_camera_info():
    # Fetch status, recording state, alarms
    status_url = get_status_url()
    recording_url = get_recording_url()
    alarm_url = get_alarm_url()
    info = {}
    try:
        sresp = requests.get(status_url, auth=HTTPDigestAuth(DEVICE_USER, DEVICE_PASS), timeout=5)
        if sresp.status_code == 200:
            info['status'] = sresp.json() if 'application/json' in sresp.headers.get("Content-Type", "") else sresp.text
        else:
            info['status'] = "unavailable"
    except Exception as e:
        info['status'] = str(e)
    try:
        rresp = requests.get(recording_url, auth=HTTPDigestAuth(DEVICE_USER, DEVICE_PASS), timeout=5)
        info['recording'] = rresp.json() if rresp.status_code == 200 else "unavailable"
    except Exception as e:
        info['recording'] = str(e)
    try:
        aresp = requests.get(alarm_url, auth=HTTPDigestAuth(DEVICE_USER, DEVICE_PASS), timeout=5, stream=True)
        if aresp.status_code == 200:
            info['alarm'] = "active"
        else:
            info['alarm'] = "inactive"
    except Exception as e:
        info['alarm'] = str(e)
    info['online'] = True if info['status'] != "unavailable" else False
    return jsonify(info)

@app.route("/ptz/control", methods=["POST"])
@app.route("/camera/ptz", methods=["POST"])
def api_ptz_control():
    if ONVIFCamera is None:
        return abort(501, "ONVIF PTZ not available (missing onvif package)")
    data = request.get_json(force=True)
    cam = get_onvif_camera()
    media_service = cam.create_media_service()
    profiles = media_service.GetProfiles()
    profile = profiles[0]
    ptz_service = cam.create_ptz_service()
    req = ptz_service.create_type('ContinuousMove')
    req.ProfileToken = profile.token
    req.Velocity = {'PanTilt': {}, 'Zoom': {}}
    # Accept both {pan, tilt, zoom} and {action, speed}
    if 'pan' in data or 'tilt' in data or 'zoom' in data:
        req.Velocity['PanTilt']['x'] = float(data.get('pan', 0)) / 100
        req.Velocity['PanTilt']['y'] = float(data.get('tilt', 0)) / 100
        req.Velocity['Zoom']['x'] = float(data.get('zoom', 0)) / 10
    elif 'action' in data:
        speed = float(data.get('speed', 1)) / 10
        action = data['action'].lower()
        if action == "left":
            req.Velocity['PanTilt']['x'] = -speed
        elif action == "right":
            req.Velocity['PanTilt']['x'] = speed
        elif action == "up":
            req.Velocity['PanTilt']['y'] = speed
        elif action == "down":
            req.Velocity['PanTilt']['y'] = -speed
        elif action == "zoom_in":
            req.Velocity['Zoom']['x'] = speed
        elif action == "zoom_out":
            req.Velocity['Zoom']['x'] = -speed
    else:
        return abort(400, "Invalid PTZ command")
    try:
        ptz_service.ContinuousMove(req)
        return jsonify({"success": True, "message": "PTZ command executed"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
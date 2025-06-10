// DeviceShifu for Hikvision IP Camera (ISAPI v2.0)
// Integrates via HTTP RESTful API, proxies MJPEG stream for browser consumption
// Maintains EdgeDevice .status.edgeDevicePhase CRD in Kubernetes
// Loads API settings from YAML config, uses env vars for all configs

const express = require('express');
const fs = require('fs');
const yaml = require('js-yaml');
const k8s = require('@kubernetes/client-node');
const axios = require('axios');
const http = require('http');
const basicAuth = require('basic-auth');
const { PassThrough } = require('stream');
const { XMLParser } = require('fast-xml-parser');

const CONFIG_DIR = '/etc/edgedevice/config/instructions';

// ENV VARS: Required for operation
const EDGEDEVICE_NAME = process.env.EDGEDEVICE_NAME;
const EDGEDEVICE_NAMESPACE = process.env.EDGEDEVICE_NAMESPACE;
const DEVICE_SHIFU_HTTP_PORT = process.env.DEVICE_SHIFU_HTTP_PORT || '8080';
const DEVICE_IP = process.env.DEVICE_IP;
const DEVICE_USERNAME = process.env.DEVICE_USERNAME;
const DEVICE_PASSWORD = process.env.DEVICE_PASSWORD;
const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';

if (!EDGEDEVICE_NAME || !EDGEDEVICE_NAMESPACE || !DEVICE_IP || !DEVICE_USERNAME || !DEVICE_PASSWORD) {
    console.error('Missing required environment variables');
    process.exit(1);
}

// API config loader
function loadApiSettings() {
    let config = {};
    try {
        const files = fs.readdirSync(CONFIG_DIR);
        files.forEach(f => {
            const full = `${CONFIG_DIR}/${f}`;
            if (fs.statSync(full).isFile()) {
                const doc = yaml.load(fs.readFileSync(full, 'utf8'));
                config = { ...config, ...doc };
            }
        });
    } catch (e) {
        // Ignore missing config, use defaults
    }
    return config;
}
const apiSettings = loadApiSettings();

// K8s client setup (in-cluster)
const kc = new k8s.KubeConfig();
kc.loadFromCluster();
const k8sApiCustom = kc.makeApiClient(k8s.CustomObjectsApi);

// EdgeDevice CRD updater
const EDGEDEVICE_GROUP = 'shifu.edgenesis.io';
const EDGEDEVICE_VERSION = 'v1alpha1';
const EDGEDEVICE_PLURAL = 'edgedevices';

// Device connection state
let devicePhase = 'Unknown';

// Utility: Update devicePhase in EdgeDevice CRD
async function updateEdgeDevicePhase(phase) {
    if (devicePhase === phase) return;
    devicePhase = phase;
    try {
        const patch = [
            {
                op: 'replace',
                path: '/status/edgeDevicePhase',
                value: phase
            }
        ];
        await k8sApiCustom.patchNamespacedCustomObjectStatus(
            EDGEDEVICE_GROUP, EDGEDEVICE_VERSION, EDGEDEVICE_NAMESPACE,
            EDGEDEVICE_PLURAL, EDGEDEVICE_NAME,
            patch,
            undefined, undefined, undefined,
            {headers: {'Content-Type': 'application/json-patch+json'}}
        );
    } catch (err) {
        // Could be first time, if no status, try to add instead
        try {
            const fullObj = await k8sApiCustom.getNamespacedCustomObject(
                EDGEDEVICE_GROUP, EDGEDEVICE_VERSION, EDGEDEVICE_NAMESPACE,
                EDGEDEVICE_PLURAL, EDGEDEVICE_NAME
            );
            if (!fullObj.body.status) {
                const patch = [
                    {
                        op: 'add',
                        path: '/status',
                        value: { edgeDevicePhase: phase }
                    }
                ];
                await k8sApiCustom.patchNamespacedCustomObjectStatus(
                    EDGEDEVICE_GROUP, EDGEDEVICE_VERSION, EDGEDEVICE_NAMESPACE,
                    EDGEDEVICE_PLURAL, EDGEDEVICE_NAME,
                    patch,
                    undefined, undefined, undefined,
                    {headers: {'Content-Type': 'application/json-patch+json'}}
                );
            }
        } catch (e2) {
            // Ignore
        }
    }
}

// Utility: Basic auth string
function getAuthHeader() {
    return 'Basic ' + Buffer.from(`${DEVICE_USERNAME}:${DEVICE_PASSWORD}`).toString('base64');
}

// Device status checker (sets devicePhase)
async function checkDeviceConnection() {
    try {
        // Try basic isapi/system/deviceinfo API
        const url = `http://${DEVICE_IP}/ISAPI/System/deviceInfo`;
        const res = await axios.get(url, {
            timeout: 3000,
            headers: {Authorization: getAuthHeader()}
        });
        if (res.status === 200) {
            await updateEdgeDevicePhase('Running');
            return true;
        } else {
            await updateEdgeDevicePhase('Failed');
            return false;
        }
    } catch (e) {
        await updateEdgeDevicePhase('Failed');
        return false;
    }
}

// Periodic device status check
setInterval(() => {
    checkDeviceConnection();
}, 10000);

// Initial status: Pending
updateEdgeDevicePhase('Pending');

// Express server
const app = express();
app.use(express.json());

// --- API: /ptz/control (POST) ---
app.post('/ptz/control', async (req, res) => {
    // {channel:int, action:string, speed:int}
    const { channel, action, speed } = req.body || {};
    const validActions = ['up','down','left','right','zoomIn','zoomOut'];
    if (!channel || !action || !validActions.includes(action) || !speed) {
        return res.status(400).send('Invalid PTZ params');
    }
    // Hikvision ISAPI PTZ
    // Command mapping
    const ptzActionMap = {
        up: 'tiltUp',
        down: 'tiltDown',
        left: 'panLeft',
        right: 'panRight',
        zoomIn: 'zoomIn',
        zoomOut: 'zoomOut'
    };
    const cmd = ptzActionMap[action];
    // Build ISAPI XML
    const xml = `<PTZData>
        <command>${cmd}</command>
        <speed>${Math.max(1, Math.min(speed, 10))}</speed>
    </PTZData>`;
    try {
        const url = `http://${DEVICE_IP}/ISAPI/PTZCtrl/channels/${channel}/continuous`;
        const rv = await axios.put(
            url,
            xml,
            {
                headers: {
                    'Authorization': getAuthHeader(),
                    'Content-Type': 'application/xml'
                }
            }
        );
        if (rv.status === 200 || rv.status === 204) {
            res.status(204).send();
        } else {
            res.status(502).send('PTZ command failed');
        }
    } catch (e) {
        res.status(502).send('PTZ command error');
    }
});

// --- API: /stream/mjpeg (GET): HTTP MJPEG streaming for browser ---
app.get('/stream/mjpeg', async (req, res) => {
    // Query: channel, resolution
    let channel = parseInt(req.query.channel) || 1;
    let resolution = req.query.resolution || '720p';

    // Hikvision ISAPI MJPEG: /ISAPI/Streaming/channels/101/httpPreview  (101 for channel 1 main)
    // MJPEG: /ISAPI/Streaming/channels/{id}/httpPreview?videoCodecType=MJPEG
    // 101: channel 1 main, 102: channel 1 sub, 201: channel 2 main, etc.
    let chId = (channel * 100) + 1; // default main stream
    let url = `http://${DEVICE_IP}/ISAPI/Streaming/channels/${chId}/httpPreview?videoCodecType=MJPEG`;
    // Optionally, can use resolution if device supports
    try {
        // Proxy MJPEG multipart stream to client
        const response = await axios.get(url, {
            responseType: 'stream',
            headers: { Authorization: getAuthHeader() }
        });
        res.setHeader('Content-Type', 'multipart/x-mixed-replace; boundary=--boundarydonotcross');
        response.data.pipe(res);
    } catch (e) {
        res.status(502).send('Stream not available');
    }
});

// --- API: /snapshot (GET): JPEG ---
app.get('/snapshot', async (req, res) => {
    let channel = parseInt(req.query.channel) || 1;
    let chId = (channel * 100) + 1;
    let url = `http://${DEVICE_IP}/ISAPI/Streaming/channels/${chId}/picture`;
    try {
        const response = await axios.get(url, {
            headers: { Authorization: getAuthHeader() },
            responseType: 'arraybuffer'
        });
        res.setHeader('Content-Type', 'image/jpeg');
        res.send(response.data);
    } catch (e) {
        res.status(502).send('Snapshot not available');
    }
});

// --- API: /stream/url (GET): Get RTSP/HTTP video stream URL (returns JSON) ---
app.get('/stream/url', async (req, res) => {
    // Returns a URI, but does NOT simply forward -- this is for clients who need RTSP/HTTP direct
    // Query: channel, codec
    let channel = parseInt(req.query.channel) || 1;
    let codec = req.query.codec || 'h264';
    let chId = (channel * 100) + 1;
    // Only allow valid codecs
    const validCodecs = ['h264', 'h265', 'mpeg4', 'mjpeg'];
    if (!validCodecs.includes(codec)) return res.status(400).json({error: 'Invalid codec'});
    // Hikvision RTSP: rtsp://user:pass@ip:554/Streaming/Channels/101?videoCodecType=H.264
    let userinfo = `${DEVICE_USERNAME}:${DEVICE_PASSWORD}`;
    let rtspUrl = `rtsp://${userinfo}@${DEVICE_IP}:554/Streaming/Channels/${chId}?videoCodecType=${codec.toUpperCase()}`;
    // For HTTP stream (MJPEG), use /ISAPI/Streaming/channels/101/httpPreview?videoCodecType=MJPEG
    let httpUrl = `http://${DEVICE_IP}/ISAPI/Streaming/channels/${chId}/httpPreview?videoCodecType=${codec.toUpperCase()}`;
    res.json({
        url: codec === 'mjpeg' ? httpUrl : rtspUrl
    });
});

// --- API: /device/reboot (POST): Reboot camera ---
app.post('/device/reboot', async (req, res) => {
    let url = `http://${DEVICE_IP}/ISAPI/System/reboot`;
    try {
        const response = await axios.put(url, '', {
            headers: {
                Authorization: getAuthHeader(),
                'Content-Type': 'application/xml'
            }
        });
        if (response.status === 200 || response.status === 202) {
            res.status(202).send('Reboot initiated');
        } else {
            res.status(502).send('Failed to initiate reboot');
        }
    } catch (e) {
        res.status(502).send('Reboot error');
    }
});

// --- API: /device/info (GET): Get camera info ---
app.get('/device/info', async (req, res) => {
    const url = `http://${DEVICE_IP}/ISAPI/System/deviceInfo`;
    try {
        const response = await axios.get(url, {
            headers: { Authorization: getAuthHeader() }
        });
        // Convert XML to JSON if possible
        const parser = new XMLParser({ ignoreAttributes: false });
        const json = parser.parse(response.data);
        res.json(json || {});
    } catch (e) {
        res.status(502).send('Device info unavailable');
    }
});

// HTTP server
const server = http.createServer(app);
server.listen(parseInt(DEVICE_SHIFU_HTTP_PORT), SERVER_HOST, () => {
    console.log(`DeviceShifu HTTP server running at http://${SERVER_HOST}:${DEVICE_SHIFU_HTTP_PORT}/`);
});
// DeviceShifu for Hikvision IP Camera - HTTP Server (Node.js)
// CRD: shifu.edgenesis.io/v1alpha1 EdgeDevice
// APIs: /snapshot, /streams, /streams/:streamId/preview

const express = require('express');
const fs = require('fs');
const yaml = require('js-yaml');
const k8s = require('@kubernetes/client-node');
const axios = require('axios');
const http = require('http');
const https = require('https');
const { PassThrough } = require('stream');

// Environment Variables (required)
const {
    EDGEDEVICE_NAME,
    EDGEDEVICE_NAMESPACE,
    DEVICE_HTTP_PORT = '8080',
    DEVICE_SHIFU_HOST = '0.0.0.0',
    KUBERNETES_SERVICE_HOST,
    KUBERNETES_SERVICE_PORT,
    DEVICE_USERNAME,
    DEVICE_PASSWORD,
    NODE_TLS_REJECT_UNAUTHORIZED = '1'
} = process.env;

if (!EDGEDEVICE_NAME || !EDGEDEVICE_NAMESPACE) {
    console.error('EDGEDEVICE_NAME and EDGEDEVICE_NAMESPACE must be set!');
    process.exit(1);
}

process.env.NODE_TLS_REJECT_UNAUTHORIZED = NODE_TLS_REJECT_UNAUTHORIZED;

// ---- Kubernetes client setup ----
const kc = new k8s.KubeConfig();
if (process.env.KUBERNETES_SERVICE_HOST) {
    // In-cluster config
    kc.loadFromCluster();
} else {
    kc.loadFromDefault();
}
const k8sApi = kc.makeApiClient(k8s.CustomObjectsApi);

const CRD_GROUP = 'shifu.edgenesis.io';
const CRD_VERSION = 'v1alpha1';
const CRD_PLURAL = 'edgedevices';

async function getEdgeDevice() {
    return k8sApi.getNamespacedCustomObject(
        CRD_GROUP,
        CRD_VERSION,
        EDGEDEVICE_NAMESPACE,
        CRD_PLURAL,
        EDGEDEVICE_NAME
    );
}

async function updateEdgeDevicePhase(phase) {
    try {
        await k8sApi.patchNamespacedCustomObjectStatus(
            CRD_GROUP,
            CRD_VERSION,
            EDGEDEVICE_NAMESPACE,
            CRD_PLURAL,
            EDGEDEVICE_NAME,
            { status: { edgeDevicePhase: phase } },
            undefined,
            undefined,
            undefined,
            { headers: { 'Content-Type': 'application/merge-patch+json' } }
        );
    } catch (e) {
        // Ignore, will retry
    }
}

// ---- Load per-API Configurations ----
const INSTRUCTIONS_PATH = '/etc/edgedevice/config/instructions';
let apiConfig = {};
try {
    if (fs.existsSync(INSTRUCTIONS_PATH)) {
        apiConfig = yaml.load(fs.readFileSync(INSTRUCTIONS_PATH, 'utf8')) || {};
    }
} catch (e) {
    apiConfig = {};
}

// ---- Device Address and ISAPI endpoints ----
let DEVICE_ADDRESS = '';
let DEVICE_PROTOCOL = 'http';
let DEVICE_PORT = 80;
let IS_HTTPS = false;

async function refreshDeviceAddress() {
    try {
        const dev = await getEdgeDevice();
        const spec = dev.body && dev.body.spec;
        if (spec && spec.address) {
            let addr = spec.address;
            if (/^https?:\/\//i.test(addr)) {
                DEVICE_ADDRESS = addr.replace(/\/$/, '');
                DEVICE_PROTOCOL = addr.startsWith('https') ? 'https' : 'http';
                IS_HTTPS = DEVICE_PROTOCOL === 'https';
                const m = addr.match(/^https?:\/\/([^:/]+)(?::(\d+))?/);
                if (m) {
                    DEVICE_PORT = m[2] ? parseInt(m[2], 10) : (IS_HTTPS ? 443 : 80);
                }
            } else {
                DEVICE_ADDRESS = `${DEVICE_PROTOCOL}://${addr}`;
            }
        }
    } catch (e) {
        // ignore
    }
}

// ---- Device connectivity check ----
async function testDeviceConnection() {
    if (!DEVICE_ADDRESS) {
        await refreshDeviceAddress();
    }
    try {
        // Use ISAPI: get device info
        const url = `${DEVICE_ADDRESS}/ISAPI/System/deviceInfo`;
        await axios.get(url, {
            timeout: 3000,
            auth: DEVICE_USERNAME && DEVICE_PASSWORD ? { username: DEVICE_USERNAME, password: DEVICE_PASSWORD } : undefined,
            httpAgent: IS_HTTPS ? undefined : new http.Agent({ keepAlive: true }),
            httpsAgent: IS_HTTPS ? new https.Agent({ rejectUnauthorized: NODE_TLS_REJECT_UNAUTHORIZED === '1' }) : undefined,
            validateStatus: s => s < 500
        });
        return true;
    } catch (e) {
        return false;
    }
}

// ---- Device phase management ----
let devicePhase = 'Pending';
let devicePhaseLast = '';
let deviceCheckTimer = null;
function devicePhaseLoop() {
    setInterval(async () => {
        let newPhase = 'Unknown';
        if (!DEVICE_ADDRESS) {
            await refreshDeviceAddress();
            newPhase = 'Pending';
        }
        if (DEVICE_ADDRESS) {
            const ok = await testDeviceConnection();
            newPhase = ok ? 'Running' : 'Failed';
        }
        devicePhase = newPhase;
        if (devicePhaseLast !== newPhase) {
            devicePhaseLast = newPhase;
            updateEdgeDevicePhase(newPhase);
        }
    }, 5000);
}

// ---- Helper: HTTP Basic Auth ----
function getAuthHeader() {
    if (DEVICE_USERNAME && DEVICE_PASSWORD) {
        const b64 = Buffer.from(`${DEVICE_USERNAME}:${DEVICE_PASSWORD}`).toString('base64');
        return { Authorization: `Basic ${b64}` };
    }
    return {};
}

// ---- API: /snapshot ----
async function getSnapshot(channel = 'main') {
    // Hikvision ISAPI: /ISAPI/Streaming/channels/101/picture
    // channel: 'main' => 101, 'sub' => 102, 'third' => 103
    const channelMap = { main: '101', sub: '102', third: '103' };
    const chId = channelMap[channel] || channelMap.main;
    const url = `${DEVICE_ADDRESS}/ISAPI/Streaming/channels/${chId}/picture`;
    return axios.get(url, {
        responseType: 'stream',
        timeout: 4000,
        headers: getAuthHeader(),
        httpAgent: IS_HTTPS ? undefined : new http.Agent({ keepAlive: true }),
        httpsAgent: IS_HTTPS ? new https.Agent({ rejectUnauthorized: NODE_TLS_REJECT_UNAUTHORIZED === '1' }) : undefined,
        validateStatus: s => s < 500
    });
}

// ---- API: /streams ----
async function getStreams() {
    // Query main/sub/third streams
    const ids = ['101', '102', '103'];
    const names = { '101': 'main', '102': 'sub', '103': 'third' };
    let results = [];
    await Promise.all(
        ids.map(async id => {
            try {
                const url = `${DEVICE_ADDRESS}/ISAPI/Streaming/channels/${id}`;
                const res = await axios.get(url, {
                    timeout: 4000,
                    headers: getAuthHeader(),
                    httpAgent: IS_HTTPS ? undefined : new http.Agent({ keepAlive: true }),
                    httpsAgent: IS_HTTPS ? new https.Agent({ rejectUnauthorized: NODE_TLS_REJECT_UNAUTHORIZED === '1' }) : undefined,
                    validateStatus: s => s < 500
                });
                if (res.status === 200 && res.data) {
                    // Parse XML for stream info (codec, resolution, enable)
                    const xml2js = require('xml2js');
                    const parsed = await xml2js.parseStringPromise(res.data, { explicitArray: false });
                    const ch = parsed && parsed.StreamingChannel;
                    if (ch && ch['enabled'] === 'true') {
                        results.push({
                            id: names[id],
                            streamId: id,
                            codec: ch['videoCodecType'] || '',
                            resolution: ch['Video'] && ch['Video']['videoResolutionWidth'] && ch['Video']['videoResolutionHeight'] ?
                                `${ch['Video']['videoResolutionWidth']}x${ch['Video']['videoResolutionHeight']}` : '',
                            status: 'enabled'
                        });
                    }
                }
            } catch (e) {
                // ignore
            }
        })
    );
    return results;
}

// ---- API: /streams/:streamId/preview ----
// Convert Hikvision JPEG-push (multipart/x-mixed-replace) to HTTP MJPEG
async function proxyMjpegStream(streamId, res) {
    // Hikvision ISAPI: /ISAPI/Streaming/channels/{id}/httpPreview
    const channelMap = { main: '101', sub: '102', third: '103' };
    const chId = channelMap[streamId] || channelMap.main;
    const url = `${DEVICE_ADDRESS}/ISAPI/Streaming/channels/${chId}/httpPreview`;
    try {
        const response = await axios({
            url,
            method: 'get',
            responseType: 'stream',
            headers: {
                ...getAuthHeader(),
                'Accept': 'multipart/x-mixed-replace'
            },
            timeout: 8000,
            httpAgent: IS_HTTPS ? undefined : new http.Agent({ keepAlive: true }),
            httpsAgent: IS_HTTPS ? new https.Agent({ rejectUnauthorized: NODE_TLS_REJECT_UNAUTHORIZED === '1' }) : undefined,
            validateStatus: s => s < 500
        });

        if (response.status !== 200) {
            res.status(503).end('Camera not streaming');
            return;
        }

        // Proxy the multipart MJPEG stream transparently
        res.setHeader('Content-Type', 'multipart/x-mixed-replace;boundary=--frame');
        response.data.pipe(res);
    } catch (e) {
        res.status(502).end('Failed to connect to device stream');
    }
}

// ---- Express Server Setup ----
const app = express();

app.get('/snapshot', async (req, res) => {
    if (devicePhase !== 'Running') {
        res.status(503).json({ error: 'Device not connected' });
        return;
    }
    const channel = req.query.channel || 'main';
    try {
        const snapshotRes = await getSnapshot(channel);
        if (snapshotRes.status === 200) {
            res.setHeader('Content-Type', 'image/jpeg');
            snapshotRes.data.pipe(res);
        } else {
            res.status(snapshotRes.status).end('Snapshot not available');
        }
    } catch (e) {
        res.status(502).end('Device snapshot error');
    }
});

app.get('/streams', async (req, res) => {
    if (devicePhase !== 'Running') {
        res.status(503).json({ error: 'Device not connected' });
        return;
    }
    try {
        const streams = await getStreams();
        res.json({ streams });
    } catch (e) {
        res.status(500).json({ error: 'Failed to retrieve streams' });
    }
});

app.get('/streams/:streamId/preview', async (req, res) => {
    if (devicePhase !== 'Running') {
        res.status(503).json({ error: 'Device not connected' });
        return;
    }
    const { streamId } = req.params;
    await proxyMjpegStream(streamId, res);
});

// ---- Start everything ----
(async () => {
    await refreshDeviceAddress();
    devicePhaseLoop();
    app.listen(parseInt(DEVICE_HTTP_PORT, 10), DEVICE_SHIFU_HOST, () => {
        console.log(
            `DeviceShifu HTTP server running at http://${DEVICE_SHIFU_HOST}:${DEVICE_HTTP_PORT}`
        );
    });
})();
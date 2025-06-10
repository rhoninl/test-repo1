// DeviceShifu for Hikvision IP Camera/Surveillance Device
// Language: JavaScript (Node.js)

const http = require('http');
const https = require('https');
const express = require('express');
const fs = require('fs');
const yaml = require('js-yaml');
const path = require('path');
const { KubeConfig, CustomObjectsApi, PatchUtils } = require('@kubernetes/client-node');

// Env Vars
const EDGEDEVICE_NAME = process.env.EDGEDEVICE_NAME;
const EDGEDEVICE_NAMESPACE = process.env.EDGEDEVICE_NAMESPACE;
const DEVICE_SHIFU_HTTP_PORT = process.env.DEVICE_SHIFU_HTTP_PORT || '8080';
const DEVICE_IP = process.env.DEVICE_IP; // Camera IP
const DEVICE_HTTP_PORT = process.env.DEVICE_HTTP_PORT || '80';
const DEVICE_HTTPS_PORT = process.env.DEVICE_HTTPS_PORT || '443';
const DEVICE_USERNAME = process.env.DEVICE_USERNAME || 'admin';
const DEVICE_PASSWORD = process.env.DEVICE_PASSWORD || '';
const DEVICE_USE_HTTPS = process.env.DEVICE_USE_HTTPS === 'true';

// Config/Instruction YAML Path
const INSTRUCTION_PATH = '/etc/edgedevice/config/instructions';

const API_GROUP = 'shifu.edgenesis.io';
const API_VERSION = 'v1alpha1';
const CRD_PLURAL = 'edgedevices';

let edgeDeviceStatusPhase = 'Unknown'; // Pending/Running/Failed/Unknown

// ------------------- Kubernetes CRD (EdgeDevice) Status Updater -------------------

const kc = new KubeConfig();
kc.loadFromCluster();
const k8sCustomApi = kc.makeApiClient(CustomObjectsApi);

// Helper: Patch EdgeDevice status
async function updateEdgeDevicePhase(phase) {
    if (!EDGEDEVICE_NAME || !EDGEDEVICE_NAMESPACE) return;
    if (phase === edgeDeviceStatusPhase) return;
    edgeDeviceStatusPhase = phase;
    try {
        await k8sCustomApi.patchNamespacedCustomObjectStatus(
            API_GROUP,
            API_VERSION,
            EDGEDEVICE_NAMESPACE,
            CRD_PLURAL,
            EDGEDEVICE_NAME,
            { status: { edgeDevicePhase: phase } },
            undefined,
            undefined,
            undefined,
            { headers: { 'Content-Type': PatchUtils.PATCH_FORMAT_JSON_MERGE_PATCH } }
        );
    } catch (e) {
        // Don't throw, just log
        console.error('Failed to patch EdgeDevice status:', e.body || e);
    }
}

// Helper: Fetch EdgeDevice
async function fetchEdgeDevice() {
    if (!EDGEDEVICE_NAME || !EDGEDEVICE_NAMESPACE) return null;
    try {
        const resp = await k8sCustomApi.getNamespacedCustomObject(
            API_GROUP,
            API_VERSION,
            EDGEDEVICE_NAMESPACE,
            CRD_PLURAL,
            EDGEDEVICE_NAME
        );
        return resp.body;
    } catch (e) {
        return null;
    }
}

// ------------------- Load API Instructions from ConfigMap -------------------
let apiInstructions = {};
function loadApiInstructions() {
    try {
        const files = fs.readdirSync(INSTRUCTION_PATH);
        apiInstructions = {};
        files.forEach(f => {
            const full = path.join(INSTRUCTION_PATH, f);
            const doc = yaml.load(fs.readFileSync(full, 'utf8'));
            Object.assign(apiInstructions, doc);
        });
    } catch (e) {
        // No instructions or error, fallback to empty
        apiInstructions = {};
    }
}
loadApiInstructions();

// ------------------- Hikvision ISAPI/HTTP Camera Helpers -------------------

// Basic Auth header
function getAuthHeader() {
    if (!DEVICE_USERNAME || !DEVICE_PASSWORD) return {};
    const auth = Buffer.from(`${DEVICE_USERNAME}:${DEVICE_PASSWORD}`).toString('base64');
    return { 'Authorization': `Basic ${auth}` };
}

// HTTP/HTTPS client
function getHttpClient() {
    return DEVICE_USE_HTTPS ? https : http;
}
function getDeviceHost() {
    return DEVICE_IP + ':' + (DEVICE_USE_HTTPS ? DEVICE_HTTPS_PORT : DEVICE_HTTP_PORT);
}
function getDeviceBaseUrl() {
    return (DEVICE_USE_HTTPS ? 'https' : 'http') + '://' + getDeviceHost();
}
function getApiSettings(apiName) {
    return (apiInstructions[apiName] && apiInstructions[apiName].protocolPropertyList) || {};
}

// Helper: Test device connectivity (fetch system info)
async function testConnectivity() {
    const url = getDeviceBaseUrl() + '/ISAPI/System/deviceInfo';
    return new Promise((resolve, reject) => {
        const opts = {
            method: 'GET',
            headers: { ...getAuthHeader() },
            timeout: 3000,
        };
        getHttpClient().get(url, opts, res => {
            if (res.statusCode === 200) resolve(true);
            else resolve(false);
        }).on('error', () => resolve(false));
    });
}

// Helper: Get available video streams
async function getStreamsList() {
    // Use ISAPI: /ISAPI/Streaming/channels
    const url = getDeviceBaseUrl() + '/ISAPI/Streaming/channels';
    return new Promise((resolve, reject) => {
        const opts = {
            method: 'GET',
            headers: { ...getAuthHeader() },
            timeout: 4000,
        };
        let buf = '';
        getHttpClient().get(url, opts, res => {
            if (res.statusCode !== 200) return resolve([]);
            res.setEncoding('utf8');
            res.on('data', d => buf += d);
            res.on('end', () => {
                // Parse XML, extract stream info
                try {
                    const parseString = require('xml2js').parseString;
                    parseString(buf, (err, result) => {
                        if (err || !result || !result.StreamingChannelList) return resolve([]);
                        const chs = result.StreamingChannelList.StreamingChannel;
                        let streams = [];
                        if (Array.isArray(chs)) {
                            for (const ch of chs) {
                                let id = ch.id && ch.id[0];
                                let enabled = ch.enabled && ch.enabled[0] === 'true';
                                let video = ch.Video && ch.Video[0];
                                let resolution = video && video.resolution && video.resolution[0];
                                let codec = video && video.codec && video.codec[0];
                                streams.push({
                                    id: id,
                                    status: enabled ? 'online' : 'offline',
                                    resolution: resolution || '',
                                    codec: codec || '',
                                });
                            }
                        }
                        resolve(streams);
                    });
                } catch (e) {
                    resolve([]);
                }
            });
        }).on('error', () => resolve([]));
    });
}

// Helper: Get snapshot JPEG buffer for a channel
async function getSnapshotBuffer(channelId) {
    // ISAPI: /ISAPI/Streaming/channels/<id>/picture
    // channelId: e.g. '101' (main), '102' (sub), '103' (third)
    const url = getDeviceBaseUrl() + `/ISAPI/Streaming/channels/${channelId}/picture`;
    return new Promise((resolve, reject) => {
        const opts = {
            method: 'GET',
            headers: { ...getAuthHeader() },
            timeout: 5000,
        };
        getHttpClient().get(url, opts, res => {
            if (res.statusCode !== 200) return resolve(null);
            let bufs = [];
            res.on('data', d => bufs.push(d));
            res.on('end', () => resolve(Buffer.concat(bufs)));
        }).on('error', () => resolve(null));
    });
}

// Helper: Get MJPEG HTTP stream for a channel
function proxyMjpegStream(channelId, clientRes) {
    // ISAPI MJPEG: /ISAPI/Streaming/channels/<id>/httpPreview
    const url = getDeviceBaseUrl() + `/ISAPI/Streaming/channels/${channelId}/httpPreview`;
    const opts = {
        method: 'GET',
        headers: { ...getAuthHeader() },
    };

    const deviceReq = getHttpClient().request(url, opts, deviceRes => {
        if (!String(deviceRes.headers['content-type'] || '').includes('multipart/x-mixed-replace')) {
            clientRes.status(502).send('Device does not provide MJPEG stream');
            deviceRes.resume();
            return;
        }
        clientRes.status(200);
        clientRes.setHeader('Content-Type', deviceRes.headers['content-type']);
        // Pipe directly
        deviceRes.pipe(clientRes);
    });

    deviceReq.on('error', (err) => {
        clientRes.status(502).send('Failed to connect to device stream');
    });
    deviceReq.end();
}

// Map channel string to channelId
function channelStringToId(channel) {
    // 'main' => '101', 'sub' => '102', 'third' => '103'
    if (channel === 'main') return '101';
    if (channel === 'sub') return '102';
    if (channel === 'third') return '103';
    if (/^\d+$/.test(channel)) return channel;
    return '101';
}

// ------------------- Express API Setup -------------------

const app = express();

// Health endpoint
app.get('/healthz', (req, res) => res.send('ok'));

// API 1: GET /snapshot?channel=main
app.get('/snapshot', async (req, res) => {
    const settings = getApiSettings('snapshot');
    const channel = req.query.channel || settings.channel || 'main';
    const channelId = channelStringToId(channel);

    const buf = await getSnapshotBuffer(channelId);
    if (!buf) {
        res.status(502).send('Failed to retrieve snapshot');
        return;
    }
    res.setHeader('Content-Type', 'image/jpeg');
    res.send(buf);
});

// API 2: GET /streams
app.get('/streams', async (req, res) => {
    const settings = getApiSettings('streams');
    const streams = await getStreamsList();
    // Map ids: 101 -> main, 102 -> sub, 103 -> third
    const idMap = { '101': 'main', '102': 'sub', '103': 'third' };
    const result = streams.map(s => ({
        id: idMap[s.id] || s.id,
        resolution: s.resolution,
        codec: s.codec,
        status: s.status,
    }));
    res.json(result);
});

// API 3: GET /streams/:streamId/preview
app.get('/streams/:streamId/preview', async (req, res) => {
    const settings = getApiSettings('preview');
    const streamId = req.params.streamId;
    const channelId = channelStringToId(streamId);
    proxyMjpegStream(channelId, res);
});

// ------------------- DeviceShifu Main Loop -------------------

async function main() {
    // Attempt to retrieve device address from EdgeDevice CRD if not set
    if (!DEVICE_IP) {
        const edgeDevice = await fetchEdgeDevice();
        if (edgeDevice && edgeDevice.spec && edgeDevice.spec.address) {
            process.env.DEVICE_IP = edgeDevice.spec.address;
        }
    }

    // Device status management loop
    async function deviceStatusLoop() {
        let lastStatus = 'Unknown';
        setInterval(async () => {
            let phase = 'Unknown';
            if (!DEVICE_IP) {
                phase = 'Pending';
            } else {
                const ok = await testConnectivity();
                phase = ok ? 'Running' : 'Failed';
            }
            await updateEdgeDevicePhase(phase);
        }, 5000);
    }

    // Start HTTP server
    app.listen(DEVICE_SHIFU_HTTP_PORT, () => {
        console.log(`DeviceShifu HTTP server listening on ${DEVICE_SHIFU_HTTP_PORT}`);
    });

    // Start status loop
    deviceStatusLoop();
}

// Reload config on SIGHUP
process.on('SIGHUP', () => loadApiInstructions());

main();
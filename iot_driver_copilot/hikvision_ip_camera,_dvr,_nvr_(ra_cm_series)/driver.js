// deviceshifu-hikvision-ipcamera-driver.js

'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const yaml = require('js-yaml');
const http = require('http');
const https = require('https');
const express = require('express');
const cors = require('cors');
const { spawn } = require('child_process');
const process = require('process');
const { KubeConfig, CustomObjectsApi } = require('@kubernetes/client-node');

// ===== Logging Setup =====
const LOG_LEVEL = process.env.LOG_LEVEL || 'info';
function log(level, ...args) {
    const levels = { error: 0, warn: 1, info: 2, debug: 3 };
    if (levels[level] <= levels[LOG_LEVEL] || level === 'error') {
        console.log(`[${level.toUpperCase()}]`, ...args);
    }
}

// ====== Environment Variable Defaults ======
const EDGEDEVICE_NAME = process.env.EDGEDEVICE_NAME || 'deviceshifu-ipcamera';
const EDGEDEVICE_NAMESPACE = process.env.EDGEDEVICE_NAMESPACE || 'devices';
const CONFIG_MOUNT_PATH = process.env.CONFIG_MOUNT_PATH || '/etc/edgedevice/config';
const HTTP_HOST = process.env.HTTP_HOST || '0.0.0.0';
const HTTP_PORT = parseInt(process.env.HTTP_PORT) || 8080;
const DEVICE_ADDRESS = process.env.DEVICE_ADDRESS || '';
const DEVICE_PORT = process.env.DEVICE_PORT || '';
const DEVICE_USERNAME = process.env.DEVICE_USERNAME || '';
const DEVICE_PASSWORD = process.env.DEVICE_PASSWORD || '';

// ====== ShifuClient Class ======
class ShifuClient {
    constructor() {
        this.kubeConfig = null;
        this.k8sApi = null;
        this.initK8sClient();
    }

    initK8sClient() {
        try {
            this.kubeConfig = new KubeConfig();
            if (fs.existsSync('/var/run/secrets/kubernetes.io/serviceaccount/token')) {
                this.kubeConfig.loadFromCluster();
                log('info', 'Loaded in-cluster Kubernetes config');
            } else {
                this.kubeConfig.loadFromDefault();
                log('info', 'Loaded local kubeconfig');
            }
            this.k8sApi = this.kubeConfig.makeApiClient(CustomObjectsApi);
        } catch (err) {
            log('error', 'Failed to init Kubernetes client:', err);
            this.k8sApi = null;
        }
    }

    async getEdgeDevice() {
        if (!this.k8sApi) return null;
        try {
            const res = await this.k8sApi.getNamespacedCustomObject(
                'edge.openyurt.io',
                'v1alpha1',
                EDGEDEVICE_NAMESPACE,
                'edgedevices',
                EDGEDEVICE_NAME
            );
            return res.body;
        } catch (err) {
            log('warn', 'Cannot fetch EdgeDevice CR:', err.body || err.message || err);
            return null;
        }
    }

    async getDeviceAddress() {
        const cr = await this.getEdgeDevice();
        if (cr && cr.spec && cr.spec.address) {
            return cr.spec.address;
        }
        return DEVICE_ADDRESS;
    }

    async updateDeviceStatus(statusObj) {
        if (!this.k8sApi) return;
        try {
            const mergePatch = {
                status: statusObj
            };
            await this.k8sApi.patchNamespacedCustomObjectStatus(
                'edge.openyurt.io',
                'v1alpha1',
                EDGEDEVICE_NAMESPACE,
                'edgedevices',
                EDGEDEVICE_NAME,
                mergePatch,
                undefined,
                undefined,
                undefined,
                {
                    headers: { 'Content-Type': 'application/merge-patch+json' }
                }
            );
            log('info', 'Updated EdgeDevice status:', statusObj.connectionStatus);
        } catch (err) {
            log('warn', 'Failed to update EdgeDevice status:', err.body || err.message || err);
        }
    }

    readMountedConfigFile(filename) {
        try {
            const filePath = path.join(CONFIG_MOUNT_PATH, filename);
            if (!fs.existsSync(filePath)) return null;
            const content = fs.readFileSync(filePath, 'utf8');
            if (filename.endsWith('.yaml') || filename.endsWith('.yml')) {
                return yaml.load(content);
            }
            return content;
        } catch (err) {
            log('error', `Error reading config file ${filename}:`, err);
            return null;
        }
    }

    getInstructionConfig() {
        // Always instructions.yaml if exists, or instructions
        let config = this.readMountedConfigFile('instructions.yaml');
        if (!config) config = this.readMountedConfigFile('instructions');
        return config;
    }
}

// ====== DeviceShifuDriver Class ======
class DeviceShifuDriver {
    constructor() {
        this.shutdownFlag = false;
        this.latestData = {}; // { endpoint: latest response }
        this.app = express();
        this.shifuClient = new ShifuClient();
        this.instructionConfig = null;
        this.httpServer = null;
        this.deviceAddress = '';
        this.devicePort = '';
        this.deviceConnected = false;
        this.connectionMonitorInterval = null;
        this.backgroundThread = null;
        this.routeHandlers = {};
    }

    async initialize() {
        this.app.use(express.json());
        this.app.use(cors());
        this.setupHealthRoutes();
        await this.loadConfigAndSetupRoutes();
        await this.connectDevice();
        this.startConnectionMonitor();
    }

    setupHealthRoutes() {
        this.app.get('/health', (req, res) => {
            res.status(200).json({
                status: 'ok',
                deviceConnected: this.deviceConnected
            });
        });

        this.app.get('/status', (req, res) => {
            res.status(200).json({
                device: {
                    address: this.deviceAddress,
                    port: this.devicePort,
                    connected: this.deviceConnected
                },
                driver: {
                    latestData: this.latestData,
                    timestamp: new Date().toISOString()
                }
            });
        });
    }

    async loadConfigAndSetupRoutes() {
        // For this device, only use the provided instruction
        this.instructionConfig = [
            {
                id: "2c83f487-7f5f-42cd-94f9-d3234d0ab406",
                session_id: "1613d116-bfa5-471b-9f06-f57fb9842f02",
                method: "GET",
                path: "/stream",
                description: "Provides a live video stream from the IP camera. Use query parameters such as 'channel' and 'quality' to customize the stream if needed. The endpoint returns a continuous media stream compatible with browser video players (e.g., via an HLS or MJPEG stream), using appropriate HTTP headers like Content-Type."
            }
        ];
        this.setupRoutesFromInstructions();
    }

    setupRoutesFromInstructions() {
        for (const instr of this.instructionConfig) {
            const method = instr.method.toLowerCase();
            const endpoint = instr.path;
            this.routeHandlers[endpoint] = this.generateRouteHandler(instr);
            this.app[method](endpoint, this.routeHandlers[endpoint]);
            log('info', `Registered endpoint: [${instr.method}] ${endpoint}`);
        }
    }

    async connectDevice() {
        // Get device address from CR or env
        this.deviceAddress = await this.shifuClient.getDeviceAddress();
        this.devicePort = DEVICE_PORT;
        // For HTTP camera, test connection by requesting a known ISAPI endpoint (e.g., GET /ISAPI/System/status)
        const url = this.getISAPIDeviceUrl('/ISAPI/System/status');
        // Camera may require Basic Auth
        return new Promise((resolve) => {
            const options = {
                method: 'GET',
                headers: {}
            };
            if (DEVICE_USERNAME && DEVICE_PASSWORD) {
                const authString = Buffer.from(`${DEVICE_USERNAME}:${DEVICE_PASSWORD}`).toString('base64');
                options.headers['Authorization'] = `Basic ${authString}`;
            }
            const proto = url.startsWith('https') ? https : http;
            const req = proto.request(url, options, (res) => {
                if (res.statusCode >= 200 && res.statusCode < 300) {
                    this.deviceConnected = true;
                    log('info', `Connected to IP camera at ${this.deviceAddress}`);
                    this.shifuClient.updateDeviceStatus({ connectionStatus: 'Connected' });
                    resolve(true);
                } else {
                    this.deviceConnected = false;
                    log('warn', `Camera returned status ${res.statusCode}`);
                    this.shifuClient.updateDeviceStatus({ connectionStatus: 'Disconnected' });
                    resolve(false);
                }
                res.resume();
            });
            req.on('error', (err) => {
                this.deviceConnected = false;
                log('warn', `Failed to connect to camera:`, err && err.message);
                this.shifuClient.updateDeviceStatus({ connectionStatus: 'Disconnected' });
                resolve(false);
            });
            req.setTimeout(5000, () => {
                this.deviceConnected = false;
                log('warn', 'Timeout connecting to camera');
                this.shifuClient.updateDeviceStatus({ connectionStatus: 'Disconnected' });
                req.abort();
                resolve(false);
            });
            req.end();
        });
    }

    getISAPIDeviceUrl(apiPath) {
        let addr = this.deviceAddress;
        if (!addr) return '';
        if (!addr.startsWith('http')) {
            addr = `http://${addr}`;
        }
        if (this.devicePort) {
            // Remove any existing port and append our port
            addr = addr.replace(/:\d+/, '');
            addr = addr.replace(/\/$/, '');
            addr = `${addr}:${this.devicePort}`;
        }
        if (!apiPath.startsWith('/')) apiPath = '/' + apiPath;
        return addr + apiPath;
    }

    // For the /stream endpoint, proxy MJPEG or HLS stream from camera to client browser
    generateRouteHandler(instr) {
        if (instr.path === '/stream' && instr.method === 'GET') {
            return async (req, res) => {
                if (!this.deviceConnected) {
                    return res.status(503).json({ error: 'Device not connected' });
                }
                // Parse query params for channel/quality if needed
                const channel = req.query.channel || 1;
                const quality = req.query.quality || 1;
                // For most Hikvision IP cameras, MJPEG stream is at /ISAPI/Streaming/channels/{channel}0{quality}/httpPreview
                // (quality: 1=mainstream, 2=substream, etc.)
                const camStreamPath = `/ISAPI/Streaming/channels/${channel}0${quality}/httpPreview`;
                const url = this.getISAPIDeviceUrl(camStreamPath);

                log('debug', `Proxying camera stream from ${url}`);

                // Proxy the MJPEG stream
                const options = {
                    method: 'GET',
                    headers: {}
                };
                if (DEVICE_USERNAME && DEVICE_PASSWORD) {
                    const authString = Buffer.from(`${DEVICE_USERNAME}:${DEVICE_PASSWORD}`).toString('base64');
                    options.headers['Authorization'] = `Basic ${authString}`;
                }
                const proto = url.startsWith('https') ? https : http;
                const camReq = proto.request(url, options, (camRes) => {
                    if (camRes.statusCode !== 200) {
                        return res.status(camRes.statusCode).json({ error: `Camera responded with ${camRes.statusCode}` });
                    }
                    // Forward headers for MJPEG
                    res.setHeader('Content-Type', camRes.headers['content-type'] || 'multipart/x-mixed-replace');
                    camRes.pipe(res);
                });
                camReq.on('error', (err) => {
                    log('error', 'Camera stream request failed:', err && err.message);
                    if (!res.headersSent)
                        res.status(502).json({ error: 'Failed to connect to camera stream' });
                });
                camReq.setTimeout(15000, () => {
                    log('warn', 'Camera stream request timeout');
                    camReq.abort();
                    if (!res.headersSent)
                        res.status(504).json({ error: 'Camera stream timeout' });
                });
                camReq.end();
            };
        }
        // 404 for any undefined method/path
        return (req, res) => {
            res.status(404).json({ error: 'Not Implemented' });
        };
    }

    startConnectionMonitor() {
        this.connectionMonitorInterval = setInterval(async () => {
            if (this.shutdownFlag) return;
            const prevStatus = this.deviceConnected;
            await this.connectDevice();
            if (this.deviceConnected !== prevStatus) {
                log('info', `Camera connection state changed: ${this.deviceConnected ? 'Connected' : 'Disconnected'}`);
            }
        }, 30000);
    }

    async shutdown() {
        this.shutdownFlag = true;
        if (this.connectionMonitorInterval) clearInterval(this.connectionMonitorInterval);
        if (this.httpServer) {
            await new Promise((resolve) => this.httpServer.close(resolve));
        }
        process.exit(0);
    }

    signalHandler(signal) {
        log('info', `Received signal ${signal}, shutting down...`);
        this.shutdown();
    }

    run() {
        this.httpServer = this.app.listen(HTTP_PORT, HTTP_HOST, () => {
            log('info', `DeviceShifu HTTP driver running at http://${HTTP_HOST}:${HTTP_PORT}`);
        });
        process.on('SIGINT', () => this.signalHandler('SIGINT'));
        process.on('SIGTERM', () => this.signalHandler('SIGTERM'));
    }
}

// ====== Main Entrypoint ======
(async () => {
    const driver = new DeviceShifuDriver();
    try {
        await driver.initialize();
        driver.run();
    } catch (err) {
        log('error', 'Fatal error in driver startup:', err);
        process.exit(1);
    }
})();
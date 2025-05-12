const http = require('http');
const url = require('url');
const { spawn } = require('child_process');
const { Writable } = require('stream');

// Configuration from environment variables
const DEVICE_IP = process.env.HIK_DEVICE_IP;
const DEVICE_RTSP_PORT = process.env.HIK_RTSP_PORT || '554';
const DEVICE_RTSP_USER = process.env.HIK_RTSP_USER || 'admin';
const DEVICE_RTSP_PASS = process.env.HIK_RTSP_PASS || '';
const DEVICE_RTSP_PATH = process.env.HIK_RTSP_PATH || 'Streaming/Channels/101';
const SERVER_HOST = process.env.HIK_HTTP_HOST || '0.0.0.0';
const SERVER_PORT = process.env.HIK_HTTP_PORT || 8080;

// Validate required env vars
if (!DEVICE_IP || !DEVICE_RTSP_USER || !DEVICE_RTSP_PASS) {
    throw new Error('Missing required environment variables for Hikvision connection');
}

// Global state
let streaming = false;
let ffmpegProcess = null;
let viewers = [];

// Helper: Build RTSP URL
function buildRtspUrl() {
    return `rtsp://${encodeURIComponent(DEVICE_RTSP_USER)}:${encodeURIComponent(DEVICE_RTSP_PASS)}@${DEVICE_IP}:${DEVICE_RTSP_PORT}/${DEVICE_RTSP_PATH}`;
}

// Helper: Start pulling RTSP and transcode to MJPEG
function startStream() {
    if (streaming && ffmpegProcess) return;

    const rtspUrl = buildRtspUrl();

    // Use ffmpeg to convert RTSP (H264/H265) to MJPEG for browser compatibility
    ffmpegProcess = spawn('ffmpeg', [
        '-rtsp_transport', 'tcp',
        '-i', rtspUrl,
        '-f', 'mjpeg',
        '-q', '5',
        '-r', '20',
        '-'
    ], { stdio: ['ignore', 'pipe', 'ignore'] });

    streaming = true;

    ffmpegProcess.stdout.on('data', (chunk) => {
        // Broadcast to all connected viewers
        viewers.forEach((res) => {
            res.write(chunk);
        });
    });

    ffmpegProcess.on('close', () => {
        streaming = false;
        ffmpegProcess = null;
        viewers.forEach((res) => {
            try {
                res.end();
            } catch (e) {}
        });
        viewers = [];
    });
}

// Helper: Stop pulling RTSP
function stopStream() {
    if (ffmpegProcess) {
        ffmpegProcess.kill('SIGTERM');
        ffmpegProcess = null;
    }
    streaming = false;
}

// HTTP Server
const server = http.createServer((req, res) => {
    const reqUrl = url.parse(req.url, true);

    // Start Stream endpoint
    if (req.method === 'POST' && reqUrl.pathname === '/stream/start') {
        if (!streaming) {
            startStream();
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ status: 'started' }));
        } else {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ status: 'already_streaming' }));
        }
        return;
    }

    // Stop Stream endpoint
    if (req.method === 'POST' && reqUrl.pathname === '/stream/stop') {
        stopStream();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'stopped' }));
        return;
    }

    // Video Stream endpoint (HTTP MJPEG)
    if (req.method === 'GET' && reqUrl.pathname === '/stream') {
        if (!streaming) {
            res.writeHead(503, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Stream not started. POST /stream/start first.' }));
            return;
        }
        res.writeHead(200, {
            'Content-Type': 'multipart/x-mixed-replace; boundary=ffserver',
            'Cache-Control': 'no-cache',
            'Connection': 'close',
            'Pragma': 'no-cache'
        });
        viewers.push(res);

        req.on('close', () => {
            viewers = viewers.filter(r => r !== res);
        });
        return;
    }

    // Not found
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
});

// Graceful shutdown
process.on('SIGINT', () => {
    stopStream();
    process.exit();
});

server.listen(SERVER_PORT, SERVER_HOST, () => {
    console.log(`Hikvision HTTP video driver running at http://${SERVER_HOST}:${SERVER_PORT}/`);
    console.log('API:');
    console.log('  POST /stream/start  - Start video streaming');
    console.log('  POST /stream/stop   - Stop video streaming');
    console.log('  GET  /stream        - HTTP MJPEG video stream');
});
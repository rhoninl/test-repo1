const http = require('http');
const { spawn } = require('child_process');
const { parse } = require('url');
const { PassThrough } = require('stream');

const CAMERA_IP = process.env.CAMERA_IP;
const CAMERA_USER = process.env.CAMERA_USER || 'admin';
const CAMERA_PASSWORD = process.env.CAMERA_PASSWORD || '';
const RTSP_PORT = process.env.RTSP_PORT || '554';
const RTSP_PATH = process.env.RTSP_PATH || 'Streaming/Channels/101';
const HTTP_HOST = process.env.HTTP_HOST || '0.0.0.0';
const HTTP_PORT = process.env.HTTP_PORT || 8080;

// RTSP URL for Hikvision
const RTSP_URL = `rtsp://${CAMERA_USER}:${CAMERA_PASSWORD}@${CAMERA_IP}:${RTSP_PORT}/${RTSP_PATH}`;

// Global state for stream management
let ffmpegProcess = null;
let streamClients = [];
let videoStream = null;
let isStreaming = false;

// Utility: Start FFmpeg to convert RTSP to MJPEG over HTTP
function startStream() {
    if (isStreaming) return;
    isStreaming = true;

    videoStream = new PassThrough();

    ffmpegProcess = spawn(
        'ffmpeg',
        [
            '-rtsp_transport', 'tcp',
            '-i', RTSP_URL,
            '-f', 'mjpeg',
            '-q:v', '5',
            '-r', '20',
            '-an',
            '-'
        ],
        { stdio: ['ignore', 'pipe', 'ignore'] }
    );

    ffmpegProcess.stdout.on('data', (chunk) => {
        if (videoStream) videoStream.write(chunk);
    });

    ffmpegProcess.on('exit', () => {
        stopStream();
    });

    ffmpegProcess.on('error', () => {
        stopStream();
    });
}

function stopStream() {
    isStreaming = false;

    if (ffmpegProcess) {
        ffmpegProcess.kill('SIGKILL');
        ffmpegProcess = null;
    }
    if (videoStream) {
        videoStream.end();
        videoStream = null;
    }
    streamClients.forEach(res => {
        try { res.end(); } catch {}
    });
    streamClients = [];
}

// HTTP Server
const server = http.createServer((req, res) => {
    const url = parse(req.url, true);
    // POST /stream/start
    if (req.method === 'POST' && url.pathname === '/stream/start') {
        startStream();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', message: 'Stream started' }));
        return;
    }
    // POST /stream/stop
    if (req.method === 'POST' && url.pathname === '/stream/stop') {
        stopStream();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', message: 'Stream stopped' }));
        return;
    }
    // GET /stream
    if (req.method === 'GET' && url.pathname === '/stream') {
        if (!isStreaming) {
            res.writeHead(503, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Stream not started. POST /stream/start first.' }));
            return;
        }
        res.writeHead(200, {
            'Content-Type': 'multipart/x-mixed-replace; boundary=ffserver',
            'Connection': 'close',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
        });
        streamClients.push(res);

        const onData = (chunk) => {
            try {
                res.write(`--ffserver\r\nContent-Type: image/jpeg\r\nContent-Length: ${chunk.length}\r\n\r\n`);
                res.write(chunk);
            } catch (e) {}
        };

        videoStream.on('data', onData);

        req.on('close', () => {
            videoStream.removeListener('data', onData);
            streamClients = streamClients.filter(c => c !== res);
        });
        return;
    }
    // 404 Not Found
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
});

// Graceful shutdown
process.on('SIGINT', () => { stopStream(); process.exit(); });
process.on('SIGTERM', () => { stopStream(); process.exit(); });

// Start server
server.listen(HTTP_PORT, HTTP_HOST, () => {
    // No output as per requirements
});
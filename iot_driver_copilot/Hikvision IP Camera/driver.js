const http = require('http');
const { spawn } = require('child_process');
const url = require('url');
const { Readable } = require('stream');

// Configuration from environment variables
const CAMERA_IP = process.env.CAMERA_IP;
const CAMERA_RTSP_PORT = process.env.CAMERA_RTSP_PORT || '554';
const CAMERA_USERNAME = process.env.CAMERA_USERNAME || 'admin';
const CAMERA_PASSWORD = process.env.CAMERA_PASSWORD || '12345';
const CAMERA_CHANNEL = process.env.CAMERA_CHANNEL || '101';
const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';
const SERVER_PORT = parseInt(process.env.SERVER_PORT || '8080', 10);

if (!CAMERA_IP) {
    throw new Error('CAMERA_IP environment variable is required');
}

// RTSP URL construction
const RTSP_URI = `rtsp://${CAMERA_USERNAME}:${CAMERA_PASSWORD}@${CAMERA_IP}:${CAMERA_RTSP_PORT}/Streaming/Channels/${CAMERA_CHANNEL}`;

// State
let ffmpegProcess = null;
let viewers = [];
let streamStarted = false;

// Helper: Start ffmpeg process to pull RTSP and output MJPEG
function startStream() {
    if (ffmpegProcess) return;

    ffmpegProcess = spawn(
        'ffmpeg', [
            '-rtsp_transport', 'tcp',
            '-i', RTSP_URI,
            '-an',
            '-c:v', 'mjpeg',
            '-f', 'mjpeg',
            '-q:v', '5',
            '-'
        ],
        { stdio: ['ignore', 'pipe', 'ignore'] }
    );

    ffmpegProcess.on('close', () => {
        ffmpegProcess = null;
        streamStarted = false;
        viewers.forEach(res => {
            if (!res.finished) res.end();
        });
        viewers = [];
    });

    ffmpegProcess.stdout.on('data', chunk => {
        viewers.forEach(res => {
            if (!res.finished) res.write(chunk);
        });
    });
}

// Helper: Stop ffmpeg process
function stopStream() {
    if (ffmpegProcess) {
        ffmpegProcess.kill('SIGKILL');
        ffmpegProcess = null;
    }
    streamStarted = false;
}

// HTTP Server
const server = http.createServer(async (req, res) => {
    const parsedUrl = url.parse(req.url, true);

    // POST /stream/start
    if (req.method === 'POST' && parsedUrl.pathname === '/stream/start') {
        if (!streamStarted) {
            startStream();
            streamStarted = true;
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ success: true, message: 'Stream started' }));
        return;
    }

    // POST /stream/stop
    if (req.method === 'POST' && parsedUrl.pathname === '/stream/stop') {
        stopStream();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ success: true, message: 'Stream stopped' }));
        return;
    }

    // GET /stream/video (browser/cli access)
    if (req.method === 'GET' && parsedUrl.pathname === '/stream/video') {
        if (!streamStarted) {
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
    res.end(JSON.stringify({ error: 'Not Found' }));
});

server.listen(SERVER_PORT, SERVER_HOST, () => {
    // Server started
});
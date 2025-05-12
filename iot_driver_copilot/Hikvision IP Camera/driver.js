const http = require('http');
const url = require('url');
const { spawn } = require('child_process');
const { PassThrough } = require('stream');

// Load configuration from environment variables
const DEVICE_IP = process.env.DEVICE_IP;
const DEVICE_RTSP_PORT = process.env.DEVICE_RTSP_PORT || '554';
const DEVICE_RTSP_PATH = process.env.DEVICE_RTSP_PATH || 'Streaming/Channels/101';
const DEVICE_RTSP_USER = process.env.DEVICE_RTSP_USER || '';
const DEVICE_RTSP_PASS = process.env.DEVICE_RTSP_PASS || '';
const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';
const SERVER_PORT = process.env.SERVER_PORT || 8080;

if (!DEVICE_IP) {
    throw new Error("DEVICE_IP environment variable is required");
}

const RTSP_URL =
    'rtsp://' +
    (DEVICE_RTSP_USER && DEVICE_RTSP_PASS
        ? encodeURIComponent(DEVICE_RTSP_USER) + ':' + encodeURIComponent(DEVICE_RTSP_PASS) + '@'
        : '') +
    DEVICE_IP +
    ':' +
    DEVICE_RTSP_PORT +
    '/' +
    DEVICE_RTSP_PATH.replace(/^\//, '');

let streamProcess = null;
let videoStream = null;
let clients = [];
let isStreaming = false;

// HLS Segmenter (in-memory, simple implementation)
const hlsSegments = [];
const HLS_SEGMENT_MAX = 5;
const hlsM3u8Header = '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n';

function startStream(res = null) {
    if (isStreaming) {
        if (res) res.writeHead(200).end(JSON.stringify({ status: 'already streaming' }));
        return;
    }
    // Use ffmpeg to convert RTSP to MPEG-TS (for HTTP/Browser playback)
    streamProcess = spawn('ffmpeg', [
        '-rtsp_transport', 'tcp',
        '-i', RTSP_URL,
        '-f', 'mpegts',
        '-codec:v', 'mpeg1video',
        '-codec:a', 'mp2',
        '-r', '25',
        '-'
    ]);
    videoStream = new PassThrough();
    streamProcess.stdout.pipe(videoStream);

    streamProcess.stderr.on('data', () => {});
    streamProcess.on('close', () => {
        isStreaming = false;
        videoStream = null;
        streamProcess = null;
        clients.forEach((client) => client.end());
        clients = [];
    });

    isStreaming = true;
    if (res) res.writeHead(200).end(JSON.stringify({ status: 'stream started' }));
}

function stopStream(res = null) {
    if (!isStreaming) {
        if (res) res.writeHead(200).end(JSON.stringify({ status: 'not streaming' }));
        return;
    }
    if (streamProcess) {
        streamProcess.kill('SIGTERM');
        streamProcess = null;
    }
    if (videoStream) {
        videoStream.end();
        videoStream = null;
    }
    isStreaming = false;
    if (res) res.writeHead(200).end(JSON.stringify({ status: 'stream stopped' }));
}

function handleStream(req, res) {
    if (!isStreaming) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Stream not started' }));
        return;
    }
    res.writeHead(200, {
        'Content-Type': 'video/mp2t',
        'Connection': 'keep-alive',
        'Cache-Control': 'no-cache'
    });
    clients.push(res);
    videoStream.pipe(res);

    req.on('close', () => {
        const idx = clients.indexOf(res);
        if (idx >= 0) clients.splice(idx, 1);
        try { res.end(); } catch (_) {}
    });
}

const requestHandler = (req, res) => {
    const parsedUrl = url.parse(req.url, true);
    if (req.method === 'POST' && parsedUrl.pathname === '/stream/start') {
        startStream(res);
        return;
    }
    if (req.method === 'POST' && parsedUrl.pathname === '/stream/stop') {
        stopStream(res);
        return;
    }
    if (req.method === 'GET' && parsedUrl.pathname === '/stream') {
        handleStream(req, res);
        return;
    }
    // Simple info endpoint
    if (req.method === 'GET' && parsedUrl.pathname === '/') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
            device: 'Hikvision IP Camera',
            endpoints: [
                { method: 'POST', path: '/stream/start', description: 'Start camera stream' },
                { method: 'POST', path: '/stream/stop', description: 'Stop camera stream' },
                { method: 'GET', path: '/stream', description: 'Get live video stream (MPEG-TS)' }
            ]
        }));
        return;
    }
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
};

const server = http.createServer(requestHandler);
server.listen(SERVER_PORT, SERVER_HOST, () => {
    console.log(`Hikvision IP Camera driver HTTP server running at http://${SERVER_HOST}:${SERVER_PORT}/`);
});
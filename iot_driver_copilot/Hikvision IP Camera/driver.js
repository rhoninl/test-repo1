const http = require('http');
const { spawn } = require('child_process');
const url = require('url');

// Environment Variables
const CAMERA_IP = process.env.CAMERA_IP;
const CAMERA_RTSP_PORT = process.env.CAMERA_RTSP_PORT || '554';
const CAMERA_USER = process.env.CAMERA_USER || '';
const CAMERA_PASS = process.env.CAMERA_PASS || '';
const CAMERA_CHANNEL = process.env.CAMERA_CHANNEL || '101';
const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';
const SERVER_PORT = process.env.SERVER_PORT || '8080';
const HTTP_STREAM_PORT = process.env.HTTP_STREAM_PORT || '8081';

if (!CAMERA_IP) {
    throw new Error('CAMERA_IP environment variable is required');
}

// RTSP URL construction
let RTSP_AUTH = '';
if (CAMERA_USER && CAMERA_PASS) {
    RTSP_AUTH = `${CAMERA_USER}:${CAMERA_PASS}@`;
}
const RTSP_URL = `rtsp://${RTSP_AUTH}${CAMERA_IP}:${CAMERA_RTSP_PORT}/Streaming/Channels/${CAMERA_CHANNEL}`;

// Stream state
let ffmpegProc = null;
let streamClients = [];
let isStreaming = false;

// HTTP MJPEG streaming server
const streamServer = http.createServer((req, res) => {
    if (req.url !== '/video') {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('Not Found');
        return;
    }
    res.writeHead(200, {
        'Content-Type': 'multipart/x-mixed-replace; boundary=ffserver',
        'Connection': 'close',
        'Pragma': 'no-cache',
        'Cache-Control': 'no-cache',
        'Expires': '0'
    });

    streamClients.push(res);

    req.on('close', () => {
        streamClients = streamClients.filter(c => c !== res);
    });
});
streamServer.listen(HTTP_STREAM_PORT, SERVER_HOST);

// Start FFmpeg process
function startStream() {
    if (isStreaming) return;
    // FFmpeg args: RTSP in, MJPEG out to stdout
    const args = [
        '-rtsp_transport', 'tcp',
        '-i', RTSP_URL,
        '-f', 'mjpeg',
        '-q:v', '2',
        '-r', '15',
        '-'
    ];
    ffmpegProc = spawn('ffmpeg', args);

    ffmpegProc.stdout.on('data', (chunk) => {
        for (const client of streamClients) {
            client.write(`--ffserver\r\nContent-Type: image/jpeg\r\nContent-Length: ${chunk.length}\r\n\r\n`);
            client.write(chunk);
            client.write('\r\n');
        }
    });

    ffmpegProc.stderr.on('data', () => { /* Silence FFmpeg logs */ });

    ffmpegProc.on('close', () => {
        isStreaming = false;
        ffmpegProc = null;
        for (const client of streamClients) {
            try { client.end(); } catch {}
        }
        streamClients = [];
    });

    isStreaming = true;
}

function stopStream() {
    if (ffmpegProc) {
        ffmpegProc.kill('SIGKILL');
        ffmpegProc = null;
    }
    isStreaming = false;
}

// Main API server
const apiServer = http.createServer((req, res) => {
    const parsedUrl = url.parse(req.url, true);

    // POST /stream/start
    if (req.method === 'POST' && parsedUrl.pathname === '/stream/start') {
        startStream();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
            status: 'streaming',
            message: 'Video stream started',
            stream_url: `http://${SERVER_HOST}:${HTTP_STREAM_PORT}/video`
        }));
        return;
    }

    // POST /stream/stop
    if (req.method === 'POST' && parsedUrl.pathname === '/stream/stop') {
        stopStream();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
            status: 'stopped',
            message: 'Video stream stopped'
        }));
        return;
    }

    // GET /
    if (req.method === 'GET' && parsedUrl.pathname === '/') {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(`
            <html>
            <body>
                <h1>Hikvision Camera Stream</h1>
                <img src="http://${SERVER_HOST}:${HTTP_STREAM_PORT}/video" style="max-width:100%;">
                <form method="POST" action="/stream/start"><button type="submit">Start Stream</button></form>
                <form method="POST" action="/stream/stop"><button type="submit">Stop Stream</button></form>
            </body>
            </html>
        `);
        return;
    }

    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not Found' }));
});

apiServer.listen(SERVER_PORT, SERVER_HOST);
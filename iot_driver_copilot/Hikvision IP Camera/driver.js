const http = require('http');
const url = require('url');
const { spawn } = require('child_process');
const { PassThrough } = require('stream');

// Environment variable configuration
const CAMERA_IP = process.env.CAMERA_IP;
const CAMERA_USERNAME = process.env.CAMERA_USERNAME;
const CAMERA_PASSWORD = process.env.CAMERA_PASSWORD;
const CAMERA_RTSP_PORT = process.env.CAMERA_RTSP_PORT || '554';

const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';
const SERVER_PORT = parseInt(process.env.SERVER_PORT, 10) || 8080;

if (!CAMERA_IP || !CAMERA_USERNAME || !CAMERA_PASSWORD) {
    throw new Error('CAMERA_IP, CAMERA_USERNAME, and CAMERA_PASSWORD environment variables must be set');
}

let streaming = false;
let ffmpeg = null;
let streamClients = [];
let videoStream = null;

// Helper to build RTSP URL
function getRtspUrl() {
    return `rtsp://${encodeURIComponent(CAMERA_USERNAME)}:${encodeURIComponent(CAMERA_PASSWORD)}@${CAMERA_IP}:${CAMERA_RTSP_PORT}/Streaming/Channels/101`;
}

// Start streaming: spawn ffmpeg and output to PassThrough stream
function startStreaming() {
    if (streaming) return;
    const rtspUrl = getRtspUrl();
    // Spawn ffmpeg process to convert RTSP to MJPEG (multipart/x-mixed-replace)
    ffmpeg = spawn('ffmpeg', [
        '-rtsp_transport', 'tcp',
        '-i', rtspUrl,
        '-f', 'mjpeg',
        '-q:v', '5',
        '-r', '20',
        '-an',
        '-'
    ], { stdio: ['ignore', 'pipe', 'ignore'] });

    videoStream = new PassThrough();

    ffmpeg.stdout.pipe(videoStream);

    ffmpeg.on('close', () => {
        streaming = false;
        videoStream && videoStream.end();
        streamClients.forEach(res => {
            if (!res.finished && !res.writableEnded) {
                try { res.end(); } catch (e) { }
            }
        });
        streamClients = [];
        videoStream = null;
    });

    streaming = true;
}

// Stop streaming: kill ffmpeg and cleanup
function stopStreaming() {
    if (ffmpeg) {
        ffmpeg.kill('SIGKILL');
        ffmpeg = null;
    }
    streaming = false;
    if (videoStream) {
        videoStream.end();
        videoStream = null;
    }
    streamClients.forEach(res => {
        if (!res.finished && !res.writableEnded) {
            try { res.end(); } catch (e) { }
        }
    });
    streamClients = [];
}

// HTTP Server
const server = http.createServer((req, res) => {
    const parsedUrl = url.parse(req.url, true);

    // Start Stream API
    if (req.method === 'POST' && parsedUrl.pathname === '/stream/start') {
        if (!streaming) {
            startStreaming();
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'streaming_started' }));
        return;
    }

    // Stop Stream API
    if (req.method === 'POST' && parsedUrl.pathname === '/stream/stop') {
        stopStreaming();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'streaming_stopped' }));
        return;
    }

    // MJPEG HTTP Video Stream
    if (req.method === 'GET' && parsedUrl.pathname === '/stream/video') {
        if (!streaming) {
            startStreaming();
        }
        res.writeHead(200, {
            'Content-Type': 'multipart/x-mixed-replace; boundary=ffserver',
            'Cache-Control': 'no-cache',
            'Connection': 'close',
            'Pragma': 'no-cache'
        });
        streamClients.push(res);
        const onClose = () => {
            streamClients = streamClients.filter(r => r !== res);
        };
        res.on('close', onClose);
        if (videoStream) {
            videoStream.pipe(res);
        } else {
            res.end();
        }
        return;
    }

    // Simple Info/Help
    if (req.method === 'GET' && parsedUrl.pathname === '/') {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(
            `<html><body>
                <h1>Hikvision IP Camera Driver</h1>
                <ul>
                  <li>POST <code>/stream/start</code> - Start Camera Streaming</li>
                  <li>POST <code>/stream/stop</code> - Stop Camera Streaming</li>
                  <li>GET <code>/stream/video</code> - View Video Stream (MJPEG)</li>
                </ul>
            </body></html>`
        );
        return;
    }

    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
});

server.listen(SERVER_PORT, SERVER_HOST, () => {
    // Server started
});
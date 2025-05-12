const http = require('http');
const express = require('express');
const { spawn } = require('child_process');
const { PassThrough } = require('stream');

// Environment variables
const DEVICE_IP = process.env.DEVICE_IP;
const RTSP_PORT = process.env.RTSP_PORT || '554';
const RTSP_USERNAME = process.env.RTSP_USERNAME;
const RTSP_PASSWORD = process.env.RTSP_PASSWORD;
const CAMERA_CHANNEL = process.env.CAMERA_CHANNEL || '101';
const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';
const SERVER_PORT = process.env.SERVER_PORT || 8080;

if (!DEVICE_IP || !RTSP_USERNAME || !RTSP_PASSWORD) {
    console.error('Missing DEVICE_IP, RTSP_USERNAME, or RTSP_PASSWORD environment variables.');
    process.exit(1);
}

// Construct RTSP URL
const getRtspUrl = () =>
    `rtsp://${encodeURIComponent(RTSP_USERNAME)}:${encodeURIComponent(RTSP_PASSWORD)}@${DEVICE_IP}:${RTSP_PORT}/Streaming/Channels/${CAMERA_CHANNEL}`;

let ffmpegProcess = null;
let videoStream = null;
let clientCount = 0;
let stopTimeout = null;

// Express app
const app = express();
app.use(express.json());

// Start RTSP stream and pipe to HTTP
function startStream() {
    if (ffmpegProcess && !ffmpegProcess.killed) {
        return;
    }
    if (videoStream) {
        videoStream.end();
    }
    videoStream = new PassThrough();

    // Spawn ffmpeg to pull RTSP and output to MPEG1 video over HTTP
    ffmpegProcess = spawn('ffmpeg', [
        '-rtsp_transport', 'tcp',
        '-i', getRtspUrl(),
        '-f', 'mpegts',
        '-codec:v', 'mpeg1video',
        '-r', '24',
        '-'
    ]);
    ffmpegProcess.stdout.pipe(videoStream);

    ffmpegProcess.stderr.on('data', () => {}); // suppress

    ffmpegProcess.on('error', (err) => {
        videoStream.end();
        ffmpegProcess = null;
    });
    ffmpegProcess.on('close', () => {
        videoStream.end();
        videoStream = null;
        ffmpegProcess = null;
    });
}

function stopStream() {
    if (ffmpegProcess) {
        ffmpegProcess.kill('SIGTERM');
    }
    if (videoStream) {
        videoStream.end();
        videoStream = null;
    }
    ffmpegProcess = null;
}

// API: Start stream
app.post('/stream/start', (req, res) => {
    startStream();
    res.status(200).json({ message: 'Stream started' });
});

// API: Stop stream
app.post('/stream/stop', (req, res) => {
    stopStream();
    res.status(200).json({ message: 'Stream stopped' });
});

// HTTP endpoint to view the stream in browser or via curl
app.get('/video', (req, res) => {
    if (!ffmpegProcess || !videoStream) {
        startStream();
    }

    clientCount++;
    res.writeHead(200, {
        'Content-Type': 'video/mp2t',
        'Connection': 'close',
        'Cache-Control': 'no-store'
    });
    const pipe = videoStream.pipe(new PassThrough());
    pipe.pipe(res);

    const cleanup = () => {
        pipe.destroy();
        clientCount--;
        if (clientCount === 0) {
            // Stop stream after 10 seconds of no clients
            if (stopTimeout) clearTimeout(stopTimeout);
            stopTimeout = setTimeout(stopStream, 10000);
        }
    };

    res.on('close', cleanup);
    res.on('finish', cleanup);
});

// Simple index for browser
app.get('/', (req, res) => {
    res.send(`
        <html>
        <body>
            <h1>Hikvision Camera Stream</h1>
            <video src="/video" controls autoplay style="width: 80vw;"></video>
        </body>
        </html>
    `);
});

const server = http.createServer(app);
server.listen(SERVER_PORT, SERVER_HOST, () => {
    console.log(`Server listening on http://${SERVER_HOST}:${SERVER_PORT}`);
});
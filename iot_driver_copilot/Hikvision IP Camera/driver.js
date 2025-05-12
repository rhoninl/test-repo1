const http = require('http');
const { spawn } = require('child_process');
const { PassThrough } = require('stream');
const url = require('url');

// Environment Variables
const DEVICE_IP = process.env.DEVICE_IP || '192.168.1.64';
const DEVICE_RTSP_PORT = process.env.DEVICE_RTSP_PORT || '554';
const DEVICE_RTSP_USER = process.env.DEVICE_RTSP_USER || 'admin';
const DEVICE_RTSP_PASS = process.env.DEVICE_RTSP_PASS || '12345';
const DEVICE_RTSP_PATH = process.env.DEVICE_RTSP_PATH || 'Streaming/Channels/101';
const HTTP_SERVER_HOST = process.env.HTTP_SERVER_HOST || '0.0.0.0';
const HTTP_SERVER_PORT = process.env.HTTP_SERVER_PORT || 8080;

let ffmpegProcess = null;
let streamClients = [];
let streamPassThrough = null;
let streaming = false;

function getRtspUrl() {
    let auth = '';
    if (DEVICE_RTSP_USER && DEVICE_RTSP_PASS) {
        auth = `${DEVICE_RTSP_USER}:${DEVICE_RTSP_PASS}@`;
    }
    return `rtsp://${auth}${DEVICE_IP}:${DEVICE_RTSP_PORT}/${DEVICE_RTSP_PATH}`;
}

// Start streaming from RTSP to HTTP MJPEG
function startStream(res = null) {
    if (!streaming) {
        streamPassThrough = new PassThrough();
        const args = [
            '-rtsp_transport', 'tcp',
            '-i', getRtspUrl(),
            '-f', 'mjpeg',
            '-q:v', '5',
            '-r', '25',
            '-an',
            '-'
        ];
        ffmpegProcess = spawn('ffmpeg', args, { stdio: ['ignore', 'pipe', 'ignore'] });
        ffmpegProcess.stdout.pipe(streamPassThrough);

        ffmpegProcess.on('close', () => {
            streaming = false;
            streamClients.forEach(client => {
                try {
                    client.end();
                } catch(e){}
            });
            streamClients = [];
            streamPassThrough = null;
            ffmpegProcess = null;
        });

        streaming = true;
    }
    if (res) {
        streamClients.push(res);
        res.writeHead(200, {
            'Content-Type': 'multipart/x-mixed-replace; boundary=ffserver',
            'Cache-Control': 'no-cache',
            'Connection': 'close',
            'Pragma': 'no-cache'
        });
        streamPassThrough.pipe(res);
        res.on('close', () => {
            // Remove client on disconnect
            streamClients = streamClients.filter(c => c !== res);
            if (streamClients.length === 0) {
                stopStream();
            }
        });
    }
}

function stopStream() {
    if (ffmpegProcess) {
        ffmpegProcess.kill('SIGTERM');
    }
}

// HTTP Server
const server = http.createServer((req, res) => {
    const parsedUrl = url.parse(req.url, true);
    if (req.method === 'POST' && parsedUrl.pathname === '/stream/start') {
        if (!streaming) {
            startStream();
        }
        res.writeHead(200, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({ status: 'ok', streaming: true }));
    } else if (req.method === 'POST' && parsedUrl.pathname === '/stream/stop') {
        stopStream();
        res.writeHead(200, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({ status: 'ok', streaming: false }));
    } else if (req.method === 'GET' && parsedUrl.pathname === '/stream') {
        if (!streaming) {
            startStream(res);
        } else {
            streamClients.push(res);
            res.writeHead(200, {
                'Content-Type': 'multipart/x-mixed-replace; boundary=ffserver',
                'Cache-Control': 'no-cache',
                'Connection': 'close',
                'Pragma': 'no-cache'
            });
            streamPassThrough.pipe(res);
            res.on('close', () => {
                streamClients = streamClients.filter(c => c !== res);
                if (streamClients.length === 0) {
                    stopStream();
                }
            });
        }
    } else {
        res.writeHead(404, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({ error: "Not found" }));
    }
});

server.listen(HTTP_SERVER_PORT, HTTP_SERVER_HOST);
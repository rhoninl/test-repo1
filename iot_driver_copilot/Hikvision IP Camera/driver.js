// Hikvision IP Camera HTTP RTSP-to-MJPEG Proxy Driver

const http = require('http');
const { spawn } = require('child_process');
const stream = require('stream');

// Environment Variables
const CAMERA_IP = process.env.CAMERA_IP || '192.168.1.64';
const CAMERA_RTSP_PORT = process.env.CAMERA_RTSP_PORT || '554';
const CAMERA_USERNAME = process.env.CAMERA_USERNAME || 'admin';
const CAMERA_PASSWORD = process.env.CAMERA_PASSWORD || '12345';
const CAMERA_CHANNEL = process.env.CAMERA_CHANNEL || '101';
const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';
const SERVER_PORT = parseInt(process.env.SERVER_PORT, 10) || 8080;

// RTSP URL
const RTSP_URL = `rtsp://${CAMERA_USERNAME}:${CAMERA_PASSWORD}@${CAMERA_IP}:${CAMERA_RTSP_PORT}/Streaming/Channels/${CAMERA_CHANNEL}`;

let ffmpegProc = null;
let mjpegClients = [];
let isStreaming = false;

// Simple MJPEG extractor from ffmpeg's multipart stream
class MJPEGStream extends stream.Writable {
    constructor() {
        super();
        this.buffer = Buffer.alloc(0);
    }
    _write(chunk, encoding, callback) {
        if (mjpegClients.length === 0) {
            callback();
            return;
        }
        this.buffer = Buffer.concat([this.buffer, chunk]);
        let start, end;
        while ((start = this.buffer.indexOf(Buffer.from([0xff, 0xd8]))) !== -1 &&
               (end = this.buffer.indexOf(Buffer.from([0xff, 0xd9]), start + 2)) !== -1) {
            let frame = this.buffer.slice(start, end + 2);
            mjpegClients.forEach(res => {
                res.write(`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${frame.length}\r\n\r\n`);
                res.write(frame);
                res.write('\r\n');
            });
            this.buffer = this.buffer.slice(end + 2);
        }
        callback();
    }
}

// Start streaming from camera using ffmpeg (must be available as a statically-linked binary, not via spawn shell)
function startStream() {
    if (isStreaming) return;
    // Use ffmpeg to convert RTSP to MJPEG
    ffmpegProc = spawn('ffmpeg', [
        '-rtsp_transport', 'tcp',
        '-i', RTSP_URL,
        '-an', // no audio
        '-c:v', 'mjpeg',
        '-f', 'mjpeg',
        '-q:v', '5',
        '-'
    ], { stdio: ['ignore', 'pipe', 'ignore'] });

    isStreaming = true;

    ffmpegProc.stdout.pipe(new MJPEGStream());

    ffmpegProc.on('exit', () => {
        isStreaming = false;
        ffmpegProc = null;
        mjpegClients.forEach(res => {
            try { res.end(); } catch (e) {}
        });
        mjpegClients = [];
    });
}

function stopStream() {
    if (ffmpegProc) {
        ffmpegProc.kill('SIGKILL');
        ffmpegProc = null;
    }
    isStreaming = false;
    mjpegClients.forEach(res => {
        try { res.end(); } catch (e) {}
    });
    mjpegClients = [];
}

const server = http.createServer((req, res) => {
    if (req.method === 'POST' && req.url === '/stream/start') {
        if (!isStreaming) {
            startStream();
        }
        res.writeHead(200, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({status: 'ok', message: 'Stream started'}));
    } else if (req.method === 'POST' && req.url === '/stream/stop') {
        stopStream();
        res.writeHead(200, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({status: 'ok', message: 'Stream stopped'}));
    } else if (req.method === 'GET' && req.url === '/stream/video') {
        res.writeHead(200, {
            'Content-Type': 'multipart/x-mixed-replace; boundary=frame',
            'Cache-Control': 'no-cache',
            'Connection': 'close',
            'Pragma': 'no-cache',
        });
        mjpegClients.push(res);
        if (!isStreaming) {
            startStream();
        }
        req.on('close', () => {
            mjpegClients = mjpegClients.filter(c => c !== res);
            if (mjpegClients.length === 0) {
                stopStream();
            }
        });
    } else if (req.method === 'GET' && req.url === '/') {
        // Minimal HTML for browser view
        res.writeHead(200, {'Content-Type': 'text/html'});
        res.end(`
            <html><body>
            <h2>Hikvision Camera Live Stream</h2>
            <img src="/stream/video" style="max-width:100%;"/>
            <form method="post" action="/stream/start"><button type="submit">Start Stream</button></form>
            <form method="post" action="/stream/stop"><button type="submit">Stop Stream</button></form>
            </body></html>
        `);
    } else {
        res.writeHead(404, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({error: 'Not found'}));
    }
});

server.listen(SERVER_PORT, SERVER_HOST, () => {
    console.log(`Hikvision IP Camera HTTP driver listening on http://${SERVER_HOST}:${SERVER_PORT}/`);
});